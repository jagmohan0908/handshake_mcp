import os
import json

os.environ.setdefault("MCP_BEARER_TOKEN", "test-token")
os.environ.setdefault("MCP_DB_PATH", "data/test_gateway.sqlite3")

import app.main as gateway  # noqa: E402
from app.main import init_db, normalize_phone, send_whatsapp_template  # noqa: E402


def test_gateway_exposes_only_whatsapp_tool():
    assert sorted(gateway.TOOLS) == ["send_whatsapp_template"]


def test_normalize_india_phone():
    assert normalize_phone("99999 99999") == "+919999999999"


def test_whatsapp_validates_template_variable_count():
    init_db()
    result = send_whatsapp_template(
        {
            "phone": "+919999999999",
            "message": "Address",
            "body_values": [],
            "agent_id": "agent",
            "call_id": "call-template-variable-count",
            "idempotency_key": "key-1",
        }
    )
    assert result["status"] == "failed"


def test_whatsapp_frappe_failed_delivery_is_failed(monkeypatch):
    init_db()

    monkeypatch.setenv(
        "WA_CHANNEL_ACCOUNTS_BY_PROFILE_JSON",
        json.dumps({"default-test-profile": {"channel_account": "Interakt SRIAAS Male", "template_name": "vobiz_ai"}}),
    )
    monkeypatch.setattr(gateway, "resolve_or_create_conversation", lambda phone, channel: "conversation-1")

    def fake_frappe_request(method, path, *, json_body=None, params=None):
        return {
            "message": {
                "conversation": "conversation-1",
                "sent": False,
                "delivery_status": "Failed",
                "error": "No approved template found",
            }
        }

    monkeypatch.setattr(gateway, "frappe_request", fake_frappe_request)

    result = send_whatsapp_template(
        {
            "profile_key": "default-test-profile",
            "phone": "+919999999999",
            "message": "Address",
            "body_values": ["Address"],
            "agent_id": "agent",
            "call_id": "call-failed-delivery",
            "idempotency_key": "key-failed-delivery",
        }
    )

    assert result["status"] == "failed"
    assert result["delivery_status"] == "failed"
    assert result["error"] == "No approved template found"


def test_whatsapp_uses_profile_channel_mapping(monkeypatch):
    init_db()
    captured = {}
    monkeypatch.setenv(
        "WA_CHANNEL_ACCOUNTS_BY_PROFILE_JSON",
        json.dumps(
            {
                "male-kamal-sriaas": {
                    "channel_account": "Interakt SRIAAS Male",
                    "template_name": "vobiz_ai",
                    "language_code": "en",
                }
            }
        ),
    )
    def fake_resolve_conversation(phone, channel):
        captured["channel"] = channel
        return "conversation-1"

    monkeypatch.setattr(gateway, "resolve_or_create_conversation", fake_resolve_conversation)

    def fake_frappe_request(method, path, *, json_body=None, params=None):
        captured["template_body"] = json_body
        return {
            "message": {
                "conversation": "conversation-1",
                "sent": True,
                "delivery_status": "Sent",
            }
        }

    monkeypatch.setattr(gateway, "frappe_request", fake_frappe_request)

    result = send_whatsapp_template(
        {
            "profile_key": "male-kamal-sriaas",
            "phone": "+919999999999",
            "message": "Address",
            "body_values": ["Address"],
            "agent_id": "agent",
            "call_id": "call-profile-channel",
            "idempotency_key": "key-profile-channel",
        }
    )

    assert result["status"] == "sent"
    assert result["profile_key"] == "male-kamal-sriaas"
    assert result["template_name"] == "vobiz_ai"
    assert result["channel_account"] == "Interakt SRIAAS Male"
    assert captured["channel"] == "Interakt SRIAAS Male"


def test_profile_mapping_overrides_request_channel_and_template(monkeypatch):
    init_db()
    captured = {}
    monkeypatch.setenv(
        "WA_CHANNEL_ACCOUNTS_BY_PROFILE_JSON",
        json.dumps(
            {
                "seedfit-agent": {
                    "channel_account": "seedfit-interakt",
                    "template_name": "vobiz_seedfit_pg",
                    "language_code": "en",
                }
            }
        ),
    )

    def fake_resolve_conversation(phone, channel):
        captured["channel"] = channel
        return "conversation-1"

    monkeypatch.setattr(gateway, "resolve_or_create_conversation", fake_resolve_conversation)

    def fake_frappe_request(method, path, *, json_body=None, params=None):
        captured["template_body"] = json_body
        return {
            "message": {
                "conversation": "conversation-1",
                "sent": True,
                "delivery_status": "Sent",
            }
        }

    monkeypatch.setattr(gateway, "frappe_request", fake_frappe_request)

    result = send_whatsapp_template(
        {
            "profile_key": "seedfit-agent",
            "phone": "+919873090386",
            "message": "Address",
            "body_values": ["Address"],
            "channel_account": "sriaas-test",
            "template_name": "wrong_template",
            "agent_id": "agent",
            "call_id": "call-profile-override",
            "idempotency_key": "key-profile-override",
        }
    )

    assert result["status"] == "sent"
    assert result["channel_account"] == "seedfit-interakt"
    assert result["template_name"] == "vobiz_seedfit_pg"
    assert captured["channel"] == "seedfit-interakt"
    assert captured["template_body"]["template_name"] == "vobiz_seedfit_pg"


def test_did_profile_lookup_overrides_wrong_request_profile(monkeypatch):
    init_db()
    captured = {}
    monkeypatch.setenv(
        "WA_CHANNEL_ACCOUNTS_BY_PROFILE_JSON",
        json.dumps(
            {
                "siya-agent": {
                    "channel_account": "sriaas-test",
                    "template_name": "vobiz_ai",
                    "language_code": "en",
                },
                "seedfit-agent": {
                    "channel_account": "seedfit-interakt",
                    "template_name": "vobiz_seedfit_pg",
                    "language_code": "en",
                },
            }
        ),
    )

    def fake_resolve_conversation(phone, channel):
        captured["channel"] = channel
        return "conversation-1"

    monkeypatch.setattr(gateway, "resolve_or_create_conversation", fake_resolve_conversation)

    def fake_frappe_request(method, path, *, json_body=None, params=None):
        if path.endswith("/vobiz_ai.api.voice_agent.get_voice_agent_config"):
            captured["profile_lookup_params"] = params
            return {"message": {"profile_key": "seedfit-agent"}}
        captured["template_body"] = json_body
        return {
            "message": {
                "conversation": "conversation-1",
                "sent": True,
                "delivery_status": "Sent",
            }
        }

    monkeypatch.setattr(gateway, "frappe_request", fake_frappe_request)

    result = send_whatsapp_template(
        {
            "profile_key": "siya-agent",
            "did_number": "+919262171487",
            "phone": "+919873090386",
            "message": "Address",
            "body_values": ["Address"],
            "agent_id": "agent",
            "call_id": "call-did-profile-override",
            "idempotency_key": "key-did-profile-override",
        }
    )

    assert result["status"] == "sent"
    assert result["profile_key"] == "seedfit-agent"
    assert result["channel_account"] == "seedfit-interakt"
    assert result["template_name"] == "vobiz_seedfit_pg"
    assert captured["profile_lookup_params"]["did_number"] == "+919262171487"
    assert captured["channel"] == "seedfit-interakt"
    assert captured["template_body"]["template_name"] == "vobiz_seedfit_pg"


def test_did_whatsapp_mapping_overrides_profile_channel(monkeypatch):
    init_db()
    captured = {}
    monkeypatch.setenv(
        "WA_CHANNEL_ACCOUNTS_BY_PROFILE_JSON",
        json.dumps(
            {
                "siya-agent": {
                    "channel_account": "sriaas-test",
                    "template_name": "vobiz_ai",
                    "language_code": "en",
                }
            }
        ),
    )
    monkeypatch.setenv(
        "WA_CHANNEL_ACCOUNTS_BY_DID_JSON",
        json.dumps(
            {
                "+919262171462": {
                    "profile_key": "siya-agent",
                    "channel_account": "siya-interakt",
                    "template_name": "vobiz_siya",
                    "language_code": "en",
                }
            }
        ),
    )

    def fake_resolve_conversation(phone, channel):
        captured["channel"] = channel
        return "conversation-1"

    monkeypatch.setattr(gateway, "resolve_or_create_conversation", fake_resolve_conversation)

    def fake_frappe_request(method, path, *, json_body=None, params=None):
        captured["template_body"] = json_body
        return {
            "message": {
                "conversation": "conversation-1",
                "sent": True,
                "delivery_status": "Sent",
            }
        }

    monkeypatch.setattr(gateway, "frappe_request", fake_frappe_request)

    result = send_whatsapp_template(
        {
            "profile_key": "siya-agent",
            "did_number": "+919262171462",
            "phone": "+919873090386",
            "message": "Address",
            "body_values": ["Address"],
            "agent_id": "agent",
            "call_id": "call-did-whatsapp-mapping",
            "idempotency_key": "key-did-whatsapp-mapping",
        }
    )

    assert result["status"] == "sent"
    assert result["profile_key"] == "siya-agent"
    assert result["channel_account"] == "siya-interakt"
    assert result["template_name"] == "vobiz_siya"
    assert captured["channel"] == "siya-interakt"
    assert captured["template_body"]["template_name"] == "vobiz_siya"


def test_wrong_channel_resolved_conversation_is_not_used(monkeypatch):
    init_db()
    captured = {"created_conversations": []}
    monkeypatch.setenv(
        "WA_CHANNEL_ACCOUNTS_BY_DID_JSON",
        json.dumps(
            {
                "+919262171487": {
                    "profile_key": "seedfit-agent",
                    "channel_account": "seedfit-interakt",
                    "template_name": "vobiz_seedfit_pg",
                    "language_code": "en",
                }
            }
        ),
    )

    def fake_frappe_request(method, path, *, json_body=None, params=None):
        if path.endswith("/wa_chat_hub.api.chat.resolve_chat_for_reference"):
            return {"message": {"conversation": "223"}}
        if path == "/api/resource/Chat%20Conversation/223":
            return {"data": {"name": "223", "channel_account": "sriaas-test", "contact": "contact-1"}}
        if method == "POST" and path == "/api/resource/Chat%20Conversation":
            captured["created_conversations"].append(json_body)
            return {"data": {"name": "224"}}
        if path.endswith("/wa_chat_hub.api.runtime.send_template_message"):
            captured["template_body"] = json_body
            return {
                "message": {
                    "conversation": json_body["conversation"],
                    "sent": True,
                    "delivery_status": "Sent",
                }
            }
        raise AssertionError(f"Unexpected request {method} {path}")

    monkeypatch.setattr(gateway, "frappe_request", fake_frappe_request)

    result = send_whatsapp_template(
        {
            "profile_key": "seedfit-agent",
            "did_number": "+919262171487",
            "phone": "+919873090386",
            "message": "Address",
            "body_values": ["Address"],
            "agent_id": "agent",
            "call_id": "call-wrong-channel-conversation",
            "idempotency_key": "key-wrong-channel-conversation",
        }
    )

    assert result["status"] == "sent"
    assert result["conversation"] == "224"
    assert captured["created_conversations"] == [
        {"channel_account": "seedfit-interakt", "contact": "contact-1", "status": "Open"}
    ]
    assert captured["template_body"]["conversation"] == "224"


def test_wrong_contact_resolved_conversation_is_not_used(monkeypatch):
    init_db()
    captured = {"created_conversations": []}
    monkeypatch.setenv(
        "WA_CHANNEL_ACCOUNTS_BY_DID_JSON",
        json.dumps(
            {
                "+919262171492": {
                    "profile_key": "Vobiz-parkinson",
                    "channel_account": "SRIAAS Parkinsons",
                    "template_name": "vobiz_park",
                    "language_code": "en",
                }
            }
        ),
    )

    def fake_frappe_request(method, path, *, json_body=None, params=None):
        if path.endswith("/wa_chat_hub.api.chat.resolve_chat_for_reference"):
            return {"message": {"conversation": "47239"}}
        if path == "/api/resource/Chat%20Conversation/47239":
            return {"data": {"name": "47239", "channel_account": "SRIAAS Parkinsons", "contact": "contact-wrong"}}
        if path == "/api/resource/Chat%20Contact/contact-wrong":
            return {"data": {"name": "contact-wrong", "phone_number": "+919999990000"}}
        if method == "POST" and path == "/api/resource/Chat%20Contact":
            captured["created_contact"] = json_body
            return {"data": {"name": "contact-correct"}}
        if method == "POST" and path == "/api/resource/Chat%20Conversation":
            captured["created_conversations"].append(json_body)
            return {"data": {"name": "47240"}}
        if path.endswith("/wa_chat_hub.api.runtime.send_template_message"):
            captured["template_body"] = json_body
            return {
                "message": {
                    "conversation": json_body["conversation"],
                    "sent": True,
                    "delivery_status": "Sent",
                }
            }
        raise AssertionError(f"Unexpected request {method} {path}")

    monkeypatch.setattr(gateway, "frappe_request", fake_frappe_request)

    result = send_whatsapp_template(
        {
            "profile_key": "Vobiz-parkinson",
            "did_number": "+919262171492",
            "phone": "+919873090498",
            "message": "Address",
            "body_values": ["Address"],
            "agent_id": "agent",
            "call_id": "call-wrong-contact-conversation",
            "idempotency_key": "key-wrong-contact-conversation",
        }
    )

    assert result["status"] == "sent"
    assert result["conversation"] == "47240"
    assert captured["created_contact"] == {"phone_number": "+919873090498", "display_name": "+919873090498"}
    assert captured["created_conversations"] == [
        {"channel_account": "SRIAAS Parkinsons", "contact": "contact-correct", "status": "Open"}
    ]
    assert captured["template_body"]["conversation"] == "47240"


def test_frappe_non_json_response_raises_frappe_error(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"<!doctype html><html>login</html>"

    monkeypatch.setenv("FRAPPE_BASE_URL", "https://frappe.example.test")
    monkeypatch.setattr(gateway, "urlopen", lambda request, timeout: FakeResponse())

    try:
        gateway.frappe_request("GET", "/api/method/test")
    except gateway.FrappeError as exc:
        assert "non-JSON response" in str(exc)
    else:
        raise AssertionError("Expected FrappeError")


def test_whatsapp_rejects_missing_profile_template_and_channel(monkeypatch):
    init_db()
    monkeypatch.delenv("WA_CHANNEL_ACCOUNTS_BY_PROFILE_JSON", raising=False)

    result = send_whatsapp_template(
        {
            "profile_key": "missing-profile",
            "phone": "+919999999999",
            "message": "Address",
            "body_values": ["Address"],
            "agent_id": "agent",
            "call_id": "call-missing-profile-config",
            "idempotency_key": "key-missing-profile-config",
        }
    )

    assert result["status"] == "failed"
    assert "template_name is required" in result["error"]
