import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from openring import providers
from openring.api import create_app
from openring.config import Settings
from openring.providers import KOKORO_FILES, ProviderError, build_services, resolve_plan
from openring.store import Store
from openring.twilio import compute_signature

AUTH = {"Authorization": "Bearer test-key"}
ASSISTANT = {
    "name": "Clinica Sorriso",
    "firstMessage": "Olá! Clínica Sorriso, como posso ajudar?",
    "model": {
        "provider": "groq",
        "model": "llama-3.3-70b-versatile",
        "temperature": 0.4,
        "messages": [{"role": "system", "content": "Você é a recepcionista."}],
    },
    "voice": {"provider": "11labs", "voiceId": "voice123"},
    "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "pt-BR"},
    "server": {"url": "https://example.com/hook"},
}


def make_client(**overrides) -> TestClient:
    settings = Settings(
        **{
            "api_key": "test-key",
            "public_url": "https://ring.example.com",
            "db_path": ":memory:",
            "twilio_auth_token": "tok",
            "validate_twilio_signature": True,
            **overrides,
        }
    )
    return TestClient(create_app(settings, Store(":memory:")))


def test_auth_required():
    client = make_client()
    assert client.get("/assistant").status_code == 401
    assert client.get("/assistant", headers=AUTH).status_code == 200
    assert client.get("/health").status_code == 200


def test_assistant_crud():
    client = make_client()
    created = client.post("/assistant", json=ASSISTANT, headers=AUTH)
    assert created.status_code == 201
    a = created.json()
    assert a["id"] and a["createdAt"] and a["name"] == "Clinica Sorriso"

    patched = client.patch(f"/assistant/{a['id']}", json={"name": "Novo"}, headers=AUTH).json()
    assert patched["name"] == "Novo" and patched["id"] == a["id"]
    assert len(client.get("/assistant", headers=AUTH).json()) == 1
    assert client.delete(f"/assistant/{a['id']}", headers=AUTH).status_code == 200
    assert client.get(f"/assistant/{a['id']}", headers=AUTH).status_code == 404


def test_unknown_provider_rejected():
    client = make_client()
    bad = {**ASSISTANT, "voice": {"provider": "nope"}}
    r = client.post("/assistant", json=bad, headers=AUTH)
    assert r.status_code == 400 and "nope" in r.json()["detail"]


def _signed_post(client, form):
    sig = compute_signature("tok", "https://ring.example.com/twilio/voice", form)
    return client.post("/twilio/voice", data=form, headers={"X-Twilio-Signature": sig})


def test_inbound_call_returns_stream_twiml():
    client = make_client()
    a = client.post("/assistant", json=ASSISTANT, headers=AUTH).json()
    client.post("/phone-number", json={"number": "+1 (555) 123-4567", "assistantId": a["id"]}, headers=AUTH)

    form = {"To": "+15551234567", "From": "+5511999999999", "CallSid": "CA123"}
    r = _signed_post(client, form)
    assert r.status_code == 200
    stream = ET.fromstring(r.text).find("./Connect/Stream")
    assert stream.get("url") == "wss://ring.example.com/ws/twilio"
    params = {p.get("name"): p.get("value") for p in stream.findall("Parameter")}
    assert params["assistantId"] == a["id"]

    call = client.get(f"/call/{params['callId']}", headers=AUTH).json()
    assert call["type"] == "inboundPhoneCall" and call["customer"]["number"] == "+5511999999999"


def test_bad_twilio_signature_rejected():
    client = make_client()
    r = client.post("/twilio/voice", data={"To": "+1"}, headers={"X-Twilio-Signature": "forged"})
    assert r.status_code == 403


def test_twilio_signature_accepted_with_or_without_the_default_port():
    # Twilio signs some webhooks over "https://host:443/..." even when it was given "https://host/...".
    client = make_client()
    form = {"To": "+19999999999", "From": "+1", "CallSid": "CA1"}
    for signed_url in ("https://ring.example.com/twilio/voice", "https://ring.example.com:443/twilio/voice"):
        sig = compute_signature("tok", signed_url, form)
        assert client.post("/twilio/voice", data=form, headers={"X-Twilio-Signature": sig}).status_code == 200
    other_port = compute_signature("tok", "https://ring.example.com:8443/twilio/voice", form)
    assert client.post("/twilio/voice", data=form, headers={"X-Twilio-Signature": other_port}).status_code == 403


def test_twilio_webhook_refused_without_auth_token():
    form = {"To": "+15551234567", "From": "+1", "CallSid": "CA1"}
    r = make_client(twilio_auth_token="").post("/twilio/voice", data=form)
    assert r.status_code == 403 and "TWILIO_AUTH_TOKEN" in r.json()["detail"]
    # Only an explicit opt-out (local tests) lets unsigned requests through.
    unchecked = make_client(twilio_auth_token="", validate_twilio_signature=False)
    assert unchecked.post("/twilio/voice", data=form).status_code == 200


def test_number_without_assistant_says_message():
    client = make_client()
    r = _signed_post(client, {"To": "+19999999999", "From": "+1", "CallSid": "CA1"})
    assert "<Say>" in r.text and "<Stream" not in r.text


def test_plan_maps_vapi_fields(monkeypatch):
    plan = resolve_plan(ASSISTANT, check_keys=False)
    assert plan.tts_provider == "elevenlabs"  # Vapi's "11labs" alias
    assert plan.llm_base_url == "https://api.groq.com/openai/v1"
    assert plan.messages[0]["role"] == "system"
    assert plan.server_url == "https://example.com/hook"
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="GROQ_API_KEY"):
        resolve_plan(ASSISTANT)
    monkeypatch.setenv("OPENRING_ENV_FILE", "/home/me/.openring/.env")  # set by the `openring` command
    with pytest.raises(ProviderError, match="add it to /home/me/.openring/.env and restart"):
        resolve_plan(ASSISTANT)


def test_services_instantiate(monkeypatch):
    for k in ("DEEPGRAM_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.setenv(k, "fake")
    stt, llm, tts = build_services(resolve_plan(ASSISTANT))
    assert type(stt).__name__ == "DeepgramSTTService"
    assert type(llm).__name__ == "OpenAILLMService"
    assert type(tts).__name__ == "ElevenLabsTTSService"


def test_kokoro_call_waits_for_the_model_download(monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "KOKORO_DIR", tmp_path)
    started = []
    monkeypatch.setattr(providers, "prepare_kokoro", lambda: started.append(True))
    monkeypatch.setenv("DEEPGRAM_API_KEY", "fake")
    monkeypatch.setenv("GROQ_API_KEY", "fake")
    assistant = {**ASSISTANT, "voice": {"provider": "kokoro", "voiceId": "pf_dora"}}
    with pytest.raises(ProviderError, match="still downloading"):
        resolve_plan(assistant)
    assert started
    for name in KOKORO_FILES:
        (tmp_path / name).write_bytes(b"model")
    assert resolve_plan(assistant).tts_provider == "kokoro"


def test_kokoro_download_lands_whole_files_only(monkeypatch, tmp_path):
    import contextlib

    import httpx

    monkeypatch.setattr(providers, "KOKORO_DIR", tmp_path)
    monkeypatch.setattr(providers, "_kokoro", {"thread": None, "bytes": 0, "error": "", "model": None})

    class Response:
        def raise_for_status(self):
            pass

        def iter_bytes(self, _size):
            yield b"abc"
            raise httpx.ReadError("connection lost")

    @contextlib.contextmanager
    def stream(*_args, **_kwargs):
        yield Response()

    monkeypatch.setattr(httpx, "stream", stream)
    with pytest.raises(httpx.ReadError):
        providers._download_kokoro()
    assert not providers.kokoro_ready()

    Response.iter_bytes = lambda self, _size: iter([b"abc", b"def"])
    providers._download_kokoro()
    assert providers.kokoro_ready()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(KOKORO_FILES)  # no .part left behind
    assert (tmp_path / next(iter(KOKORO_FILES))).read_bytes() == b"abcdef"


def test_dashboard_served_without_key():
    client = make_client()
    r = client.get("/")
    assert r.status_code == 200 and "<title>OpenRing</title>" in r.text
    assert r.headers["cache-control"] == "no-cache"  # an upgrade shows the new dashboard right away


def _local_client(**kw):
    settings = Settings(api_key="test-key", public_url="", db_path=":memory:")
    return TestClient(create_app(settings, Store(":memory:")), client=("127.0.0.1", 50000), **kw)


DASH = {"X-OpenRing": "dashboard"}


def test_password_setup_only_from_this_computer():
    # ngrok/cloudflared connect from 127.0.0.1 but keep the public Host and add forwarding headers.
    tunnel = _local_client(base_url="https://abc.ngrok-free.app")
    assert tunnel.get("/auth/state").json()["canSetUp"] is False
    assert tunnel.post("/auth/setup", json={"password": "long enough"}).status_code == 403
    forwarded = _local_client(base_url="http://localhost:8000")
    assert forwarded.post("/auth/setup", json={"password": "long enough"},
                          headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 403
    assert make_client().post("/auth/setup", json={"password": "long enough"}).status_code == 403

    local = _local_client(base_url="http://localhost:8000")
    state = local.get("/auth/state").json()
    assert state["canSetUp"] and not state["passwordSet"] and not state["signedIn"]
    assert local.post("/auth/setup", json={"password": "short"}).status_code == 400
    assert local.post("/auth/setup", json={"password": "long enough"}).status_code == 200
    assert local.get("/auth/state", headers=DASH).json()["signedIn"] is True
    assert local.post("/auth/setup", json={"password": "another one"}).status_code == 409


def test_dashboard_session_unlocks_the_api():
    client = make_client(password="correct horse")
    assert client.get("/assistant").status_code == 401
    assert client.post("/auth/login", json={"password": "wrong password"}).status_code == 401
    r = client.post("/auth/login", json={"password": "correct horse"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    # The cookie alone is not enough: a cross-site request cannot add the dashboard header.
    assert client.get("/assistant").status_code == 401
    assert client.get("/assistant", headers=DASH).status_code == 200
    info = client.get("/dashboard/info", headers=DASH).json()
    assert info["apiKey"] == "test-key"
    assert info["twilioVoiceUrl"] == "https://ring.example.com/twilio/voice"
    assert {p["id"] for p in info["providers"]} >= {"deepgram", "groq", "11labs", "kokoro", "twilio"}
    assert "test-key" not in str(info["providers"])

    client.post("/auth/logout", headers=DASH)
    assert client.get("/assistant", headers=DASH).status_code == 401


def test_dashboard_info_needs_a_session_not_the_api_key():
    client = make_client()
    assert client.get("/dashboard/info", headers=AUTH).status_code == 401


def test_change_password_signs_out_other_browsers():
    settings = Settings(api_key="test-key", public_url="", db_path=":memory:", password="first password")
    app = create_app(settings, Store(":memory:"))
    a, b = TestClient(app), TestClient(app)
    for c in (a, b):
        assert c.post("/auth/login", json={"password": "first password"}).status_code == 200
    assert a.post("/auth/password", json={"current": "nope", "password": "second password"},
                  headers=DASH).status_code == 400
    assert a.post("/auth/password", json={"current": "first password", "password": "second password"},
                  headers=DASH).status_code == 200
    assert a.get("/assistant", headers=DASH).status_code == 200
    assert b.get("/assistant", headers=DASH).status_code == 401
    assert b.post("/auth/login", json={"password": "second password"}).status_code == 200


def test_login_throttled_after_many_wrong_passwords():
    client = make_client(password="correct horse")
    for _ in range(10):
        assert client.post("/auth/login", json={"password": "guess"}).status_code == 401
    assert client.post("/auth/login", json={"password": "correct horse"}).status_code == 429


def test_stats_endpoint():
    client = make_client()
    assert client.get("/stats").status_code == 401
    body = client.get("/stats?days=7", headers=AUTH).json()
    assert body["calls"] == 0 and len(body["perDay"]) == 7


def test_an_example_api_key_is_never_accepted():
    """`change-me` from .env.example would let anyone in: it counts as no key at all."""
    client = make_client(api_key="change-me")
    r = client.get("/assistant", headers={"Authorization": "Bearer change-me"})
    assert r.status_code == 401 and "only answers this computer" in r.json()["detail"]
    local = TestClient(create_app(Settings(api_key="change-me", public_url="", db_path=":memory:"),
                                  Store(":memory:")), client=("127.0.0.1", 50000), base_url="http://localhost:8000")
    assert local.get("/assistant").status_code == 200


def test_without_an_api_key_only_this_computer_reaches_the_api():
    remote = make_client(api_key="")
    assert remote.get("/assistant").status_code == 401
    assert remote.get("/stats").status_code == 401
    # Tunnels connect from 127.0.0.1 but keep the public Host and add forwarding headers.
    tunnel = TestClient(create_app(Settings(api_key="", public_url="", db_path=":memory:"), Store(":memory:")),
                        client=("127.0.0.1", 50000), base_url="https://abc.trycloudflare.com")
    assert tunnel.get("/assistant").status_code == 401
    local = _local_client(base_url="http://localhost:8000")
    local.app.state.settings.api_key = ""
    assert local.get("/assistant").status_code == 200
    assert local.get("/assistant", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 401


def test_without_an_api_key_the_dashboard_session_still_works():
    client = make_client(api_key="", password="correct horse")
    assert client.post("/auth/login", json={"password": "correct horse"}).status_code == 200
    assert client.get("/assistant", headers=DASH).status_code == 200


def test_phone_numbers_must_look_like_phone_numbers():
    client = make_client()
    for bad in ("abc", "+1", "", "<img src=x onerror=alert(1)>", "+1234567890123456"):
        r = client.post("/phone-number", json={"number": bad}, headers=AUTH)
        assert r.status_code == 400 and "country code" in r.json()["detail"], bad
    ok = client.post("/phone-number", json={"number": "+1 (555) 123-4567"}, headers=AUTH)
    assert ok.status_code == 201
    r = client.patch(f"/phone-number/{ok.json()['id']}", json={"number": "call me"}, headers=AUTH)
    assert r.status_code == 400
    assert client.patch(f"/phone-number/{ok.json()['id']}", json={"number": "+15557654321"},
                        headers=AUTH).status_code == 200


def test_a_caller_without_digits_never_matches_a_number():
    # Saved before validation existed: a number with no digits must not answer an anonymous caller.
    store = Store(":memory:")
    store.create("phone_numbers", {"number": "unknown"})
    assert store.find_phone_number("anonymous") is None
    assert store.find_phone_number("") is None


def test_the_owner_at_this_computer_is_never_locked_out_by_typos():
    settings = Settings(api_key="test-key", public_url="", db_path=":memory:", password="correct horse")
    local = TestClient(create_app(settings, Store(":memory:")), client=("127.0.0.1", 50000),
                       base_url="http://localhost:8000")
    for _ in range(15):
        assert local.post("/auth/login", json={"password": "typo"}).status_code == 401
    assert local.post("/auth/login", json={"password": "correct horse"}).status_code == 200
    # Through a tunnel (public Host, forwarding headers) the limit still applies.
    tunnel = TestClient(local.app, client=("127.0.0.1", 50000), base_url="https://abc.trycloudflare.com")
    for _ in range(10):
        tunnel.post("/auth/login", json={"password": "guess"}, headers={"X-Forwarded-For": "203.0.113.9"})
    assert tunnel.post("/auth/login", json={"password": "correct horse"},
                       headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 429


def test_words_to_recognize_are_kept_and_bad_ones_refused():
    client = make_client()
    transcriber = {"provider": "deepgram", "keyterm": ["Clínica Sorriso"], "keywords": ["Sorriso:2"]}
    made = client.post("/assistant", json={**ASSISTANT, "transcriber": transcriber}, headers=AUTH)
    assert made.status_code == 201 and made.json()["transcriber"]["keyterm"] == ["Clínica Sorriso"]
    bad = {**ASSISTANT, "transcriber": {"provider": "deepgram", "keywords": ["two words"]}}
    r = client.post("/assistant", json=bad, headers=AUTH)
    assert r.status_code == 400 and "keywords" in r.json()["detail"]


def _clock(monkeypatch, *stamps):
    """The next documents are created at these moments."""
    moments = iter(stamps)
    monkeypatch.setattr("openring.store.now_iso", lambda: next(moments))


def _calls_between(client, **filters):
    response = client.get("/call", params=filters, headers=AUTH)
    assert response.status_code == 200, response.text
    return {call["name"] for call in response.json()}


def test_list_date_filters(monkeypatch):
    store = Store(":memory:")
    client = TestClient(create_app(Settings(api_key="test-key", db_path=":memory:"), store))
    _clock(monkeypatch, "2026-01-01T10:00:00.250000Z", "2026-02-01T10:00:00.000000Z", "2026-03-01T10:00:00.000000Z")
    for name in ("First", "Second", "Third"):
        store.create("calls", {"name": name})

    assert _calls_between(client, createdAtGt="2026-01-15T00:00:00.000Z") == {"Second", "Third"}
    assert _calls_between(client, createdAtLt="2026-02-15T00:00:00Z") == {"First", "Second"}
    assert _calls_between(client, createdAtGe="2026-02-01T10:00:00Z") == {"Second", "Third"}
    assert _calls_between(client, createdAtLe="2026-02-01T10:00:00Z") == {"First", "Second"}
    assert _calls_between(client, createdAtGe="2026-01-15", createdAtLe="2026-02-15") == {"Second"}
    assert _calls_between(client) == {"First", "Second", "Third"}
    # A moment is a moment, whichever way it is written: this is 10:00 UTC, and the first call is 10:00:00.25.
    assert _calls_between(client, createdAtGt="2026-01-01T07:00:00-03:00") == {"First", "Second", "Third"}
    assert _calls_between(client, createdAtGt="2026-01-01T10:00:00+00:00") == {"First", "Second", "Third"}
    assert _calls_between(client, createdAtGt="2026-01-01T10:00:01+00:00") == {"Second", "Third"}
    assert _calls_between(client, createdAtLe="2026-02-01T07:00:00-03:00") == {"First", "Second"}


@pytest.mark.parametrize("value", ["yesterday", "2026-13-45", "", "1; DROP TABLE calls"])
def test_a_date_that_is_not_one_is_refused_not_ignored(value):
    client = TestClient(create_app(Settings(api_key="test-key", db_path=":memory:"), Store(":memory:")))
    for path in ("/call", "/assistant", "/tool", "/phone-number"):
        r = client.get(path, params={"createdAtLt": value}, headers=AUTH)
        assert r.status_code == 400 and "createdAtLt" in r.json()["detail"], path


def test_every_list_that_takes_the_filters_applies_them(monkeypatch):
    store = Store(":memory:")
    client = TestClient(create_app(Settings(api_key="test-key", db_path=":memory:"), store))
    _clock(monkeypatch, *["2026-01-01T00:00:00.000000Z"] * 4)
    for collection in ("assistants", "tools", "phone_numbers", "calls"):
        store.create(collection, {"name": collection})
    for path in ("/assistant", "/tool", "/phone-number", "/call"):
        assert len(client.get(path, headers=AUTH).json()) == 1
        assert client.get(path, params={"createdAtGt": "2026-06-01"}, headers=AUTH).json() == []
        assert len(client.get(path, params={"createdAtLt": "2026-06-01"}, headers=AUTH).json()) == 1


def test_importing_from_another_openring_gets_every_page(monkeypatch):
    """The import pages back with createdAtLt. A server that ignored it sent the same first page again, for ever."""
    import asyncio

    import httpx

    from openring.importer import import_account

    source = Store(":memory:")
    app = create_app(Settings(api_key="test-key", db_path=":memory:"), source)
    # Each at a moment of its own: paging by time can't tell apart what was made in the same tick of the clock.
    with monkeypatch.context() as clock:
        _clock(clock, *[f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}.000000Z" for i in range(230)])
        for i in range(230):
            source.create("tools", {"type": "endCall", "name": f"tool {i}"})

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport) as client:
            return await asyncio.wait_for(import_account("https://source.example.com", "test-key", Store(":memory:"),
                                                         client=client), 60)

    assert len(asyncio.run(go())["tools"]) == 230
