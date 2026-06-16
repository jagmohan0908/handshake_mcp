# Handshake MCP Gateway

Handshake MCP is a guarded domain gateway between AI agents and Frappe.

Agents call this service through JSON-RPC `tools/call`. This service owns Frappe credentials, idempotency, per-call limits, audit logs, low-concurrency writes, and circuit breaker behavior.

## Tools

- `send_whatsapp_template`

The gateway intentionally exposes only the WhatsApp template sending tool instead of broad Frappe CRUD write access.

## Run Locally

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m app.main
```

Set these in `.env`:

- `MCP_BEARER_TOKEN`
- `FRAPPE_BASE_URL`
- `FRAPPE_AUTHORIZATION`
- `WA_CHANNEL_ACCOUNTS_BY_PROFILE_JSON`
- `WA_CHANNEL_ACCOUNTS_BY_DID_JSON`

## Agent Endpoint

Use:

```text
POST /mcp
Authorization: Bearer <MCP_BEARER_TOKEN>
```

Example:

```json
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "tools/call",
  "params": {
    "name": "send_whatsapp_template",
    "arguments": {
      "phone": "+919999999999",
      "profile_key": "male-kamal-sriaas",
      "did_number": "+919262171487",
      "message": "Clinic address: B-92, near Millennium City Centre Metro Station, Gurugram.",
      "language_code": "en",
      "body_values": ["Clinic address: B-92, near Millennium City Centre Metro Station, Gurugram."],
      "agent_id": "vobiz-gemini-live",
      "call_id": "livekit-room-name",
      "idempotency_key": "vobiz-gemini-live:livekit-room-name:whatsapp:hash"
    }
  }
}
```

## Safety Defaults

- WhatsApp sends: 1 per `agent_id + call_id`
- Total Frappe calls: 10 per `agent_id + call_id`
- Idempotency TTL: 24 hours
- Frappe write concurrency: 2

## WhatsApp Behavior

The gateway sends approved templates only. It does not send direct/free-text WhatsApp messages.

Template body assumption:

```text
Hi, I am from SRIAAS {{1}}
```

The `template_name` comes from the matching profile config, and the agent's generated text is passed as the first body variable.

To route WhatsApp sends by voice profile, set both the WhatsApp channel account and template in `WA_CHANNEL_ACCOUNTS_BY_PROFILE_JSON`:

```text
WA_CHANNEL_ACCOUNTS_BY_PROFILE_JSON={"male-kamal-sriaas":{"channel_account":"Interakt SRIAAS Male","template_name":"vobiz_ai","language_code":"en"},"female-megha-sriaas":{"channel_account":"Interakt SRIAAS Female","template_name":"vobiz_ai_female","language_code":"en"}}
```

To force routing by the called DID/phone number, set `WA_CHANNEL_ACCOUNTS_BY_DID_JSON`. DID mapping wins over profile mapping:

```text
WA_CHANNEL_ACCOUNTS_BY_DID_JSON={"+919262171462":{"profile_key":"siya-agent","channel_account":"siya-interakt","template_name":"vobiz_siya","language_code":"en"},"+919262171487":{"profile_key":"seedfit-agent","channel_account":"seedfit-interakt","template_name":"vobiz_seedfit_pg","language_code":"en"}}
```

When `did_number` matches `WA_CHANNEL_ACCOUNTS_BY_DID_JSON`, that DID mapping wins for `profile_key`, `channel_account`, and `template_name`. Otherwise, when `profile_key` matches `WA_CHANNEL_ACCOUNTS_BY_PROFILE_JSON`, that profile mapping wins for `channel_account` and `template_name`. Request values are only used as fallback when the matching config does not define that field. Without a mapped or explicit channel/template, the send is rejected.

If `did_number` is included, MCP first asks Frappe's voice-agent config API to resolve the true profile for that DID, then uses that resolved profile for WhatsApp routing.
