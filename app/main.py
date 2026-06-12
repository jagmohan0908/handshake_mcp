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
from urllib.parse import urlencode
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
        "whatsapp": env_int("MCP_MAX_WHATSAPP_SENDS_PER_CALL", 1),
        "lead": env_int("MCP_MAX_LEAD_WRITES_PER_CALL", 1),
        "appointment": env_int("MCP_MAX_APPOINTMENT_WRITES_PER_CALL", 1),
    }.get(action, 1)


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
    auth = os.getenv("FRAPPE_AUTHORIZATION", "")
    api_key = os.getenv("FRAPPE_API_KEY", "")
    api_secret = os.getenv("FRAPPE_API_SECRET", "")
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
            data = json.loads(text) if text else {}
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
        raise FrappeError(text[:1000], exc.code) from exc
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


def doctype_path(doctype: str) -> str:
    return doctype.replace(" ", "%20")


def resolve_or_create_conversation(phone: str, channel_account: str) -> str:
    cleaned = normalize_phone(phone)
    candidates = [phone, cleaned, cleaned.removeprefix("+")]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        data = frappe_request(
            "POST",
            "/api/method/wa_chat_hub.api.chat.resolve_chat_for_reference",
            json_body={"reference_doctype": "Chat Contact", "phone_number": candidate, "channel_account": channel_account},
        )
        conversation = unwrap_result(data).get("conversation") or unwrap_result(data).get("conversation_name") or unwrap_result(data).get("chat_conversation")
        if conversation:
            return str(conversation)

    try:
        contact_data = frappe_request("POST", "/api/resource/Chat%20Contact", json_body={"phone_number": cleaned or phone, "display_name": cleaned or phone})
        contact_name = (contact_data.get("data") or {}).get("name") or cleaned or phone
    except FrappeError:
        contact_name = cleaned or phone

    convo_data = frappe_request("POST", "/api/resource/Chat%20Conversation", json_body={"channel_account": channel_account, "contact": contact_name, "status": "Open"})
    conversation = (convo_data.get("data") or {}).get("name")
    if not conversation:
        raise FrappeError("Could not resolve or create Chat Conversation")
    return str(conversation)


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
    template_name = args.get("template_name") or os.getenv("WA_DEFAULT_TEMPLATE", "vobiz_dg")
    channel_account = args.get("channel_account") or os.getenv("WA_DEFAULT_CHANNEL_ACCOUNT", "Interakt SRIAAS Male")
    language_code = args.get("language_code") or os.getenv("WA_DEFAULT_LANGUAGE", "en")
    body_values = args.get("body_values") or [message]
    expected_values = env_int("WA_TEMPLATE_VARIABLE_COUNT", 1)
    if not isinstance(body_values, list) or len(body_values) != expected_values:
        return {"status": "failed", "error": f"template expects {expected_values} body value(s)"}

    def write() -> dict[str, Any]:
        conversation = resolve_or_create_conversation(phone, str(channel_account))
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
        return {"status": "sent" if result.get("sent", True) else "accepted", "conversation": result.get("conversation") or conversation, "message": result.get("message"), "template_name": template_name}

    return guarded_write("whatsapp", {**args, "phone": phone}, write)


def create_or_update_lead(args: dict[str, Any]) -> dict[str, Any]:
    phone = normalize_phone(args.get("phone") or args.get("mobile_no"))
    first_name = str(args.get("first_name") or args.get("name") or "").strip()
    if not phone:
        return {"status": "failed", "error": "phone is required"}
    if not first_name:
        return {"status": "failed", "error": "first_name is required"}
    doctype = os.getenv("FRAPPE_LEAD_DOCTYPE", "Lead")
    phone_field = os.getenv("FRAPPE_LEAD_PHONE_FIELD", "mobile_no")
    payload = {
        "first_name": first_name,
        "middle_name": args.get("middle_name") or "",
        phone_field: phone,
        "phone": phone,
        "gender": args.get("gender") or "Male",
        "sr_lead_message": args.get("sr_lead_message") or args.get("concern") or "",
        "sr_lead_notes": args.get("sr_lead_notes") or "Lead generated from AI gateway",
        "sr_lead_disease": args.get("sr_lead_disease") or "",
        "sr_lead_country": args.get("sr_lead_country") or "India",
    }

    def write() -> dict[str, Any]:
        search = frappe_request("GET", f"/api/resource/{doctype_path(doctype)}", params={"filters": json.dumps([[doctype, phone_field, "=", phone]]), "fields": json.dumps(["name"]), "limit_page_length": 1})
        rows = search.get("data") or []
        if rows:
            name = rows[0]["name"]
            frappe_request("PUT", f"/api/resource/{doctype_path(doctype)}/{name}", json_body=payload)
            return {"status": "updated", "doctype": doctype, "name": name}
        created = frappe_request("POST", f"/api/resource/{doctype_path(doctype)}", json_body=payload)
        return {"status": "created", "doctype": doctype, "name": (created.get("data") or {}).get("name")}

    return guarded_write("lead", {**args, "phone": phone}, write)


def create_appointment(args: dict[str, Any]) -> dict[str, Any]:
    phone = normalize_phone(args.get("phone"))
    patient_name = str(args.get("patient_name") or "").strip()
    preferred_time = str(args.get("preferred_time") or "").strip()
    concern = str(args.get("concern") or "").strip()
    missing = [name for name, value in {"phone": phone, "patient_name": patient_name, "preferred_time": preferred_time, "concern": concern}.items() if not value]
    if missing:
        return {"status": "failed", "error": f"missing required fields: {', '.join(missing)}"}
    doctype = os.getenv("FRAPPE_APPOINTMENT_DOCTYPE", "Patient Appointment")
    customer_email = str(args.get("customer_email") or args.get("email") or "").strip()
    if not customer_email:
        fallback_domain = os.getenv("FRAPPE_APPOINTMENT_EMAIL_DOMAIN", "sriaas.invalid")
        customer_email = f"{phone.replace('+', '')}@{fallback_domain}"
    payload = {
        "scheduled_time": preferred_time,
        "status": os.getenv("FRAPPE_APPOINTMENT_DEFAULT_STATUS", "Open"),
        "customer_name": patient_name,
        "customer_phone_number": phone,
        "customer_email": customer_email,
        "customer_details": concern,
    }
    appointment_with = os.getenv("FRAPPE_APPOINTMENT_WITH", "").strip()
    party = os.getenv("FRAPPE_APPOINTMENT_PARTY", "").strip()
    if appointment_with:
        payload["appointment_with"] = appointment_with
    if party:
        payload["party"] = party

    def write() -> dict[str, Any]:
        created = frappe_request("POST", f"/api/resource/{doctype_path(doctype)}", json_body=payload)
        return {"status": "accepted", "doctype": doctype, "name": (created.get("data") or {}).get("name")}

    return guarded_write("appointment", {**args, "phone": phone}, write)


TOOLS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "send_whatsapp_template": send_whatsapp_template,
    "create_or_update_lead": create_or_update_lead,
    "create_appointment": create_appointment,
}


class McpHandler(BaseHTTPRequestHandler):
    server_version = "HandshakeMCP/1.0"

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
