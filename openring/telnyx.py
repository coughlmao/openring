"""Telnyx helpers: webhook signatures and TeXML that streams a call's audio to OpenRing.

A Telnyx number answers through a TeXML Application whose voice URL is `$PUBLIC_URL/telnyx/voice`.
OpenRing answers with TeXML (Telnyx's TwiML-like XML) that connects the call to `/ws/telnyx`.
"""

from __future__ import annotations

import base64
import time
from xml.sax.saxutils import quoteattr

# Telnyx signs each webhook with Ed25519 over "<timestamp>|<raw body>". Older timestamps are replays.
SIGNATURE_TOLERANCE = 300  # seconds


def is_valid_signature(public_key: str, timestamp: str, body: bytes, signature: str, *, now: float | None = None,
                       tolerance: int = SIGNATURE_TOLERANCE) -> bool:
    """Check the telnyx-signature-ed25519 and telnyx-timestamp headers against the account's public key."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        sent_at = int(timestamp)
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key))
        raw_signature = base64.b64decode(signature)
    except (TypeError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - sent_at) > tolerance:
        return False
    try:
        key.verify(raw_signature, timestamp.encode() + b"|" + body)
    except InvalidSignature:
        return False
    return True


def stream_texml(ws_url: str) -> str:
    """Connect the call's audio, both ways, to OpenRing's media websocket."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url={quoteattr(ws_url)} bidirectionalMode="rtp" /></Connect></Response>'
    )


class TelnyxError(Exception):
    """Telnyx refused a request; the message is Telnyx's own explanation."""


# How an outgoing call that never reached the assistant ended, from Telnyx's status to Vapi's endedReason.
UNREACHED = {
    "busy": "customer-busy",
    "no-answer": "customer-did-not-answer",
    "canceled": "customer-did-not-answer",
    "failed": "telnyx-failed-to-connect-call",
    "completed": "telnyx-failed-to-connect-call",  # answered, but the media stream never started
}


async def create_call(api_key: str, application_id: str, *, to: str, from_: str, url: str, status_callback: str) -> str:
    """Start an outgoing call through a TeXML application. Telnyx fetches `url` (by POST, signed like an incoming
    call's webhook) for instructions once it is answered. Returns Telnyx's id for the call, when it says one."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"https://api.telnyx.com/v2/texml/calls/{application_id}",
                              headers={"Authorization": f"Bearer {api_key}"},
                              data={"To": to, "From": from_, "Url": url, "UrlMethod": "POST",
                                    "StatusCallback": status_callback, "StatusCallbackMethod": "POST"})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400:
        errors = body.get("errors") if isinstance(body, dict) else None
        said = (errors[0].get("detail") or errors[0].get("title")) if errors and isinstance(errors[0], dict) else None
        raise TelnyxError(f"{said or r.text or r.status_code} (Telnyx error {r.status_code})")
    data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    return str(data.get("call_sid") or data.get("sid") or "") if isinstance(data, dict) else ""
