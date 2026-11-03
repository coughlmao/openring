"""Vonage Voice API: signed webhooks, the NCCO that connects a call's audio to OpenRing, and its REST calls.

A Vonage number answers through an Application whose Answer URL is `$PUBLIC_URL/vonage/voice` and Event URL
`$PUBLIC_URL/vonage/event`. OpenRing answers with an NCCO that connects the call to `/ws/vonage`. Calls to Vonage
carry a JWT signed with the application's private key (RS256); Vonage's webhooks carry one signed with the
account's signature secret (HS256).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

API = "https://api.nexmo.com/v1/calls"
# 16-bit linear PCM at 16 kHz, in packets of 20 ms: what Pipecat's serializer sends and reads.
AUDIO = "audio/l16;rate=16000"
PACKET_BYTES = 640
TOLERANCE = 300  # seconds a webhook's token may differ from now: older ones are replays
# How an outgoing call that never reached the assistant ended, from Vonage's status to Vapi's endedReason.
UNREACHED = {
    "busy": "customer-busy",
    "unanswered": "customer-did-not-answer",
    "timeout": "customer-did-not-answer",
    "rejected": "customer-did-not-answer",
    "cancelled": "customer-did-not-answer",
    "failed": "vonage-failed-to-connect-call",
}


class VonageError(Exception):
    """Vonage refused a request, or what it needs is missing; the message says which."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def digits(number: str) -> str:
    """Vonage wants a number as digits only, country code first, without the +."""
    return "".join(ch for ch in str(number) if ch.isdigit())


def verify_webhook(secret: str, token: str, body: bytes = b"", *, application_id: str = "",
                   now: float | None = None) -> bool:
    """Whether a webhook's bearer token is Vonage's: HS256 with the signature secret, issued within the last
    minutes and, when it says so, for this application and for this body."""
    if not secret:
        return False
    try:
        head, claims_part, signature = token.split(".")
        if json.loads(_unb64(head)).get("alg") != "HS256":  # never "none", never anything a stranger picks
            return False
        expected = hmac.new(secret.encode(), f"{head}.{claims_part}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(signature)):
            return False
        claims = json.loads(_unb64(claims_part))
        if abs((time.time() if now is None else now) - float(claims["iat"])) > TOLERANCE:
            return False
    except (ValueError, KeyError, TypeError, AttributeError):
        return False
    if application_id and claims.get("application_id") and claims["application_id"] != application_id:
        return False
    if claims.get("payload_hash") and body:
        return hmac.compare_digest(str(claims["payload_hash"]).lower(), hashlib.sha256(body).hexdigest())
    return True


def load_key(value: str) -> str:
    """The private key's PEM, given the key itself or the path of the file Vonage let you download."""
    if "BEGIN" in value:
        return value.replace("\\n", "\n")
    try:
        return Path(value).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise VonageError(f"can't read the private key file {value}: {exc.strerror or exc}") from exc


def make_jwt(application_id: str, private_key: str, *, now: float | None = None) -> str:
    """The token Vonage's API takes: the application's id, signed with its private key."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    issued = int(time.time() if now is None else now)
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"application_id": application_id, "iat": issued, "exp": issued + 900,
                              "jti": str(uuid.uuid4())}).encode())
    try:
        key = serialization.load_pem_private_key(load_key(private_key).encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise VonageError("the private key isn't a PEM file Vonage made for this application") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise VonageError("the private key isn't an RSA key, which Vonage applications use")
    signature = key.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{claims}.{_b64(signature)}"


def connect_ncco(ws_url: str) -> list[dict[str, Any]]:
    """Connect the call's audio, both ways, to OpenRing's media websocket."""
    return [{"action": "connect", "endpoint": [{"type": "websocket", "uri": ws_url, "content-type": AUDIO}]}]


def say_ncco(text: str) -> list[dict[str, Any]]:
    return [{"action": "talk", "text": text}]


def _error(response: httpx.Response) -> VonageError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    said = (body.get("error_text") or body.get("detail") or body.get("title") or body.get("error_title")
            or response.text or response.status_code) if isinstance(body, dict) else response.text
    return VonageError(f"{said} (Vonage error {response.status_code})")


async def create_call(application_id: str, private_key: str, *, to: str, from_: str, ws_url: str,
                      event_url: str) -> str:
    """Start an outgoing call, which connects to `ws_url` once answered. Returns the call's uuid."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(API, headers={"Authorization": f"Bearer {make_jwt(application_id, private_key)}"}, json={
            "to": [{"type": "phone", "number": digits(to)}], "from": {"type": "phone", "number": digits(from_)},
            "ncco": connect_ncco(ws_url), "event_url": [event_url], "event_method": "POST"})
    if r.status_code >= 400:
        raise _error(r)
    return str((r.json() or {}).get("uuid") or "")


async def _modify(application_id: str, private_key: str, call_uuid: str, body: dict[str, Any]) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.put(f"{API}/{call_uuid}",
                             headers={"Authorization": f"Bearer {make_jwt(application_id, private_key)}"}, json=body)
    if r.status_code >= 400:
        raise _error(r)


async def hang_up(application_id: str, private_key: str, call_uuid: str) -> None:
    await _modify(application_id, private_key, call_uuid, {"action": "hangup"})


async def transfer_call(application_id: str, private_key: str, call_uuid: str, number: str, from_: str) -> None:
    """Move a live call to a phone: its stream ends and Vonage rings `number`."""
    await _modify(application_id, private_key, call_uuid, {"action": "transfer", "destination": {
        "type": "ncco", "ncco": [{"action": "connect", "from": digits(from_),
                                  "endpoint": [{"type": "phone", "number": digits(number)}]}]}})
