"""Telnyx: signed TeXML webhooks, the media stream and the dashboard connector."""

import asyncio
import base64
import json
import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from openring import keys, telnyx
from openring.api import create_app
from openring.bot import keep_phone_line_open, telnyx_transport
from openring.config import Settings
from openring.providers import provider_status
from openring.store import Store

from test_api import ASSISTANT, AUTH

PRIVATE = Ed25519PrivateKey.generate()
PUBLIC = base64.b64encode(PRIVATE.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def sign(body: bytes, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(time.time()) if timestamp is None else timestamp)
    signature = base64.b64encode(PRIVATE.sign(ts.encode() + b"|" + body)).decode()
    return {"telnyx-timestamp": ts, "telnyx-signature-ed25519": signature}


def test_signature_check():
    body = b"From=%2B5511&To=%2B15551234567"
    headers = sign(body)
    ts, sig = headers["telnyx-timestamp"], headers["telnyx-signature-ed25519"]
    assert telnyx.is_valid_signature(PUBLIC, ts, body, sig)
    assert not telnyx.is_valid_signature(PUBLIC, ts, body + b"&x=1", sig)  # body changed
    old = sign(body, timestamp=int(time.time()) - 3600)
    assert not telnyx.is_valid_signature(PUBLIC, old["telnyx-timestamp"], body, old["telnyx-signature-ed25519"])
    other = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw)).decode()
    assert not telnyx.is_valid_signature(other, ts, body, sig)
    assert not telnyx.is_valid_signature(PUBLIC, "", body, sig)
    assert not telnyx.is_valid_signature("not base64!", ts, body, "nope")


def test_stream_texml():
    root = ET.fromstring(telnyx.stream_texml("wss://x.test/ws/telnyx?callId=c1&a=b"))
    stream = root.find("./Connect/Stream")
    assert stream.get("url") == "wss://x.test/ws/telnyx?callId=c1&a=b" and stream.get("bidirectionalMode") == "rtp"


def app(monkeypatch, **overrides):
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(**{"api_key": "test-key", "public_url": "https://x.test", "db_path": ":memory:",
                           "telnyx_public_key": PUBLIC, "telnyx_api_key": "KEY123", **overrides})
    client = TestClient(create_app(settings, store))
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"], "provider": "telnyx"},
                headers=AUTH)
    return client, store, a


FORM = {"CallSid": "v3:abc", "From": "+5511999990000", "To": "+15551234567", "Direction": "inbound"}


def ring(client, form=FORM, headers=None, sign_it=True):
    body = urlencode(form).encode()
    h = {"content-type": "application/x-www-form-urlencoded", **(sign(body) if sign_it else {}), **(headers or {})}
    return client.post("/telnyx/voice", content=body, headers=h)


def test_incoming_call_streams_to_its_assistant(monkeypatch):
    client, store, a = app(monkeypatch)
    r = ring(client)
    assert r.status_code == 200
    stream = ET.fromstring(r.text).find("./Connect/Stream")
    url = urlsplit(stream.get("url"))
    assert (url.scheme, url.netloc, url.path) == ("wss", "x.test", "/ws/telnyx")
    call = store.get("calls", parse_qs(url.query)["callId"][0])
    assert (call["type"], call["status"], call["assistantId"]) == ("inboundPhoneCall", "queued", a["id"])
    assert (call["phoneCallProvider"], call["phoneCallProviderId"]) == ("telnyx", "v3:abc")
    assert call["customer"] == {"number": "+5511999990000"}


def test_json_webhooks_work_too(monkeypatch):
    client, store, _ = app(monkeypatch)
    body = json.dumps(FORM).encode()
    r = client.post("/telnyx/voice", content=body, headers={"content-type": "application/json", **sign(body)})
    assert r.status_code == 200 and ET.fromstring(r.text).find("./Connect/Stream") is not None


def test_unsigned_or_unverifiable_webhooks_are_refused(monkeypatch):
    client, store, _ = app(monkeypatch)
    assert ring(client, sign_it=False).status_code == 403
    assert ring(client, headers={"telnyx-signature-ed25519": base64.b64encode(b"x" * 64).decode()}).status_code == 403
    no_key, _, _ = app(monkeypatch, telnyx_public_key="")
    assert ring(no_key).status_code == 403
    assert store.list("calls") == []
    trusting, _, _ = app(monkeypatch, telnyx_public_key="", validate_telnyx_signature=False)
    assert ring(trusting, sign_it=False).status_code == 200


def test_number_without_assistant_says_so(monkeypatch):
    client, _, _ = app(monkeypatch)
    r = ring(client, form={**FORM, "To": "+19990000000"})
    assert ET.fromstring(r.text).find("./Say") is not None


@pytest.fixture
def fake_bot(monkeypatch):
    runs = []

    async def run_call(websocket, *, plan, call_id, **_):
        runs.append((call_id, plan))
        await websocket.close()

    monkeypatch.setattr("openring.bot.telnyx_transport", lambda websocket, *_: websocket)
    monkeypatch.setattr("openring.bot.run_call", run_call)
    return runs


def _connect(client, call_id):
    """Open a media stream the way Telnyx does and return the code the server closes it with."""
    with client.websocket_connect(f"/ws/telnyx?callId={call_id}") as ws:
        ws.send_text(json.dumps({"event": "connected", "version": "1.0.0"}))
        ws.send_text(json.dumps({"event": "start", "sequence_number": "1", "stream_id": "st-1", "start": {
            "call_control_id": "v3:abc", "from": FORM["From"], "to": FORM["To"],
            "media_format": {"encoding": "PCMU", "sample_rate": 8000, "channels": 1}}}))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    return closed.value.code


def test_media_stream_runs_the_calls_assistant_once(monkeypatch, fake_bot):
    client, store, _ = app(monkeypatch)
    call_id = parse_qs(urlsplit(ET.fromstring(ring(client).text).find("./Connect/Stream").get("url")).query)[
        "callId"][0]
    assert _connect(client, call_id) == 1000
    assert _connect(client, call_id) == 1008  # a second connection for the same call
    assert _connect(client, "made-up") == 1008
    assert [(c, p.first_message) for c, p in fake_bot] == [(call_id, ASSISTANT["firstMessage"])]
    assert store.get("calls", call_id)["status"] == "in-progress"


def test_transport_hangs_up_through_telnyx_unless_transferred(monkeypatch):
    from pipecat.frames.frames import EndFrame

    settings = Settings(telnyx_api_key="KEY123", db_path=":memory:")
    call_data = {"stream_id": "st-1", "call_id": "v3:abc", "outbound_encoding": "PCMU"}
    transport = telnyx_transport(SimpleNamespace(), call_data, settings)
    serializer = transport._params.serializer
    hung_up = []

    async def hang_up():
        hung_up.append(True)

    monkeypatch.setattr(serializer, "_hang_up_call", hang_up)
    asyncio.run(serializer.serialize(EndFrame()))
    assert hung_up == [True]
    transport = telnyx_transport(SimpleNamespace(), call_data, settings)
    keep_phone_line_open(transport)
    monkeypatch.setattr(transport._params.serializer, "_hang_up_call", hang_up)
    asyncio.run(transport._params.serializer.serialize(EndFrame()))
    assert hung_up == [True]  # not hung up again
    # Without an API key OpenRing cannot hang up, so it doesn't try.
    telnyx_transport(SimpleNamespace(), call_data, Settings(db_path=":memory:"))


def test_dashboard_connects_telnyx(monkeypatch, tmp_path):
    assert keys.CONNECTORS["telnyx"]["fields"] == [("TELNYX_API_KEY", "API key"), ("TELNYX_PUBLIC_KEY", "Public key")]
    url, options = keys._check_request("telnyx", {"TELNYX_API_KEY": "KEY123", "TELNYX_PUBLIC_KEY": PUBLIC})
    assert url == "https://api.telnyx.com/v2/balance" and options["headers"]["Authorization"] == "Bearer KEY123"

    rows = {r["id"]: r for r in provider_status(Settings(db_path=":memory:", public_url="https://x.test",
                                                          telnyx_api_key="KEY123", telnyx_public_key=PUBLIC))}
    assert rows["telnyx"]["kind"] == "telephony" and rows["telnyx"]["ready"] is True
    assert provider_status(Settings(db_path=":memory:"))[-1]["ready"] is False


def _dial(client, number="+5511912345678"):
    n = client.get("/phone-number", headers=AUTH).json()[0]
    return client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": number}}, headers=AUTH)


@pytest.fixture
def placed(monkeypatch):
    calls = []

    async def create_call(api_key, application_id, *, to, from_, url, status_callback):
        calls.append({"key": api_key, "application": application_id, "to": to, "from": from_, "url": url,
                      "status_callback": status_callback})
        return "v3:out-1"

    monkeypatch.setattr(telnyx, "create_call", create_call)
    return calls


def test_an_outgoing_call_goes_through_a_texml_application_and_is_answered(monkeypatch, placed):
    client, store, a = app(monkeypatch, telnyx_application_id="app-1")
    r = _dial(client, "+55 11 91234-5678")
    assert r.status_code == 201, r.text
    call = r.json()
    assert (call["type"], call["status"], call["phoneCallProvider"], call["phoneCallProviderId"]) == (
        "outboundPhoneCall", "queued", "telnyx", "v3:out-1")
    assert placed == [{"key": "KEY123", "application": "app-1", "to": "+5511912345678", "from": "+15551234567",
                       "url": f"https://x.test/telnyx/voice?callId={call['id']}",
                       "status_callback": f"https://x.test/telnyx/status?callId={call['id']}"}]

    def fetched(call_id):
        body = urlencode({"CallSid": "v3:out-1", "To": "+5511912345678"}).encode()
        return client.post(f"/telnyx/voice?callId={call_id}", content=body, headers={
            "content-type": "application/x-www-form-urlencoded", **sign(body)})

    stream = ET.fromstring(fetched(call["id"]).text).find("./Connect/Stream")
    assert stream.get("url") == f"wss://x.test/ws/telnyx?callId={call['id']}"
    assert ET.fromstring(fetched("made-up").text).find("./Connect/Stream") is None
    store.update("calls", call["id"], {"status": "in-progress"})  # already connected: nothing to connect again
    assert ET.fromstring(fetched(call["id"]).text).find("./Connect/Stream") is None
    body = urlencode({"CallSid": "v3:out-1"}).encode()
    unsigned = client.post(f"/telnyx/voice?callId={call['id']}", content=body, headers={
        "content-type": "application/x-www-form-urlencoded"})
    assert unsigned.status_code == 403


def test_the_number_can_name_its_own_application(monkeypatch, placed):
    client, _, _ = app(monkeypatch, telnyx_application_id="app-1")
    n = client.get("/phone-number", headers=AUTH).json()[0]
    client.patch(f"/phone-number/{n['id']}", json={"texmlApplicationId": "app-of-this-number"}, headers=AUTH)
    assert _dial(client).status_code == 201
    assert placed[0]["application"] == "app-of-this-number"


def test_an_outgoing_call_that_nobody_answered_ends_with_the_reason(monkeypatch, placed):
    client, store, _ = app(monkeypatch, telnyx_application_id="app-1")
    call = _dial(client).json()
    body = urlencode({"CallStatus": "no-answer"}).encode()
    r = client.post(f"/telnyx/status?callId={call['id']}", content=body, headers={
        "content-type": "application/x-www-form-urlencoded", **sign(body)})
    assert r.status_code == 204
    ended = store.get("calls", call["id"])
    assert (ended["status"], ended["endedReason"]) == ("ended", "customer-did-not-answer")
    other = _dial(client, "+5511912345679").json()
    store.update("calls", other["id"], {"status": "in-progress"})  # it connected: the bot ends it, not this hook
    body = urlencode({"CallStatus": "completed"}).encode()
    client.post(f"/telnyx/status?callId={other['id']}", content=body, headers={
        "content-type": "application/x-www-form-urlencoded", **sign(body)})
    assert store.get("calls", other["id"])["status"] == "in-progress"


def test_outgoing_calls_need_the_key_an_application_and_a_phone_number(monkeypatch, placed):
    client, _, _ = app(monkeypatch)  # no application anywhere
    r = _dial(client)
    assert r.status_code == 400 and "TELNYX_APPLICATION_ID" in r.json()["detail"]
    client, _, _ = app(monkeypatch, telnyx_application_id="app-1", public_url="")
    assert "PUBLIC_URL" in _dial(client).json()["detail"]
    client, _, _ = app(monkeypatch, telnyx_application_id="app-1")
    n = client.get("/phone-number", headers=AUTH).json()[0]
    r = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"sipUri": "sip:a@b.example"}},
                    headers=AUTH)
    assert r.status_code == 400 and "customer.number" in r.json()["detail"]
    assert placed == []


def test_a_refusal_from_telnyx_is_recorded_and_explained(monkeypatch):
    client, store, _ = app(monkeypatch, telnyx_application_id="app-1")

    async def refuse(*_a, **_k):
        raise telnyx.TelnyxError("Caller ID not verified (Telnyx error 422)")

    monkeypatch.setattr(telnyx, "create_call", refuse)
    r = _dial(client)
    assert r.status_code == 400 and "Telnyx did not place the call" in r.json()["detail"]
    assert store.list("calls")[0]["endedReason"] == "telnyx-failed-to-connect-call"


def test_telnyxs_api_call(monkeypatch):
    import httpx

    seen = []

    def handler(request):
        seen.append((request.method, str(request.url), request.headers["authorization"], parse_qs(
            request.content.decode())))
        if request.headers["authorization"] == "Bearer bad":
            return httpx.Response(401, json={"errors": [{"title": "Unauthorized", "detail": "Invalid key"}]})
        return httpx.Response(200, json={"data": {"call_sid": "v3:77", "status": "queued"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    sid = asyncio.run(telnyx.create_call("KEY123", "app-1", to="+5511", from_="+1555", url="https://x/v",
                                         status_callback="https://x/s"))
    assert sid == "v3:77"
    method, url, auth, form = seen[0]
    assert (method, url, auth) == ("POST", "https://api.telnyx.com/v2/texml/calls/app-1", "Bearer KEY123")
    assert form == {"To": ["+5511"], "From": ["+1555"], "Url": ["https://x/v"], "UrlMethod": ["POST"],
                    "StatusCallback": ["https://x/s"], "StatusCallbackMethod": ["POST"]}
    with pytest.raises(telnyx.TelnyxError, match="Invalid key"):
        asyncio.run(telnyx.create_call("bad", "app-1", to="x", from_="y", url="u", status_callback="s"))
