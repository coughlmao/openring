"""Vonage: signed webhooks, the NCCO, the audio websocket, outgoing calls and transfers."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from openring import keys, vonage
from openring.api import create_app
from openring.bot import vonage_transport
from openring.config import Settings
from openring.providers import provider_status
from openring.store import Store

from test_api import ASSISTANT, AUTH

SECRET = "signature-secret-123"
APPLICATION = "11111111-2222-3333-4444-555555555555"
PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = PRIVATE.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def webhook_token(secret=SECRET, *, alg="HS256", iat=None, body=b"", **claims):
    head = b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    payload = {"iat": int(time.time()) if iat is None else iat, "jti": "j1", "iss": "Vonage", "api_key": "k",
               **({"payload_hash": hashlib.sha256(body).hexdigest()} if body else {}), **claims}
    part = b64(json.dumps(payload).encode())
    signature = hmac.new(secret.encode(), f"{head}.{part}".encode(), hashlib.sha256).digest() if alg == "HS256" \
        else b""
    return f"{head}.{part}.{b64(signature)}"


def test_who_may_call_the_webhooks():
    token = webhook_token()
    assert vonage.verify_webhook(SECRET, token)
    assert vonage.verify_webhook(SECRET, webhook_token(application_id=APPLICATION), application_id=APPLICATION)
    body = b'{"status": "started"}'
    assert vonage.verify_webhook(SECRET, webhook_token(body=body), body)
    assert not vonage.verify_webhook(SECRET, webhook_token(body=body), b'{"status": "other"}')  # not the body signed
    assert not vonage.verify_webhook("another-secret", token)
    assert not vonage.verify_webhook("", token)
    assert not vonage.verify_webhook(SECRET, webhook_token(iat=int(time.time()) - 3600))  # an old one, replayed
    assert not vonage.verify_webhook(SECRET, webhook_token(iat=int(time.time()) + 3600))
    assert not vonage.verify_webhook(SECRET, webhook_token(application_id="another-application"),
                                     application_id=APPLICATION)
    assert not vonage.verify_webhook(SECRET, webhook_token(alg="none"))  # a token that says it needs no signature
    for broken in ("", "a.b", "a.b.c.d", "not.a.token", token[:-4], "Bearer " + token):
        assert not vonage.verify_webhook(SECRET, broken)


def test_the_token_for_vonages_api():
    token = vonage.make_jwt(APPLICATION, PEM, now=1_800_000_000)
    head, payload, signature = token.split(".")
    assert json.loads(base64.urlsafe_b64decode(head + "==")) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    assert claims["application_id"] == APPLICATION and claims["iat"] == 1_800_000_000 and claims["exp"] > claims["iat"]
    PRIVATE.public_key().verify(base64.urlsafe_b64decode(signature + "=="), f"{head}.{payload}".encode(),
                                padding.PKCS1v15(), hashes.SHA256())  # Vonage checks it with the public key
    assert vonage.make_jwt(APPLICATION, PEM.replace("\n", "\\n")).count(".") == 2  # the key's text in one line


def test_the_private_key_may_be_a_file(tmp_path):
    path = tmp_path / "private.key"
    path.write_text(PEM, encoding="utf-8")
    assert vonage.make_jwt(APPLICATION, str(path)).count(".") == 2
    with pytest.raises(vonage.VonageError, match="can't read"):
        vonage.make_jwt(APPLICATION, str(tmp_path / "missing.key"))
    path.write_text("not a key", encoding="utf-8")
    with pytest.raises(vonage.VonageError, match="isn't a PEM"):
        vonage.make_jwt(APPLICATION, str(path))
    other = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    with pytest.raises(vonage.VonageError, match="RSA"):
        vonage.make_jwt(APPLICATION, other)


def test_the_ncco():
    ncco = vonage.connect_ncco("wss://x.test/ws/vonage?callId=c1")
    assert ncco == [{"action": "connect", "endpoint": [{"type": "websocket", "uri": "wss://x.test/ws/vonage?callId=c1",
                                                        "content-type": "audio/l16;rate=16000"}]}]
    assert vonage.say_ncco("Hi") == [{"action": "talk", "text": "Hi"}]
    assert vonage.digits("+55 (11) 91234-5678") == "5511912345678"


def app(monkeypatch, **overrides):
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    store = Store(":memory:")
    settings = Settings(**{"api_key": "test-key", "public_url": "https://x.test", "db_path": ":memory:",
                           "vonage_application_id": APPLICATION, "vonage_private_key": PEM,
                           "vonage_signature_secret": SECRET, **overrides})
    client = TestClient(create_app(settings, store))
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    n = client.post("/phone-number", json={"number": "+15551234567", "assistantId": a["id"], "provider": "vonage"},
                    headers=AUTH).json()
    return client, store, a, n


def signed(token=None):
    return {"Authorization": f"Bearer {token or webhook_token()}"}


QUERY = "from=5511999990000&to=15551234567&uuid=leg-1&conversation_uuid=con-1"


def test_incoming_call_is_answered_with_the_ncco_for_its_assistant(monkeypatch):
    client, store, a, _ = app(monkeypatch)
    r = client.get(f"/vonage/voice?{QUERY}", headers=signed())
    assert r.status_code == 200
    endpoint = r.json()[0]["endpoint"][0]
    url = urlsplit(endpoint["uri"])
    assert (url.scheme, url.netloc, url.path) == ("wss", "x.test", "/ws/vonage")
    call = store.get("calls", parse_qs(url.query)["callId"][0])
    assert (call["type"], call["status"], call["assistantId"]) == ("inboundPhoneCall", "queued", a["id"])
    assert (call["phoneCallProvider"], call["phoneCallProviderId"]) == ("vonage", "leg-1")
    assert call["customer"] == {"number": "+5511999990000"}
    body = json.dumps({"from": "5511999990000", "to": "15551234567", "uuid": "leg-2"}).encode()
    posted = client.post("/vonage/voice", content=body, headers={"content-type": "application/json",
                                                                 **signed(webhook_token(body=body))})
    assert posted.status_code == 200 and posted.json()[0]["action"] == "connect"


def test_unsigned_or_unverifiable_webhooks_are_refused(monkeypatch):
    client, store, _, _ = app(monkeypatch)
    assert client.get(f"/vonage/voice?{QUERY}").status_code == 403
    assert client.get(f"/vonage/voice?{QUERY}", headers=signed(webhook_token("another-secret"))).status_code == 403
    assert client.get(f"/vonage/voice?{QUERY}", headers=signed(webhook_token(alg="none"))).status_code == 403
    no_secret, store2, _, _ = app(monkeypatch, vonage_signature_secret="")
    assert no_secret.get(f"/vonage/voice?{QUERY}", headers=signed()).status_code == 403
    assert store.list("calls") == [] and store2.list("calls") == []
    trusting, _, _, _ = app(monkeypatch, vonage_signature_secret="", validate_vonage_signature=False)
    assert trusting.get(f"/vonage/voice?{QUERY}").status_code == 200


def test_number_without_assistant_says_so(monkeypatch):
    client, _, _, _ = app(monkeypatch)
    r = client.get("/vonage/voice?from=5511&to=19990000000&uuid=x", headers=signed())
    assert r.json()[0]["action"] == "talk"


@pytest.fixture
def placed(monkeypatch):
    calls = []

    async def create_call(application_id, private_key, *, to, from_, ws_url, event_url):
        calls.append({"application": application_id, "to": to, "from": from_, "ws": ws_url, "event": event_url})
        return "leg-out-1"

    monkeypatch.setattr(vonage, "create_call", create_call)
    return calls


def test_an_outgoing_call_connects_to_its_assistant_when_answered(monkeypatch, placed):
    client, store, _, n = app(monkeypatch)
    r = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": "+55 11 91234-5678"}},
                    headers=AUTH)
    assert r.status_code == 201, r.text
    call = r.json()
    assert (call["type"], call["status"], call["phoneCallProvider"], call["phoneCallProviderId"]) == (
        "outboundPhoneCall", "queued", "vonage", "leg-out-1")
    assert placed == [{"application": APPLICATION, "to": "+5511912345678", "from": "+15551234567",
                       "ws": f"wss://x.test/ws/vonage?callId={call['id']}",
                       "event": f"https://x.test/vonage/event?callId={call['id']}"}]


@pytest.mark.parametrize("status, reason", [("busy", "customer-busy"), ("unanswered", "customer-did-not-answer"),
                                            ("rejected", "customer-did-not-answer"),
                                            ("failed", "vonage-failed-to-connect-call")])
def test_an_outgoing_call_that_never_connected_ends_with_the_reason(monkeypatch, placed, status, reason):
    client, store, _, n = app(monkeypatch)
    call = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": "+5511912345678"}},
                       headers=AUTH).json()
    body = json.dumps({"status": status, "uuid": "leg-out-1"}).encode()
    r = client.post(f"/vonage/event?callId={call['id']}", content=body, headers={
        "content-type": "application/json", **signed(webhook_token(body=body))})
    assert r.status_code == 204
    assert (store.get("calls", call["id"])["status"], store.get("calls", call["id"])["endedReason"]) == (
        "ended", reason)


def test_events_of_a_call_that_connected_or_of_no_call_change_nothing(monkeypatch, placed):
    client, store, _, n = app(monkeypatch)
    call = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": "+5511912345678"}},
                       headers=AUTH).json()
    store.update("calls", call["id"], {"status": "in-progress"})
    for path in (f"/vonage/event?callId={call['id']}", "/vonage/event"):
        body = json.dumps({"status": "completed" if "callId" in path else "busy"}).encode()
        assert client.post(path, content=body, headers={"content-type": "application/json",
                                                        **signed(webhook_token(body=body))}).status_code == 204
    assert store.get("calls", call["id"])["status"] == "in-progress"
    assert client.post(f"/vonage/event?callId={call['id']}", json={"status": "busy"}).status_code == 403


def test_outgoing_calls_need_the_application_and_a_readable_key(monkeypatch, placed, tmp_path):
    for overrides, missing in (({"vonage_private_key": ""}, "VONAGE_PRIVATE_KEY"), ({"public_url": ""}, "PUBLIC_URL"),
                               ({"vonage_application_id": ""}, "VONAGE_APPLICATION_ID"),
                               ({"vonage_private_key": str(tmp_path / "gone.key")}, "can't read")):
        client, _, _, n = app(monkeypatch, **overrides)
        r = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": "+5511912345678"}},
                        headers=AUTH)
        assert r.status_code == 400 and missing in r.json()["detail"], overrides
    assert placed == []


def test_a_refusal_from_vonage_is_recorded_and_explained(monkeypatch):
    client, store, _, n = app(monkeypatch)

    async def refuse(*_a, **_k):
        raise vonage.VonageError("Insufficient funds (Vonage error 402)")

    monkeypatch.setattr(vonage, "create_call", refuse)
    r = client.post("/call", json={"phoneNumberId": n["id"], "customer": {"number": "+5511912345678"}},
                    headers=AUTH)
    assert r.status_code == 400 and "Vonage did not place the call" in r.json()["detail"]
    assert store.list("calls")[0]["endedReason"] == "vonage-failed-to-connect-call"


def test_vonages_api_calls(monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url), request.headers["authorization"].split(".")[0],
                     json.loads(request.content)))
        return httpx.Response(201 if request.method == "POST" else 204, json={"uuid": "leg-7"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    uuid = asyncio.run(vonage.create_call(APPLICATION, PEM, to="+5511", from_="+1555", ws_url="wss://x/ws",
                                          event_url="https://x/e"))
    asyncio.run(vonage.hang_up(APPLICATION, PEM, "leg-7"))
    asyncio.run(vonage.transfer_call(APPLICATION, PEM, "leg-7", "+55 11 98888-7777", "+1555"))
    assert uuid == "leg-7"
    method, url, header, body = seen[0]
    assert (method, url) == ("POST", "https://api.nexmo.com/v1/calls") and header.startswith("Bearer ")
    assert body["to"] == [{"type": "phone", "number": "5511"}] and body["from"] == {"type": "phone", "number": "1555"}
    assert body["ncco"] == vonage.connect_ncco("wss://x/ws") and body["event_url"] == ["https://x/e"]
    assert seen[1][:2] == ("PUT", "https://api.nexmo.com/v1/calls/leg-7") and seen[1][3] == {"action": "hangup"}
    assert seen[2][3]["destination"]["ncco"] == [{"action": "connect", "from": "1555",
                                                  "endpoint": [{"type": "phone", "number": "5511988887777"}]}]
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"title": "Unauthorized"})), **kw))
    with pytest.raises(vonage.VonageError, match="Unauthorized"):
        asyncio.run(vonage.hang_up(APPLICATION, PEM, "x"))


@pytest.fixture
def fake_bot(monkeypatch):
    class Runs(list):
        hung_up: list

    runs = Runs()

    async def run_call(websocket, *, plan, call_id, **kwargs):
        runs.append({"call": call_id, "transfer": kwargs.get("transfer"), "rate": kwargs.get("sample_rate")})
        await websocket.close()

    monkeypatch.setattr("openring.bot.vonage_transport", lambda websocket: websocket)
    monkeypatch.setattr("openring.bot.run_call", run_call)
    runs.hung_up = []

    async def hang_up(application_id, private_key, call_uuid):
        runs.hung_up.append(call_uuid)

    monkeypatch.setattr(vonage, "hang_up", hang_up)
    return runs


def _connect(client, call_id, first=None):
    """Open the websocket the way Vonage does and return the code the server closes it with."""
    with client.websocket_connect(f"/ws/vonage?callId={call_id}") as ws:
        ws.send_text(json.dumps(first or {"event": "websocket:connected", "content-type": "audio/l16;rate=16000"}))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    return closed.value.code


def _waiting_call(client):
    answered = client.get(f"/vonage/voice?{QUERY}", headers=signed()).json()
    return parse_qs(urlsplit(answered[0]["endpoint"][0]["uri"]).query)["callId"][0]


def test_the_websocket_runs_the_calls_assistant_once_and_hangs_up(monkeypatch, fake_bot):
    client, store, _, _ = app(monkeypatch)
    call_id = _waiting_call(client)
    assert _connect(client, call_id) == 1000
    assert _connect(client, call_id) == 1008  # a second connection for the same call
    assert _connect(client, "made-up") == 1008
    assert [r["call"] for r in fake_bot] == [call_id] and fake_bot[0]["rate"] == 16000  # wideband, not 8 kHz
    assert store.get("calls", call_id)["status"] == "in-progress"
    assert fake_bot.hung_up == ["leg-1"]  # the call ends with the stream, whatever Vonage does on its own


def test_a_websocket_that_does_not_say_it_is_connected_is_refused(monkeypatch, fake_bot):
    client, store, _, _ = app(monkeypatch)
    call_id = _waiting_call(client)
    assert _connect(client, call_id, first={"event": "something-else"}) == 1008
    assert _connect(client, call_id, first=[1, 2]) == 1008
    with client.websocket_connect(f"/ws/vonage?callId={call_id}") as ws:
        ws.send_bytes(b"\0" * 640)  # audio before the greeting is not what Vonage sends
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert closed.value.code == 1008
    assert fake_bot == [] and store.get("calls", call_id)["status"] == "queued"
    assert _connect(client, call_id) == 1000  # and the real one still gets the call


def test_a_transferred_call_is_not_hung_up(monkeypatch, fake_bot):
    client, _, _, _ = app(monkeypatch)
    moved = []

    async def transfer_call(application_id, private_key, call_uuid, number, from_):
        moved.append((call_uuid, number, from_))

    monkeypatch.setattr(vonage, "transfer_call", transfer_call)
    assert _connect(client, _waiting_call(client)) == 1000
    asyncio.run(fake_bot[0]["transfer"]("+5511988887777"))
    assert moved == [("leg-1", "+5511988887777", "+15551234567")]
    # run_call already returned in this stub, so check the order the other way: a failed transfer hangs up as usual.
    client2, _, _, _ = app(monkeypatch)
    fake_bot.hung_up.clear()

    async def refuse(*_a):
        raise vonage.VonageError("no")

    monkeypatch.setattr(vonage, "transfer_call", refuse)
    assert _connect(client2, _waiting_call(client2)) == 1000
    with pytest.raises(vonage.VonageError):
        asyncio.run(fake_bot[-1]["transfer"]("+5511988887777"))
    assert fake_bot.hung_up == ["leg-1"]


def test_the_stream_is_sent_in_packets_of_20_ms():
    transport = vonage_transport(SimpleNamespace())
    assert transport._params.fixed_audio_packet_size == 640 == 16000 * 2 * 20 // 1000


def test_dashboard_connects_vonage(tmp_path):
    fields = keys.CONNECTORS["vonage"]["fields"]
    assert [env for env, _ in fields] == ["VONAGE_APPLICATION_ID", "VONAGE_PRIVATE_KEY", "VONAGE_SIGNATURE_SECRET"]
    path = tmp_path / "private.key"
    path.write_text(PEM, encoding="utf-8")
    shown = str(path).replace("\\", "/")
    values = {"VONAGE_APPLICATION_ID": APPLICATION, "VONAGE_PRIVATE_KEY": shown, "VONAGE_SIGNATURE_SECRET": SECRET}
    assert keys.clean_values("vonage", values) == values
    for bad in ("with space.key", 'quote".key', "a;b"):
        with pytest.raises(keys.ConnectError, match="doesn't look like"):
            keys.clean_values("vonage", {**values, "VONAGE_PRIVATE_KEY": bad})
    with pytest.raises(keys.ConnectError, match="can't read"):
        keys.clean_values("vonage", {**values, "VONAGE_PRIVATE_KEY": shown + ".missing"})
    url, options = keys._check_request("vonage", values)
    assert url == "https://api.nexmo.com/v1/calls?page_size=1" and options["headers"]["Authorization"].startswith(
        "Bearer ")
    settings = Settings(db_path=":memory:", public_url="https://x.test", vonage_application_id=APPLICATION,
                        vonage_private_key=shown, vonage_signature_secret=SECRET)
    assert {r["id"]: r for r in provider_status(settings)}["vonage"]["ready"] is True
    assert {r["id"]: r for r in provider_status(Settings(db_path=":memory:"))}["vonage"]["ready"] is False
