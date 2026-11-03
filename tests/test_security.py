"""Security guards for the whole API: a new route must not be reachable without a key by accident."""

import re

from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient

from openring.api import create_app
from openring.config import Settings
from openring.store import Store

# Routes anyone may call. Webhooks prove who sends them inside the handler (each provider's signature);
# the dashboard page, health check, sign-in and API docs are public on purpose.
PUBLIC_HTTP = {
    "/", "/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
    "/auth/state", "/auth/setup", "/auth/login", "/auth/logout",
    "/twilio/voice", "/twilio/status", "/telnyx/voice", "/telnyx/status",
    "/plivo/voice", "/plivo/status", "/plivo/transfer/{token}", "/vonage/voice", "/vonage/event",
}
# Media streams: each claims a queued call by id (Twilio, Telnyx, Plivo, Vonage) or checks a signature (Genesys).
PUBLIC_WS = {"/ws/twilio", "/ws/telnyx", "/ws/plivo", "/ws/vonage", "/ws/genesys"}


def app():
    return create_app(Settings(api_key="k" * 32, public_url="https://ring.example.com", db_path=":memory:",
                               twilio_auth_token="tok", telnyx_public_key="A" * 44, plivo_auth_token="tok",
                               vonage_signature_secret="secret-123"), Store(":memory:"))


def test_every_route_but_the_public_ones_needs_a_key_or_a_session():
    a = app()
    client = TestClient(a)
    checked = 0
    for route in a.routes:
        if isinstance(route, APIWebSocketRoute):
            assert route.path in PUBLIC_WS, f"new websocket {route.path}: decide how it authenticates"
            continue
        if not isinstance(route, APIRoute) or route.path in PUBLIC_HTTP:
            continue
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        for method in route.methods - {"HEAD"}:
            r = client.request(method, path, json={})
            assert r.status_code == 401, f"{method} {route.path} answered {r.status_code} without a key"
            r = client.request(method, path, json={}, headers={"Authorization": "Bearer wrong"})
            assert r.status_code == 401, f"{method} {route.path} answered {r.status_code} with a wrong key"
            checked += 1
    assert checked > 40  # the guard really walked the API


def test_webhooks_refuse_unsigned_requests():
    client = TestClient(app())
    for path in ("/twilio/voice", "/twilio/status?callId=x"):
        assert client.post(path, data={"CallSid": "CA1", "To": "+15551234567"}).status_code == 403
    for path in ("/telnyx/voice", "/telnyx/status?callId=x", "/plivo/voice", "/plivo/status?callId=x",
                 "/plivo/transfer/x", "/vonage/voice", "/vonage/event?callId=x"):
        assert client.post(path, data={"To": "+15551234567"}).status_code == 403, path
    for path in ("/plivo/voice?To=1", "/vonage/voice?to=1", "/vonage/event?callId=x"):  # they can be GETs too
        assert client.get(path).status_code == 403, path
