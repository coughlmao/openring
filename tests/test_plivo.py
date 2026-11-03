"""Plivo: signed webhooks, the audio stream, outgoing calls and transfers."""

import asyncio
import base64
import hashlib
import hmac
import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from openring import keys, plivo
from openring.api import create_app
from openring.bot import plivo_transport
from openring.config import Settings
from openring.providers import provider_status
from openring.store import Store

from test_api import ASSISTANT, AUTH

TOKEN = "plivo-token-123"
FORM = {"CallUUID": "uuid-1", "From": "5511999990000", "To": "15551234567", "CallStatus": "in-progress"}


def reference(token, url, nonce, fields):
    """What Plivo's SDKs sign, written out by hand for a POST: the URL, a `.` after its query when fields follow,
    the fields as name and value sorted by name, a `.` and the nonce."""
    text = url
    if "?" not in text:
        text += "?"
    elif fields:
        text += "."
    text += "".join(k + fields[k] for k in sorted(fields)) + "." + nonce
    return base64.b64encode(hmac.new(token.encode(), text.encode(), hashlib.sha256).digest()).decode()


def test_the_string_plivo_signs():
    url = "https://x.test/plivo/voice"
    assert plivo.signed_string("POST", url, {"To": "1", "From": "2"}) == url + "?From2To1"
    assert plivo.signed_string("POST", url + "?callId=c1", {"To": "1", "From": "2"}) == url + "?callId=c1.From2To1"
    assert plivo.signed_string("POST", url, {}) == url
    assert plivo.signed_string("POST", url + "?b=2&a=1", {}) == url + "?a=1&b=2"
    assert plivo.signed_string("GET", url + "?b=2&a=1", {}) == url + "?a=1&b=2"
    assert plivo.signed_string("GET", url, {}) == url


@pytest.mark.parametrize("query", ["", "?callId=c1"])
def test_signature_matches_the_reference(query):
    url = "https://x.test/plivo/voice" + query
    signature = reference(TOKEN, url, "n-1", FORM)
    assert plivo.compute_signature(TOKEN, "POST", url, FORM, "n-1") == signature
    assert plivo.is_valid_signature(TOKEN, "POST", url, FORM, "n-1", signature)
    assert plivo.is_valid_signature(TOKEN, "POST", url, FORM, "n-1", f"bogus, {signature}")  # one per token
    assert not plivo.is_valid_signature(TOKEN, "POST", url, {**FORM, "To": "1"}, "n-1", signature)
    assert not plivo.is_valid_signature(TOKEN, "POST", url, FORM, "n-2", signature)
    assert not plivo.is_valid_signature("another", "POST", url, FORM, "n-1", signature)
    assert not plivo.is_valid_signature(TOKEN, "POST", url, FORM, "", signature)
    assert not plivo.is_valid_signature(TOKEN, "POST", url, FORM, "n-1", "")
    assert not plivo.is_valid_signature("", "POST", url, FORM, "n-1", signature)


def app(monkeypatch, **overrides):
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(**{"api_key": "test-key", "public_url": "https://x.test", "db_path": ":memory:",
                           "plivo_auth_id": "MAXXXXXXXXXXXXXXXXXX", "plivo_auth_token": TOKEN, **overrides})
    client = TestClient(create_app(settings, store))
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    n = client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"], "provider": "plivo"},
                    headers=AUTH).json()
    return client, store, a, n


def hook(client, path, form=FORM, nonce="n-1", sign_it=True, token=TOKEN):
    url = "https://x.test" + path
    headers = {"X-Plivo-Signature-V3": reference(token, url, nonce, form), "X-Plivo-Signature-V3-Nonce": nonce} \
        if sign_it else {}
    return client.post(path, data=form, headers=headers)


def test_incoming_call_streams_to_its_assistant(monkeypatch):
    client, store, a, _ = app(monkeypatch)
    r = hook(client, "/plivo/voice")
    assert r.status_code == 200
    stream = ET.fromstring(r.text).find("./Stream")
    assert stream.get("bidirectional") == "true" and stream.get("contentType") == "audio/x-mulaw;rate=8000"
    url = urlsplit(stream.text)
    assert (url.scheme, url.netloc, url.path) == ("wss", "x.test", "/ws/plivo")
    call = store.get("calls", parse_qs(url.query)["callId"][0])
    assert (call["type"], call["status"], call["assistantId"]) == ("inboundPhoneCall", "queued", a["id"])
    assert (call["phoneCallProvider"], call["phoneCallProviderId"]) == ("plivo", "uuid-1")
    assert call["customer"] == {"number": "5511999990000"}


def test_a_get_webhook_is_signed_by_its_url(monkeypatch):
    client, store, _, _ = app(monkeypatch)
    path = "/plivo/voice?From=5511999990000&To=15551234567&CallUUID=uuid-1"
    signature = reference(TOKEN, "https://x.test" + path.replace("?", "?") + "", {}, "n-1") if False else \
        plivo.compute_signature(TOKEN, "GET", "https://x.test" + path, {}, "n-1")
    headers = {"X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": "n-1"}
    assert client.get(path, headers=headers).status_code == 200
    assert client.get(path.replace("5511", "5522"), headers=headers).status_code == 403
    assert client.get(path).status_code == 403


def test_unsigned_or_unverifiable_webhooks_are_refused(monkeypatch):
    client, store, _, _ = app(monkeypatch)
    assert hook(client, "/plivo/voice", sign_it=False).status_code == 403
    assert hook(client, "/plivo/voice", token="not-the-token").status_code == 403
    assert hook(client, "/plivo/voice", form={**FORM, "To": "1"}, nonce="x", sign_it=True,
                token=TOKEN).status_code == 200  # signed over what was sent
    no_token, store2, _, _ = app(monkeypatch, plivo_auth_token="")
    assert hook(no_token, "/plivo/voice").status_code == 403
    assert store2.list("calls") == []
    trusting, _, _, _ = app(monkeypatch, plivo_auth_token="", validate_plivo_signature=False)
    assert hook(trusting, "/plivo/voice", sign_it=False).status_code == 200


def test_number_without_assistant_says_so(monkeypatch):
    client, _, _, _ = app(monkeypatch)
    r = hook(client, "/plivo/voice", form={**FORM, "To": "19990000000"})
    assert ET.fromstring(r.text).find("./Speak") is not None


@pytest.fixture
def placed(monkeypatch):
    calls = []

    async def create_call(auth_id, auth_token, *, to, from_, answer_url, hangup_url):
        calls.append({"to": to, "from": from_, "answer_url": answer_url, "hangup_url": hangup_url})
        return "req-1"

    monkeypatch.setattr(plivo, "create_call", create_call)
    return calls


def test_an_outgoing_call_is_placed_and_answered_by_its_assistant(monkeypatch, placed):
    client, store, a, n = app(monkeypatch)
    r = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": "+55 11 91234-5678"}},
                    headers=AUTH)
    assert r.status_code == 201, r.text
    call = r.json()
    assert (call["type"], call["status"], call["phoneCallProvider"], call["phoneCallProviderId"]) == (
        "outboundPhoneCall", "queued", "plivo", "req-1")
    assert placed == [{"to": "+5511912345678", "from": "+15551234567",
                       "answer_url": f"https://x.test/plivo/voice?callId={call['id']}",
                       "hangup_url": f"https://x.test/plivo/status?callId={call['id']}"}]
    # Answered: Plivo fetches the call's URL, signed with its query, and gets the stream.
    answered = hook(client, f"/plivo/voice?callId={call['id']}", form={"CallUUID": "uuid-9"})
    stream = ET.fromstring(answered.text).find("./Stream")
    assert parse_qs(urlsplit(stream.text).query)["callId"] == [call["id"]]
    # Not a call that is waiting for its answer: nothing to connect.
    assert ET.fromstring(hook(client, "/plivo/voice?callId=made-up", form={"CallUUID": "x"}).text
                         ).find("./Stream") is None
    store.update("calls", call["id"], {"status": "in-progress"})
    assert ET.fromstring(hook(client, f"/plivo/voice?callId={call['id']}", form={"CallUUID": "x"}).text
                         ).find("./Stream") is None


def test_an_outgoing_call_that_nobody_answered_ends_with_the_reason(monkeypatch, placed):
    client, store, _, n = app(monkeypatch)
    call = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": "+5511912345678"}},
                       headers=AUTH).json()
    r = hook(client, f"/plivo/status?callId={call['id']}", form={"CallUUID": "u", "CallStatus": "busy"})
    assert r.status_code == 204
    ended = store.get("calls", call["id"])
    assert (ended["status"], ended["endedReason"]) == ("ended", "customer-busy")
    other = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": "+5511912345679"}},
                        headers=AUTH).json()
    store.update("calls", other["id"], {"status": "in-progress"})  # it connected: the bot ends it, not this hook
    hook(client, f"/plivo/status?callId={other['id']}", form={"CallUUID": "u", "CallStatus": "completed"})
    assert store.get("calls", other["id"])["status"] == "in-progress"


def test_a_refusal_from_plivo_is_recorded_and_explained(monkeypatch):
    client, store, _, n = app(monkeypatch)

    async def refuse(*_a, **_k):
        raise plivo.PlivoError("caller id not allowed (Plivo error 400)")

    monkeypatch.setattr(plivo, "create_call", refuse)
    r = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": "+5511912345678"}},
                    headers=AUTH)
    assert r.status_code == 400 and "Plivo did not place the call" in r.json()["detail"]
    assert store.list("calls")[0]["endedReason"] == "plivo-failed-to-connect-call"


def test_outgoing_calls_need_the_login_and_a_phone_number(monkeypatch, placed):
    for overrides, missing in (({"plivo_auth_token": ""}, "PLIVO_AUTH_TOKEN"), ({"public_url": ""}, "PUBLIC_URL")):
        client, _, _, n = app(monkeypatch, **overrides)
        r = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": "+5511912345678"}},
                        headers=AUTH)
        assert r.status_code == 400 and missing in r.json()["detail"]
    client, _, _, n = app(monkeypatch)
    r = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"sipUri": "sip:a@b.example"}},
                    headers=AUTH)
    assert r.status_code == 400 and "customer.number" in r.json()["detail"]
    assert placed == []


def test_plivos_api_calls(monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url), request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(201, json={"message": "call fired", "request_uuid": "req-7"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    uuid = asyncio.run(plivo.create_call("MAID", "secret", to="+5511", from_="+1555", answer_url="https://x/a",
                                         hangup_url="https://x/h"))
    asyncio.run(plivo.transfer_call("MAID", "secret", "uuid-1", "https://x/t"))
    assert uuid == "req-7"
    basic = "Basic " + base64.b64encode(b"MAID:secret").decode()
    assert seen[0][:3] == ("POST", "https://api.plivo.com/v1/Account/MAID/Call/", basic)
    assert seen[0][3] == {"from": "+1555", "to": "+5511", "answer_url": "https://x/a", "answer_method": "POST",
                          "hangup_url": "https://x/h", "hangup_method": "POST"}
    assert seen[1][:3] == ("POST", "https://api.plivo.com/v1/Account/MAID/Call/uuid-1/", basic)
    assert seen[1][3] == {"legs": "aleg", "aleg_url": "https://x/t", "aleg_method": "POST"}

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": "invalid number"})), **kw))
    with pytest.raises(plivo.PlivoError, match="invalid number"):
        asyncio.run(plivo.create_call("MAID", "secret", to="x", from_="y", answer_url="a", hangup_url="h"))


@pytest.fixture
def fake_bot(monkeypatch):
    runs = []

    async def run_call(websocket, *, plan, call_id, **kwargs):
        runs.append({"call": call_id, "transfer": kwargs.get("transfer"), "rate": kwargs.get("sample_rate")})
        await websocket.close()

    monkeypatch.setattr("openring.bot.plivo_transport", lambda websocket, *_: websocket)
    monkeypatch.setattr("openring.bot.run_call", run_call)
    return runs


def _connect(client, call_id):
    """Open an audio stream the way Plivo does and return the code the server closes it with."""
    with client.websocket_connect(f"/ws/plivo?callId={call_id}") as ws:
        ws.send_text(json.dumps({"event": "start", "sequenceNumber": 1, "start": {
            "streamId": "st-1", "callId": "uuid-1"}, "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}}))
        # Plivo's first audio follows at once; Pipecat reads two messages to tell the provider.
        ws.send_text(json.dumps({"event": "media", "streamId": "st-1", "media": {
            "track": "inbound", "payload": base64.b64encode(b"\xff" * 160).decode()}}))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    return closed.value.code


def _stream_call_id(client):
    stream = ET.fromstring(hook(client, "/plivo/voice").text).find("./Stream")
    return parse_qs(urlsplit(stream.text).query)["callId"][0]


def test_the_stream_runs_the_calls_assistant_once(monkeypatch, fake_bot):
    client, store, _, _ = app(monkeypatch)
    call_id = _stream_call_id(client)
    assert _connect(client, call_id) == 1000
    assert _connect(client, call_id) == 1008  # a second connection for the same call
    assert _connect(client, "made-up") == 1008
    assert [r["call"] for r in fake_bot] == [call_id] and fake_bot[0]["rate"] == 8000
    assert store.get("calls", call_id)["status"] == "in-progress"


def test_a_transfer_goes_through_a_link_that_works_once(monkeypatch, fake_bot):
    client, _, _, _ = app(monkeypatch)
    moved = []

    async def transfer_call(auth_id, auth_token, call_uuid, answer_url):
        moved.append((call_uuid, answer_url))

    monkeypatch.setattr(plivo, "transfer_call", transfer_call)
    assert _connect(client, _stream_call_id(client)) == 1000
    asyncio.run(fake_bot[0]["transfer"]("+55 11 98888-7777"))
    (call_uuid, link), = moved
    assert call_uuid == "uuid-1" and link.startswith("https://x.test/plivo/transfer/")  # Plivo's id, from its stream
    path = link.removeprefix("https://x.test")
    dialed = ET.fromstring(hook(client, path, form={"CallUUID": "uuid-1"}).text)
    assert dialed.find("./Dial/Number").text == "+5511988887777"
    again = ET.fromstring(hook(client, path, form={"CallUUID": "uuid-1"}).text)
    assert again.find("./Hangup") is not None and again.find("./Dial") is None  # no second use
    assert hook(client, path, form={"CallUUID": "uuid-1"}, sign_it=False).status_code == 403
    assert ET.fromstring(hook(client, "/plivo/transfer/guess", form={"CallUUID": "x"}).text
                         ).find("./Hangup") is not None


def test_transport_hangs_up_through_plivo_only_with_what_it_needs(monkeypatch):
    from pipecat.frames.frames import EndFrame

    settings = Settings(plivo_auth_id="MAID", plivo_auth_token="secret", db_path=":memory:")
    call_data = {"stream_id": "st-1", "call_id": "uuid-1"}
    serializer = plivo_transport(SimpleNamespace(), call_data, settings)._params.serializer
    assert serializer._params.auto_hang_up is True
    hung_up = []

    async def hang_up():
        hung_up.append(True)

    monkeypatch.setattr(serializer, "_hang_up_call", hang_up)
    asyncio.run(serializer.serialize(EndFrame()))  # the assistant ended the call: Plivo is told to hang up
    assert hung_up == [True]
    assert plivo_transport(SimpleNamespace(), call_data, Settings(db_path=":memory:")
                           )._params.serializer._params.auto_hang_up is False


def test_dashboard_connects_plivo():
    assert keys.CONNECTORS["plivo"]["fields"] == [("PLIVO_AUTH_ID", "Auth ID"), ("PLIVO_AUTH_TOKEN", "Auth token")]
    url, options = keys._check_request("plivo", {"PLIVO_AUTH_ID": "MAID", "PLIVO_AUTH_TOKEN": "secret"})
    assert url == "https://api.plivo.com/v1/Account/MAID/" and options["auth"] == ("MAID", "secret")
    settings = Settings(db_path=":memory:", public_url="https://x.test", plivo_auth_id="MAID", plivo_auth_token="s")
    rows = {r["id"]: r for r in provider_status(settings)}
    assert rows["plivo"]["kind"] == "telephony" and rows["plivo"]["ready"] is True
    assert {r["id"]: r for r in provider_status(Settings(db_path=":memory:"))}["plivo"]["ready"] is False

    def handler(request):
        return httpx.Response(200, json={"cash_credits": "12.50"})

    async def read():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await keys.balance("plivo", {"PLIVO_AUTH_ID": "MAID", "PLIVO_AUTH_TOKEN": "s"}, client)

    assert asyncio.run(read()) == {"left": 12.5, "unit": "USD", "limit": None, "used": None, "low": False}
