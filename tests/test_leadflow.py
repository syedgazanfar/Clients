import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from leadflow.app import create_app
from leadflow.config import Settings
from leadflow.db import Database, iso
from leadflow.phone import normalize_phone
from leadflow.providers import AkgProvider, CloudProvider, ConsoleProvider, Inbound, SendError, WahaProvider
from leadflow.service import LeadService, parse_choice, SERVICES

OWNER = "447000000001"
LEAD = "447700900123"
# A Wednesday at 11:00 London time, inside the send window.
MIDDAY = datetime(2026, 11, 18, 11, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "leads.db"),
        admin_token="admin-secret",
        webhook_token="hook-secret",
        owner_whatsapp=OWNER,
        send_gap_seconds=0,
        followup_interval_seconds=0,
        cloud_app_secret="app-secret",
        cloud_verify_token="verify-me",
        akg_url="https://wa.example.test",
        akg_api_key="akg-key",
        akg_session="main",
        akg_webhook_secret="akg-secret",
    )


@pytest.fixture
def provider():
    return ConsoleProvider()


@pytest.fixture
def service(settings, provider):
    return LeadService(settings, Database(settings.db_path), provider)


def to_lead(provider):
    return [m["text"] for m in provider.sent if m["to"] == LEAD]


def to_owner(provider):
    return [m["text"] for m in provider.sent if m["to"] == OWNER]


def say(service, text, phone=LEAD, message_id=None):
    return service.handle_inbound(Inbound(phone=phone, text=text, provider_id=message_id))


# Phone ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+44 7700 900123", "447700900123"),
        ("07700 900123", "447700900123"),
        ("0044 7700 900123", "447700900123"),
        ("447700900123@c.us", "447700900123"),
        ("+91 98765 43210", "919876543210"),
        ("7700900123", "447700900123"),
        ("123", None),
        ("", None),
        ("not a number", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw, "44") == expected


def test_parse_choice():
    assert parse_choice("2", SERVICES) == SERVICES["2"]
    assert parse_choice("3.", SERVICES) == SERVICES["3"]
    assert parse_choice("chatbots", SERVICES) == SERVICES["1"]
    assert parse_choice("something else entirely", SERVICES) == "something else entirely"


# Form capture ---------------------------------------------------------------


def test_form_lead_with_consent_gets_welcome_and_owner_is_notified(service, provider):
    lead = service.capture_lead(phone="07700 900123", name="Ada Lovelace", consent=True)
    assert lead["phone"] == LEAD
    assert lead["stage"] == "ask_company"  # name already known, so it is skipped
    assert len(to_lead(provider)) == 1
    assert "Hi Ada," in to_lead(provider)[0]
    assert "STOP" in to_lead(provider)[0]
    assert provider.sent[-1]["kind"] == "welcome"
    assert "New website lead" in to_owner(provider)[0]


def test_form_lead_without_consent_is_never_messaged(service, provider):
    lead = service.capture_lead(phone=LEAD, name="Ada", consent=False)
    assert lead["status"] == "prospect"
    assert to_lead(provider) == []
    assert service.run_followups(MIDDAY + timedelta(days=30)) == 0
    assert to_lead(provider) == []


def test_duplicate_form_submission_does_not_resend_welcome(service, provider):
    service.capture_lead(phone=LEAD, name="Ada", consent=True)
    lead = service.capture_lead(phone=LEAD, name="Ada", email="ada@example.com", consent=True)
    assert lead["email"] == "ada@example.com"
    assert len(to_lead(provider)) == 1


def test_invalid_phone_rejected(service):
    with pytest.raises(ValueError):
        service.capture_lead(phone="12", name="Ada", consent=True)


# Conversation ---------------------------------------------------------------


def test_full_qualification_from_whatsapp(service, provider):
    say(service, "Hello")
    assert "what is your name" in to_lead(provider)[-1]
    say(service, "Ada Lovelace")
    assert "company" in to_lead(provider)[-1]
    say(service, "Analytical Engines Ltd")
    assert "Reply with a number" in to_lead(provider)[-1]
    say(service, "1")
    assert "hoping AI can do" in to_lead(provider)[-1]
    say(service, "We want a support chatbot on our website and WhatsApp for customers")
    say(service, "1")

    lead = service.db.get_lead_by_phone(LEAD)
    assert lead["stage"] == "done"
    assert lead["name"] == "Ada Lovelace"
    assert lead["service"] == SERVICES["1"]
    assert lead["timeline"] == "As soon as possible"
    assert lead["score"] >= 60
    assert "book a free 30-minute call" in to_lead(provider)[-1]
    assert any(text.startswith("HOT lead") for text in to_owner(provider))


def test_substantive_first_message_is_kept_as_the_need(service, provider):
    say(service, "Hi, we need an AI assistant integrated with our HubSpot CRM")
    lead = service.db.get_lead_by_phone(LEAD)
    assert "HubSpot" in lead["challenge"]
    assert lead["stage"] == "ask_name"


def test_messages_after_qualification_go_to_owner_without_auto_reply(service, provider):
    service.capture_lead(
        phone=LEAD, name="Ada", company="AE", service="1", message="x" * 50, consent=True
    )
    say(service, "2")
    assert service.db.get_lead_by_phone(LEAD)["stage"] == "done"
    before = len(to_lead(provider))
    say(service, "Can we talk tomorrow at 10?")
    assert len(to_lead(provider)) == before
    assert "tomorrow at 10" in to_owner(provider)[-1]


def test_stop_opts_out_and_blocks_all_further_messages(service, provider):
    service.capture_lead(phone=LEAD, name="Ada", consent=True)
    say(service, "stop")
    lead = service.db.get_lead_by_phone(LEAD)
    assert lead["opted_out"] == 1
    assert "unsubscribed" in to_lead(provider)[-1]
    count = len(to_lead(provider))

    say(service, "hello?")
    service.capture_lead(phone=LEAD, name="Ada", consent=True)
    assert service.run_followups(MIDDAY + timedelta(days=30)) == 0
    assert len(to_lead(provider)) == count

    say(service, "START")
    assert service.db.get_lead_by_phone(LEAD)["opted_out"] == 0
    assert len(to_lead(provider)) == count + 1


def test_asking_for_a_human_hands_off(service, provider):
    say(service, "Hello")
    say(service, "human")
    lead = service.db.get_lead_by_phone(LEAD)
    assert lead["status"] == "handoff"
    assert "asked for a person" in to_owner(provider)[-1]
    count = len(to_lead(provider))
    say(service, "are you there")
    assert len(to_lead(provider)) == count


def test_duplicate_webhook_delivery_is_ignored(service, provider):
    say(service, "Hello", message_id="wamid.1")
    count = len(provider.sent)
    assert say(service, "Hello", message_id="wamid.1") is None
    assert len(provider.sent) == count


def test_failed_send_is_recorded_not_raised(service, provider):
    def boom(*args, **kwargs):
        raise SendError("gateway down")

    provider.send = boom
    lead = service.capture_lead(phone=LEAD, name="Ada", consent=True)
    messages = service.db.list_messages(lead["id"])
    assert [m["status"] for m in messages] == ["failed"]
    assert lead["last_outbound_at"] is None


# Follow-ups -----------------------------------------------------------------


def backdate(service, hours, now=MIDDAY):
    lead = service.db.get_lead_by_phone(LEAD)
    service.db.update_lead(lead["id"], last_outbound_at=iso(now - timedelta(hours=hours)))


def test_followup_sequence_then_cold(service, provider):
    service.capture_lead(phone=LEAD, name="Ada", consent=True)
    backdate(service, 2)
    assert service.run_followups(MIDDAY) == 0  # too soon

    backdate(service, 25)
    assert service.run_followups(MIDDAY) == 1
    assert "checking in" in to_lead(provider)[-1]

    backdate(service, 73)
    assert service.run_followups(MIDDAY) == 1
    assert "last note" in to_lead(provider)[-1]

    backdate(service, 200)
    assert service.run_followups(MIDDAY) == 0
    assert service.db.get_lead_by_phone(LEAD)["status"] == "cold"


def test_no_followup_after_reply_or_outside_window(service, provider):
    service.capture_lead(phone=LEAD, name="Ada", consent=True)
    backdate(service, 25)
    assert service.run_followups(datetime(2026, 11, 18, 23, 0, tzinfo=timezone.utc)) == 0
    assert service.run_followups(datetime(2026, 11, 21, 11, 0, tzinfo=timezone.utc)) == 0  # Saturday

    say(service, "human")
    backdate(service, 25)
    assert service.run_followups(MIDDAY) == 0


# Providers ------------------------------------------------------------------


def test_waha_parse_filters_noise():
    def event(**payload):
        return {"event": "message", "payload": payload}

    good = event(id="abc", fromMe=False, body=" hi ", **{"from": f"{LEAD}@c.us"})
    assert WahaProvider.parse(good) == [Inbound(phone=LEAD, text="hi", provider_id="abc")]
    assert WahaProvider.parse(event(fromMe=True, body="hi", **{"from": f"{LEAD}@c.us"})) == []
    assert WahaProvider.parse(event(body="hi", **{"from": "1203630@g.us"})) == []
    assert WahaProvider.parse(event(body="hi", **{"from": "status@broadcast"})) == []
    assert WahaProvider.parse({"event": "session.status", "payload": {}}) == []


def test_cloud_parse_and_template_body(settings):
    cloud = CloudProvider(settings)
    payload = {
        "entry": [{"changes": [{"value": {"messages": [
            {"from": LEAD, "id": "wamid.9", "type": "text", "text": {"body": "hi"}},
            {"from": LEAD, "id": "wamid.10", "type": "image"},
        ]}}]}]
    }
    assert CloudProvider.parse(payload) == [Inbound(phone=LEAD, text="hi", provider_id="wamid.9")]

    assert cloud.build_body(LEAD, "hello", "welcome", "Ada")["type"] == "text"
    settings.cloud_welcome_template = "lead_welcome"
    body = cloud.build_body(LEAD, "hello", "welcome", "Ada")
    assert body["type"] == "template"
    assert body["template"]["name"] == "lead_welcome"
    assert cloud.build_body(LEAD, "hello", "reply", "Ada")["type"] == "text"


def akg_event(**overrides):
    data = {
        "key": {"remoteJid": f"{LEAD}@s.whatsapp.net", "fromMe": False, "id": "3EB0A1"},
        "from": f"{LEAD}@s.whatsapp.net",
        "sender": "100429287395370@lid",
        "type": "TEXT",
        "content": " Hello ",
        "isGroup": False,
    }
    data.update(overrides)
    return {"event": "message.received", "sessionId": "main", "data": data}


def test_akg_parse():
    assert AkgProvider.parse(akg_event(), "main") == [
        Inbound(phone=LEAD, text="Hello", provider_id="3EB0A1")
    ]
    assert AkgProvider.parse(akg_event(), "other-session") == []
    assert AkgProvider.parse(akg_event(isGroup=True), "main") == []
    assert AkgProvider.parse(akg_event(type="IMAGE"), "main") == []
    assert AkgProvider.parse(akg_event(key={"fromMe": True, "id": "x"}), "main") == []
    assert AkgProvider.parse({**akg_event(), "event": "message.sent"}, "main") == []
    # Privacy id in "from": fall back to the phone JID in the key.
    assert AkgProvider.parse(akg_event(**{"from": "1004@lid"}), "main")[0].phone == LEAD
    lid_only = akg_event(**{"from": "1004@lid"}, key={"remoteJid": "1004@lid", "id": "x"})
    assert AkgProvider.parse(lid_only, "main") == []


def test_akg_send_request(settings):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": True, "data": {"key": {"id": "OUT1"}}})

    akg = AkgProvider(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    assert akg.send(LEAD, "hi") == "OUT1"
    assert seen["url"] == f"https://wa.example.test/api/messages/main/{LEAD}@s.whatsapp.net/send"
    assert seen["key"] == "akg-key"
    assert seen["body"] == {"message": {"text": "hi"}}

    down = AkgProvider(
        settings, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    )
    with pytest.raises(SendError):
        down.send(LEAD, "hi")


# HTTP -----------------------------------------------------------------------


@pytest.fixture
def client(settings, provider):
    with TestClient(create_app(settings, provider)) as test_client:
        yield test_client


def test_api_lead_submission(client, provider):
    response = client.post(
        "/api/leads", json={"name": "Ada", "phone": "+44 7700 900123", "consent": True}
    )
    assert response.status_code == 200
    assert len(to_lead(provider)) == 1
    assert client.post("/api/leads", json={"name": "Ada", "phone": "1"}).status_code == 422


def test_api_honeypot_stores_nothing(client, provider):
    response = client.post(
        "/api/leads",
        json={"name": "Bot", "phone": LEAD, "consent": True, "website": "http://spam"},
    )
    assert response.status_code == 200
    assert provider.sent == []


def test_api_rate_limit(client):
    for index in range(5):
        body = {"name": "Ada", "phone": f"+4477009001{index:02d}", "consent": True}
        assert client.post("/api/leads", json=body).status_code == 200
    assert client.post("/api/leads", json=body).status_code == 429


def test_cors_only_allows_site_origin(client):
    headers = {"Access-Control-Request-Method": "POST"}
    ok = client.options("/api/leads", headers={**headers, "Origin": "https://heystartup.co.uk"})
    assert ok.headers["access-control-allow-origin"] == "https://heystartup.co.uk"
    bad = client.options("/api/leads", headers={**headers, "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers


def test_waha_webhook_requires_token(client, provider):
    payload = {"event": "message", "payload": {"from": f"{LEAD}@c.us", "body": "Hello", "id": "m1"}}
    assert client.post("/webhooks/waha", json=payload).status_code == 401
    assert client.post("/webhooks/waha?token=wrong", json=payload).status_code == 401
    response = client.post("/webhooks/waha", json=payload, headers={"X-Webhook-Token": "hook-secret"})
    assert response.json() == {"ok": True, "handled": 1}
    assert len(to_lead(provider)) == 1


def test_cloud_webhook_verification_and_signature(client, provider):
    verify = client.get(
        "/webhooks/cloud",
        params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"},
    )
    assert verify.text == "42"
    assert client.get("/webhooks/cloud", params={"hub.mode": "subscribe"}).status_code == 403

    raw = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": LEAD, "id": "wamid.1", "type": "text", "text": {"body": "Hello"}}
    ]}}]}]}).encode()
    headers = {"Content-Type": "application/json"}
    assert client.post("/webhooks/cloud", content=raw, headers=headers).status_code == 401
    signature = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhooks/cloud", content=raw, headers={**headers, "X-Hub-Signature-256": signature}
    )
    assert response.json()["handled"] == 1


def test_admin_requires_password_and_escapes_html(client, service):
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", auth=("admin", "wrong")).status_code == 401
    client.post(
        "/api/leads",
        json={"name": "<script>alert(1)</script>", "phone": LEAD, "consent": True},
    )
    page = client.get("/admin", auth=("admin", "admin-secret"))
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert client.get("/admin/leads/1", auth=("admin", "admin-secret")).status_code == 200
    csv_export = client.get("/admin/leads.csv", auth=("admin", "admin-secret"))
    assert LEAD in csv_export.text


def test_widget_served(client):
    response = client.get("/widget.js")
    assert response.status_code == 200
    assert "/api/leads" in response.text


def test_akg_webhook_requires_valid_signature(client, provider):
    raw = json.dumps(akg_event()).encode()
    headers = {"Content-Type": "application/json"}
    assert client.post("/webhooks/akg", content=raw, headers=headers).status_code == 401
    bad = {**headers, "X-Webhook-Signature": "sha256=" + "0" * 64}
    assert client.post("/webhooks/akg", content=raw, headers=bad).status_code == 401
    signature = "sha256=" + hmac.new(b"akg-secret", raw, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhooks/akg", content=raw, headers={**headers, "X-Webhook-Signature": signature}
    )
    assert response.json() == {"ok": True, "handled": 1}
    assert "what is your name" in to_lead(provider)[-1]
