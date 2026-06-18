import asyncio
import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


APP_NAME = "handshake-mcp"
frappe_write_lock = asyncio.Semaphore(int(os.getenv("MCP_FRAPPE_WRITE_CONCURRENCY", "2")))


def log_event(event: str, **fields: Any) -> None:
    safe_fields = {k: v for k, v in fields.items() if k not in {"authorization", "token", "secret"}}
    print(json.dumps({"event": event, **safe_fields}, default=str), flush=True)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def normalize_phone(phone: str | None) -> str:
    raw = "".join(ch for ch in (phone or "") if ch.isdigit() or ch == "+")
    if not raw:
        return ""
    if raw.startswith("+"):
        return raw
    if raw.startswith("91") and len(raw) == 12:
        return f"+{raw}"
    if len(raw) >= 10:
        return f"+91{raw[-10:]}"
    return raw


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def db_path() -> Path:
    path = Path(os.getenv("MCP_DB_PATH", "data/handshake_mcp.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            create table if not exists idempotency (
                key text primary key,
                created_at real not null,
                expires_at real not null,
                result_json text not null
            );
            create table if not exists counters (
                scope text primary key,
                call_id text not null,
                agent_id text not null,
                action text not null,
                count integer not null,
                updated_at real not null
            );
            create table if not exists circuit_breakers (
                module text primary key,
                opened_until real not null,
                reason text not null,
                updated_at real not null
            );
            create table if not exists audit_log (
                id integer primary key autoincrement,
                created_at text not null,
                agent_id text,
                call_id text,
                action text not null,
                phone text,
                idempotency_key text,
                status text not null,
                detail_json text not null
            );
            """
        )


class FrappeError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def audit(action: str, args: dict[str, Any], status: str, detail: dict[str, Any]) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            insert into audit_log(created_at, agent_id, call_id, action, phone, idempotency_key, status, detail_json)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                args.get("agent_id"),
                args.get("call_id"),
                action,
                args.get("phone"),
                args.get("idempotency_key"),
                status,
                json.dumps(detail, sort_keys=True, default=str),
            ),
        )


def idempotency_get(key: str | None) -> dict[str, Any] | None:
    if not key:
        return None
    with db_connect() as conn:
        row = conn.execute("select result_json from idempotency where key = ? and expires_at > ?", (key, time.time())).fetchone()
    return json.loads(row["result_json"]) if row else None


def idempotency_set(key: str | None, result: dict[str, Any]) -> None:
    if not key:
        return
    ttl = env_int("MCP_IDEMPOTENCY_TTL_SECONDS", 86400)
    now = time.time()
    with db_connect() as conn:
        conn.execute(
            "replace into idempotency(key, created_at, expires_at, result_json) values (?, ?, ?, ?)",
            (key, now, now + ttl, json.dumps(result, sort_keys=True, default=str)),
        )


def limit_for_action(action: str) -> int:
    return {
        "whatsapp": env_int("MCP_MAX_WHATSAPP_SENDS_PER_CALL", 5),
        "whatsapp_template": env_int("MCP_MAX_WHATSAPP_TEMPLATES_PER_CALL", 1),
    }.get(action, 1)


def profile_key_from_args(args: dict[str, Any]) -> str:
    for key in ("profile_key", "profileKey", "voice_agent_profile", "voiceAgentProfile"):
        value = str(args.get(key) or "").strip()
        if value:
            return value
    return ""


def did_number_from_args(args: dict[str, Any]) -> str:
    for key in ("did_number", "didNumber", "sip_called_number", "sipCalledNumber", "called_number", "calledNumber"):
        value = str(args.get(key) or "").strip()
        if value:
            return value
    return ""


def env_json_object(name: str) -> dict[str, Any]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log_event("env_config_error", variable=name, error="not_valid_json")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def whatsapp_config_from_raw(raw_config: Any) -> dict[str, str]:
    if isinstance(raw_config, dict):
        return {
            "profile_key": str(raw_config.get("profile_key") or raw_config.get("profileKey") or "").strip(),
            "channel_account": str(raw_config.get("channel_account") or raw_config.get("channel") or "").strip(),
            "template_name": str(raw_config.get("template_name") or raw_config.get("template") or "").strip(),
            "language_code": str(raw_config.get("language_code") or raw_config.get("language") or "").strip(),
        }
    if raw_config:
        return {"profile_key": "", "channel_account": str(raw_config).strip(), "template_name": "", "language_code": ""}
    return {"profile_key": "", "channel_account": "", "template_name": "", "language_code": ""}


def did_lookup_keys(did_number: str) -> list[str]:
    normalized = normalize_phone(did_number)
    keys = [did_number, normalized, normalized.removeprefix("+")]
    if normalized:
        keys.append(normalized[-10:])
    seen: set[str] = set()
    unique_keys = []
    for key in keys:
        if key and key not in seen:
            unique_keys.append(key)
            seen.add(key)
    return unique_keys


def did_whatsapp_config(args: dict[str, Any]) -> dict[str, str]:
    did_number = did_number_from_args(args)
    did_configs = env_json_object("WA_CHANNEL_ACCOUNTS_BY_DID_JSON")
    for key in did_lookup_keys(did_number):
        raw_config = did_configs.get(key)
        if raw_config:
            config = whatsapp_config_from_raw(raw_config)
            log_event(
                "whatsapp_did_config_applied",
                did_number=did_number,
                did_key=key,
                profile_key=config.get("profile_key") or None,
                channel_account=config.get("channel_account") or None,
                template_name=config.get("template_name") or None,
            )
            return config
    return {"profile_key": "", "channel_account": "", "template_name": "", "language_code": ""}


def resolve_profile_key_for_request(args: dict[str, Any]) -> str:
    requested_profile_key = profile_key_from_args(args)
    did_number = did_number_from_args(args)
    if not did_number:
        return requested_profile_key

    method = os.getenv("FRAPPE_VOICE_AGENT_CONFIG_METHOD", "vobiz_ai.api.voice_agent.get_voice_agent_config").strip().strip("/")
    params = {
        "profile_key": requested_profile_key,
        "voice_agent_profile": requested_profile_key,
        "did_number": did_number,
        "caller_phone": args.get("phone") or "",
        "company_key": args.get("company_key") or args.get("companyKey") or "",
    }
    try:
        payload = frappe_request("GET", f"/api/method/{method}", params=params)
    except FrappeError as exc:
        log_event(
            "voice_profile_lookup_failed",
            profile_key=requested_profile_key,
            did_number=did_number,
            error=str(exc)[:300],
        )
        return requested_profile_key

    config = payload.get("message") if isinstance(payload, dict) else payload
    if not isinstance(config, dict):
        return requested_profile_key
    resolved_profile_key = str(config.get("profile_key") or config.get("profileKey") or "").strip()
    if resolved_profile_key and resolved_profile_key != requested_profile_key:
        log_event(
            "voice_profile_resolved_from_did",
            requested_profile_key=requested_profile_key,
            resolved_profile_key=resolved_profile_key,
            did_number=did_number,
        )
    return resolved_profile_key or requested_profile_key


def profile_whatsapp_config(profile_key: str) -> dict[str, str]:
    profile_configs = env_json_object("WA_CHANNEL_ACCOUNTS_BY_PROFILE_JSON")
    raw_config = profile_configs.get(profile_key) if profile_key else None
    return whatsapp_config_from_raw(raw_config)


def merge_whatsapp_config(base_config: dict[str, str], override_config: dict[str, str]) -> dict[str, str]:
    return {
        "profile_key": override_config.get("profile_key") or base_config.get("profile_key", ""),
        "channel_account": override_config.get("channel_account") or base_config.get("channel_account", ""),
        "template_name": override_config.get("template_name") or base_config.get("template_name", ""),
        "language_code": override_config.get("language_code") or base_config.get("language_code", ""),
    }


def channel_account_for_request(args: dict[str, Any], profile_config: dict[str, str]) -> str:
    mapped_channel = profile_config.get("channel_account", "")
    if mapped_channel:
        return mapped_channel
    explicit_channel = str(args.get("channel_account") or "").strip()
    return explicit_channel


def template_name_for_request(args: dict[str, Any], profile_config: dict[str, str]) -> str:
    mapped_template = profile_config.get("template_name", "")
    if mapped_template:
        return mapped_template
    explicit_template = str(args.get("template_name") or "").strip()
    return explicit_template


def reserve_limit(action: str, args: dict[str, Any]) -> dict[str, Any] | None:
    agent_id = str(args.get("agent_id") or "unknown-agent")
    call_id = str(args.get("call_id") or "unknown-call")
    action_scope = f"{agent_id}:{call_id}:{action}"
    total_scope = f"{agent_id}:{call_id}:total"
    now = time.time()
    with db_connect() as conn:
        action_count = conn.execute("select count from counters where scope = ?", (action_scope,)).fetchone()
        total_count = conn.execute("select count from counters where scope = ?", (total_scope,)).fetchone()
        if action_count and action_count["count"] >= limit_for_action(action):
            return {"status": "blocked", "reason": f"{action}_limit_reached"}
        if total_count and total_count["count"] >= env_int("MCP_MAX_FRAPPE_CALLS_PER_CALL", 10):
            return {"status": "blocked", "reason": "total_frappe_call_limit_reached"}
        conn.execute(
            "replace into counters(scope, call_id, agent_id, action, count, updated_at) values (?, ?, ?, ?, ?, ?)",
            (action_scope, call_id, agent_id, action, (action_count["count"] if action_count else 0) + 1, now),
        )
        conn.execute(
            "replace into counters(scope, call_id, agent_id, action, count, updated_at) values (?, ?, ?, ?, ?, ?)",
            (total_scope, call_id, agent_id, "total", (total_count["count"] if total_count else 0) + 1, now),
        )
    return None


def circuit_status(module: str) -> dict[str, Any] | None:
    with db_connect() as conn:
        row = conn.execute("select opened_until, reason from circuit_breakers where module = ?", (module,)).fetchone()
    if row and row["opened_until"] > time.time():
        return {"status": "deferred", "reason": row["reason"], "retry_after_seconds": int(row["opened_until"] - time.time())}
    return None


def open_circuit(module: str, reason: str) -> None:
    now = time.time()
    with db_connect() as conn:
        conn.execute(
            "replace into circuit_breakers(module, opened_until, reason, updated_at) values (?, ?, ?, ?)",
            (module, now + env_int("MCP_CIRCUIT_BREAKER_SECONDS", 60), reason, now),
        )


def frappe_headers() -> dict[str, str]:
    auth = os.getenv("FRAPPE_AUTHORIZATION", "").strip()
    api_key = os.getenv("FRAPPE_API_KEY", "").strip()
    api_secret = os.getenv("FRAPPE_API_SECRET", "").strip()
    if not auth and api_key and api_secret:
        auth = f"token {api_key}:{api_secret}"
    if auth and not auth.lower().startswith(("token ", "bearer ")):
        auth = f"token {auth}"
    user_agent = os.getenv(
        "FRAPPE_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36",
    )
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": os.getenv("FRAPPE_ACCEPT_LANGUAGE", "en-US,en;q=0.9"),
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "Origin": os.getenv("FRAPPE_ORIGIN", frappe_base_url()),
        "Pragma": "no-cache",
        "Referer": os.getenv("FRAPPE_REFERER", f"{frappe_base_url()}/"),
        "User-Agent": user_agent,
        "X-Requested-With": "XMLHttpRequest",
    }
    extra_headers = os.getenv("FRAPPE_EXTRA_HEADERS_JSON", "").strip()
    if extra_headers:
        try:
            parsed_headers = json.loads(extra_headers)
            if isinstance(parsed_headers, dict):
                headers.update({str(k): str(v) for k, v in parsed_headers.items()})
        except json.JSONDecodeError:
            log_event("frappe_header_config_error", error="FRAPPE_EXTRA_HEADERS_JSON is not valid JSON")
    if auth:
        headers["Authorization"] = auth
    return headers


def frappe_error_message(text: str, path: str) -> str:
    if "AuthenticationError" in text or "validate_api_key_secret" in text:
        return (
            f"Frappe rejected API credentials while calling {path}. "
            "Check FRAPPE_AUTHORIZATION is exactly 'token <api_key>:<api_secret>', "
            "or set FRAPPE_API_KEY and FRAPPE_API_SECRET. Verify the key/secret are active "
            "for a Frappe user with permission to call the wa_chat_hub APIs."
        )
    return text[:1000]


def frappe_base_url() -> str:
    base_url = os.getenv("FRAPPE_BASE_URL") or os.getenv("FRAPPE_URL") or ""
    if not base_url:
        raise FrappeError("FRAPPE_BASE_URL is not configured")
    return base_url.rstrip("/")


def frappe_request(method: str, path: str, *, json_body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = f"?{urlencode(params)}" if params else ""
    url = f"{frappe_base_url()}{path}{query}"
    body = json.dumps(json_body).encode("utf-8") if json_body is not None else None
    request = Request(url, data=body, method=method, headers=frappe_headers())
    try:
        with urlopen(request, timeout=env_int("FRAPPE_TIMEOUT_SECONDS", 20)) as response:
            text = response.read().decode("utf-8")
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError as exc:
                raise FrappeError(f"Frappe returned non-JSON response from {path}: {text[:300]}") from exc
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        if exc.code == 403 and ("error_code\":1010" in text or "browser_signature_banned" in text):
            raise FrappeError(
                "Cloudflare 1010 blocked the MCP server while calling Frappe. "
                "Allowlist the Render outbound traffic/service, relax Cloudflare Bot Fight/BIC rules for API paths, "
                "or add a server-to-server bypass rule for Authorization-token requests. "
                f"Raw: {text[:700]}",
                exc.code,
            ) from exc
        raise FrappeError(frappe_error_message(text, path), exc.code) from exc
    except URLError as exc:
        raise FrappeError(str(exc), None) from exc
    if isinstance(data, dict) and data.get("exc"):
        raise FrappeError(str(data.get("exc"))[:1000])
    return data if isinstance(data, dict) else {"data": data}


def unwrap_result(data: dict[str, Any]) -> dict[str, Any]:
    message = data.get("message", data)
    if isinstance(message, dict) and isinstance(message.get("result"), dict):
        return message["result"]
    return message if isinstance(message, dict) else {}


def chat_conversation_detail(conversation: str) -> dict[str, Any]:
    if not conversation:
        return {}
    try:
        data = frappe_request("GET", f"/api/resource/Chat%20Conversation/{quote(str(conversation), safe='')}")
    except FrappeError as exc:
        log_event(
            "conversation_detail_lookup_failed",
            conversation=conversation,
            error=str(exc)[:300],
        )
        return {}
    detail = data.get("data") if isinstance(data, dict) else {}
    return detail if isinstance(detail, dict) else {}


def same_channel(left: str | None, right: str | None) -> bool:
    return str(left or "").strip().lower() == str(right or "").strip().lower()


def chat_contact_detail(contact: str) -> dict[str, Any]:
    if not contact:
        return {}
    try:
        data = frappe_request("GET", f"/api/resource/Chat%20Contact/{quote(str(contact), safe='')}")
    except FrappeError as exc:
        log_event(
            "contact_detail_lookup_failed",
            contact=contact,
            error=str(exc)[:300],
        )
        return {}
    detail = data.get("data") if isinstance(data, dict) else {}
    return detail if isinstance(detail, dict) else {}


def contact_phone_candidates(contact_detail: dict[str, Any], contact_name: str = "") -> list[str]:
    values = [
        contact_detail.get("phone_number"),
        contact_detail.get("mobile_no"),
        contact_detail.get("mobile"),
        contact_detail.get("phone"),
        contact_detail.get("whatsapp_number"),
        contact_detail.get("name"),
        contact_name,
    ]
    seen: set[str] = set()
    phones = []
    for value in values:
        phone = normalize_phone(str(value or ""))
        if phone and phone not in seen:
            phones.append(phone)
            seen.add(phone)
    return phones


def same_phone(left: str | None, right: str | None) -> bool:
    left_phone = normalize_phone(str(left or ""))
    right_phone = normalize_phone(str(right or ""))
    if not left_phone or not right_phone:
        return False
    return left_phone == right_phone or left_phone[-10:] == right_phone[-10:]


def conversation_recipient_info(conversation: str, requested_phone: str) -> dict[str, Any]:
    detail = chat_conversation_detail(conversation)
    contact = str(detail.get("contact") or "").strip()
    contact_detail = chat_contact_detail(contact)
    contact_phones = contact_phone_candidates(contact_detail, contact)
    primary_contact_phone = contact_phones[0] if contact_phones else ""
    return {
        "requested_recipient_phone": normalize_phone(requested_phone),
        "requested_recipient_phone_suffix": normalize_phone(requested_phone)[-4:],
        "conversation_contact": contact or None,
        "conversation_contact_phone": primary_contact_phone or None,
        "conversation_contact_phone_suffix": primary_contact_phone[-4:] if primary_contact_phone else None,
        "conversation_contact_phone_matches": any(same_phone(requested_phone, contact_phone) for contact_phone in contact_phones) if contact_phones else None,
    }


def create_conversation_for_channel(contact_name: str, phone: str, channel_account: str) -> str:
    contact = contact_name
    if not contact:
        try:
            contact_data = frappe_request("POST", "/api/resource/Chat%20Contact", json_body={"phone_number": phone, "display_name": phone})
            contact = (contact_data.get("data") or {}).get("name") or phone
        except FrappeError:
            contact = phone

    convo_data = frappe_request("POST", "/api/resource/Chat%20Conversation", json_body={"channel_account": channel_account, "contact": contact, "status": "Open"})
    conversation = (convo_data.get("data") or {}).get("name")
    if not conversation:
        raise FrappeError("Could not resolve or create Chat Conversation")
    log_event(
        "conversation_created_for_channel",
        conversation=conversation,
        contact=contact,
        channel_account=channel_account,
    )
    return str(conversation)


def resolve_or_create_conversation(phone: str, channel_account: str) -> str:
    cleaned = normalize_phone(phone)
    candidates = [phone, cleaned, cleaned.removeprefix("+")]
    seen: set[str] = set()
    fallback_contact = ""
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        data = frappe_request(
            "POST",
            "/api/method/wa_chat_hub.api.chat.resolve_chat_for_reference",
            json_body={"reference_doctype": "Chat Contact", "phone_number": candidate, "channel_account": channel_account},
        )
        result = unwrap_result(data)
        conversation = result.get("conversation") or result.get("conversation_name") or result.get("chat_conversation")
        if conversation:
            detail = chat_conversation_detail(str(conversation))
            actual_channel = detail.get("channel_account") or result.get("channel_account")
            fallback_contact = str(detail.get("contact") or result.get("contact") or fallback_contact or "").strip()
            if not same_channel(actual_channel, channel_account):
                log_event(
                    "conversation_channel_mismatch",
                    conversation=conversation,
                    expected_channel=channel_account,
                    actual_channel=actual_channel or None,
                    contact=fallback_contact or None,
                )
                continue

            contact_detail = chat_contact_detail(fallback_contact)
            contact_phones = contact_phone_candidates(contact_detail, fallback_contact)
            phone_matches = any(same_phone(cleaned or phone, contact_phone) for contact_phone in contact_phones)
            if phone_matches or not contact_phones:
                log_event(
                    "conversation_resolved_for_channel",
                    conversation=conversation,
                    channel_account=channel_account,
                    contact=fallback_contact or None,
                    contact_phone_suffix=(contact_phones[0][-4:] if contact_phones else None),
                    requested_phone_suffix=(cleaned or phone)[-4:],
                )
                return str(conversation)
            if contact_phones and not phone_matches:
                log_event(
                    "conversation_contact_mismatch",
                    conversation=conversation,
                    channel_account=channel_account,
                    contact=fallback_contact or None,
                    contact_phone_suffixes=[contact_phone[-4:] for contact_phone in contact_phones],
                    requested_phone_suffix=(cleaned or phone)[-4:],
                )
                fallback_contact = ""

    return create_conversation_for_channel(fallback_contact, cleaned or phone, channel_account)


def guarded_write(action: str, args: dict[str, Any], fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    existing = idempotency_get(args.get("idempotency_key"))
    if existing:
        audit(action, args, "duplicate", existing)
        log_event("mcp_duplicate", action=action, status=existing.get("status"), idempotency_key=args.get("idempotency_key"))
        return {**existing, "duplicate": True}
    blocked = reserve_limit(action, args)
    if blocked:
        audit(action, args, "blocked", blocked)
        idempotency_set(args.get("idempotency_key"), blocked)
        log_event("mcp_blocked", action=action, reason=blocked.get("reason"), idempotency_key=args.get("idempotency_key"))
        return blocked
    circuit = circuit_status(action)
    if circuit:
        audit(action, args, "deferred", circuit)
        idempotency_set(args.get("idempotency_key"), circuit)
        log_event("mcp_deferred", action=action, reason=circuit.get("reason"), idempotency_key=args.get("idempotency_key"))
        return circuit
    try:
        result = fn()
        idempotency_set(args.get("idempotency_key"), result)
        audit(action, args, str(result.get("status", "accepted")), result)
        log_event("mcp_result", action=action, status=result.get("status"), name=result.get("name"), idempotency_key=args.get("idempotency_key"))
        return result
    except FrappeError as exc:
        if exc.status_code in (429, 500, 502, 503, 504) or "Lock wait timeout" in str(exc):
            open_circuit(action, str(exc)[:300])
            result = {"status": "deferred", "reason": "frappe_temporarily_unavailable", "error": str(exc)[:500]}
        else:
            result = {"status": "failed", "error": str(exc)[:500]}
        idempotency_set(args.get("idempotency_key"), result)
        audit(action, args, result["status"], result)
        log_event("mcp_error", action=action, status=result["status"], error=result.get("error"), idempotency_key=args.get("idempotency_key"))
        return result


def send_whatsapp_template(args: dict[str, Any]) -> dict[str, Any]:
    phone = normalize_phone(args.get("phone"))
    message = str(args.get("message") or "").strip()
    if not phone:
        return {"status": "failed", "error": "phone is required"}
    if not message:
        return {"status": "failed", "error": "message is required"}
    did_config = did_whatsapp_config(args)
    profile_key = did_config.get("profile_key") or resolve_profile_key_for_request(args)
    profile_config = merge_whatsapp_config(profile_whatsapp_config(profile_key), did_config)
    template_name = template_name_for_request(args, profile_config)
    channel_account = channel_account_for_request(args, profile_config)
    language_code = profile_config.get("language_code") or args.get("language_code") or os.getenv("WA_DEFAULT_LANGUAGE", "en")
    if not template_name:
        return {"status": "failed", "error": f"template_name is required for profile: {profile_key or 'unknown'}"}
    if not channel_account:
        return {"status": "failed", "error": f"channel_account is required for profile: {profile_key or 'unknown'}"}
    body_values = args.get("body_values") or [message]
    expected_values = env_int("WA_TEMPLATE_VARIABLE_COUNT", 1)
    if not isinstance(body_values, list) or len(body_values) != expected_values:
        return {"status": "failed", "error": f"template expects {expected_values} body value(s)"}

    def write() -> dict[str, Any]:
        conversation = resolve_or_create_conversation(phone, str(channel_account))
        recipient_info = conversation_recipient_info(conversation, phone)
        log_event(
            "whatsapp_template_request",
            profile_key=profile_key,
            phone_suffix=phone[-4:],
            requested_recipient_phone=recipient_info.get("requested_recipient_phone"),
            conversation_contact=recipient_info.get("conversation_contact"),
            conversation_contact_phone=recipient_info.get("conversation_contact_phone"),
            conversation_contact_phone_matches=recipient_info.get("conversation_contact_phone_matches"),
            conversation=conversation,
            template_name=template_name,
            channel_account=channel_account,
            language_code=language_code,
            body_value_count=len(body_values),
            message_len=len(message),
            idempotency_key=args.get("idempotency_key"),
        )
        data = frappe_request(
            "POST",
            "/api/method/wa_chat_hub.api.runtime.send_template_message",
            json_body={
                "conversation": conversation,
                "template_name": template_name,
                "language_code": language_code,
                "body_values": body_values,
                "header_values": [],
                "button_values": {},
                "button_payload": {},
                "body": f"Template: {template_name}",
            },
        )
        result = unwrap_result(data)
        sent_value = result.get("sent")
        delivery_status = str(result.get("status") or result.get("delivery_status") or "").lower()
        error_message = result.get("error")
        if sent_value is True or delivery_status in {"sent", "success", "delivered"}:
            status = "sent"
        elif sent_value is False or delivery_status in {"failed", "error", "rejected"} or error_message:
            status = "failed"
        else:
            status = "accepted"
        if status != "sent":
            log_event(
                "whatsapp_template_not_confirmed_sent",
                profile_key=profile_key,
                phone_suffix=phone[-4:],
                requested_recipient_phone=recipient_info.get("requested_recipient_phone"),
                conversation_contact=recipient_info.get("conversation_contact"),
                conversation_contact_phone=recipient_info.get("conversation_contact_phone"),
                conversation_contact_phone_matches=recipient_info.get("conversation_contact_phone_matches"),
                conversation=result.get("conversation") or conversation,
                template_name=template_name,
                channel_account=channel_account,
                sent=sent_value,
                delivery_status=delivery_status,
                error=error_message,
                frappe_result=json.dumps(result, default=str)[:1000],
                idempotency_key=args.get("idempotency_key"),
            )
        return {
            "status": status,
            "conversation": result.get("conversation") or conversation,
            "message": result.get("message"),
            "profile_key": profile_key or None,
            "template_name": template_name,
            "channel_account": channel_account,
            "requested_recipient_phone": recipient_info.get("requested_recipient_phone"),
            "conversation_contact": recipient_info.get("conversation_contact"),
            "conversation_contact_phone": recipient_info.get("conversation_contact_phone"),
            "conversation_contact_phone_matches": recipient_info.get("conversation_contact_phone_matches"),
            "delivery_status": delivery_status or None,
            "sent": sent_value,
            "error": error_message,
            "frappe_result": result,
        }

    return guarded_write("whatsapp_template", {**args, "phone": phone}, write)


def send_whatsapp_message(args: dict[str, Any]) -> dict[str, Any]:
    phone = normalize_phone(args.get("phone"))
    message = str(args.get("message") or "").strip()
    if not phone:
        return {"status": "failed", "error": "phone is required"}
    if not message:
        return {"status": "failed", "error": "message is required"}
    did_config = did_whatsapp_config(args)
    profile_key = did_config.get("profile_key") or resolve_profile_key_for_request(args)
    profile_config = merge_whatsapp_config(profile_whatsapp_config(profile_key), did_config)
    channel_account = channel_account_for_request(args, profile_config)
    if not channel_account:
        return {"status": "failed", "error": f"channel_account is required for profile: {profile_key or 'unknown'}"}

    def write() -> dict[str, Any]:
        conversation = resolve_or_create_conversation(phone, str(channel_account))
        recipient_info = conversation_recipient_info(conversation, phone)
        log_event(
            "whatsapp_message_request",
            profile_key=profile_key,
            phone_suffix=phone[-4:],
            requested_recipient_phone=recipient_info.get("requested_recipient_phone"),
            conversation_contact=recipient_info.get("conversation_contact"),
            conversation_contact_phone=recipient_info.get("conversation_contact_phone"),
            conversation_contact_phone_matches=recipient_info.get("conversation_contact_phone_matches"),
            conversation=conversation,
            channel_account=channel_account,
            message_len=len(message),
            idempotency_key=args.get("idempotency_key"),
        )
        method = os.getenv("WA_SEND_MESSAGE_METHOD", "wa_chat_hub.api.runtime.send_message").strip().strip("/")
        data = frappe_request(
            "POST",
            f"/api/method/{method}",
            json_body={
                "conversation": conversation,
                "message": message,
                "content": message,
                "body": message,
            },
        )
        result = unwrap_result(data)
        sent_value = result.get("sent")
        delivery_status = str(result.get("status") or result.get("delivery_status") or "").lower()
        error_message = result.get("error")
        if sent_value is True or delivery_status in {"sent", "success", "delivered"}:
            status = "sent"
        elif sent_value is False or delivery_status in {"failed", "error", "rejected"} or error_message:
            status = "failed"
        else:
            status = "accepted"
        if status != "sent":
            log_event(
                "whatsapp_message_not_confirmed_sent",
                profile_key=profile_key,
                phone_suffix=phone[-4:],
                conversation=result.get("conversation") or conversation,
                channel_account=channel_account,
                sent=sent_value,
                delivery_status=delivery_status,
                error=error_message,
                frappe_result=json.dumps(result, default=str)[:1000],
                idempotency_key=args.get("idempotency_key"),
            )
        return {
            "status": status,
            "conversation": result.get("conversation") or conversation,
            "message": result.get("message"),
            "profile_key": profile_key or None,
            "channel_account": channel_account,
            "requested_recipient_phone": recipient_info.get("requested_recipient_phone"),
            "conversation_contact": recipient_info.get("conversation_contact"),
            "conversation_contact_phone": recipient_info.get("conversation_contact_phone"),
            "conversation_contact_phone_matches": recipient_info.get("conversation_contact_phone_matches"),
            "delivery_status": delivery_status or None,
            "sent": sent_value,
            "error": error_message,
            "frappe_result": result,
        }

    return guarded_write("whatsapp", {**args, "phone": phone}, write)


TOOLS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "send_whatsapp_message": send_whatsapp_message,
    "send_whatsapp_template": send_whatsapp_template,
}


class McpHandler(BaseHTTPRequestHandler):
    server_version = "HandshakeMCP/1.0"

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
            log_event(
                "client_disconnected_before_response",
                path=self.path,
                status=status,
                error=str(exc),
            )

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"status": "ok", "service": APP_NAME})
        else:
            self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/mcp":
            self.send_json(404, {"error": "not_found"})
            return
        expected = os.getenv("MCP_BEARER_TOKEN", "")
        auth = self.headers.get("Authorization", "")
        if not expected:
            self.send_json(500, {"error": "MCP_BEARER_TOKEN is not configured"})
            return
        if auth != f"Bearer {expected}":
            self.send_json(401, {"error": "invalid_or_missing_bearer_token"})
            return
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
        try:
            payload = json.loads(raw.decode("utf-8"))
            request_id = payload.get("id")
            if payload.get("method") != "tools/call":
                self.send_json(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Only tools/call is supported"}})
                return
            params = payload.get("params") or {}
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            handler = TOOLS.get(tool_name)
            if not handler:
                self.send_json(200, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}})
                return
            result = handler(arguments)
            log_event("mcp_tool_call", tool=tool_name, status=result.get("status"), error=result.get("error"), idempotency_key=arguments.get("idempotency_key"))
            self.send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:
            log_event("mcp_exception", error=str(exc))
            self.send_json(200, {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}})


def run() -> None:
    init_db()
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = env_int("MCP_PORT", 8100)
    server = ThreadingHTTPServer((host, port), McpHandler)
    print(f"{APP_NAME} listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
