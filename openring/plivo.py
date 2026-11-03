"""Plivo: signed webhooks, the XML that streams a call's audio to OpenRing, and the REST calls for outgoing calls.

A Plivo number answers through an Application whose Answer URL is `$PUBLIC_URL/plivo/voice` (and Hangup URL
`$PUBLIC_URL/plivo/status`). OpenRing answers with XML that connects the call to `/ws/plivo`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qs, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import httpx

API = "https://api.plivo.com/v1/Account"
# G.711 mu-law at 8 kHz: what the phone network carries, and what Pipecat's serializer sends and reads.
CONTENT_TYPE = "audio/x-mulaw;rate=8000"
# How an outgoing call that never reached the assistant ended, from Plivo's status to Vapi's endedReason.
UNREACHED = {
    "busy": "customer-busy",
    "no-answer": "customer-did-not-answer",
    "timeout": "customer-did-not-answer",
    "cancel": "customer-did-not-answer",
    "failed": "plivo-failed-to-connect-call",
    "completed": "plivo-failed-to-connect-call",  # answered, but the media stream never started
}


class PlivoError(Exception):
    """Plivo refused a request; the message is Plivo's own explanation."""


def _sorted_query(query: dict[str, list[str]]) -> str:
    return "&".join(f"{key}={value}" for key in sorted(query) for value in sorted(query[key]))


def signed_string(method: str, url: str, params: dict[str, str]) -> str:
    """What Plivo signs, as its SDKs build it: the URL with its query sorted and, for a POST, the form fields
    appended as name and value, sorted by name. With both a query and fields, a `.` goes between them."""
    parts = urlsplit(url)
    base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    query = _sorted_query(parse_qs(parts.query, keep_blank_values=True))
    if method.upper() == "GET":
        return base + (f"?{query}" if query else "")
    if query or params:
        base += f"?{query}"
    if query and params:
        base += "."
    return base + "".join(f"{key}{params[key]}" for key in sorted(params))


def compute_signature(auth_token: str, method: str, url: str, params: dict[str, str], nonce: str) -> str:
    """Plivo's X-Plivo-Signature-V3: base64(HMAC-SHA256(token, signed string + "." + nonce))."""
    data = f"{signed_string(method, url, params)}.{nonce}"
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha256).digest()).decode()


def is_valid_signature(auth_token: str, method: str, url: str, params: dict[str, str], nonce: str,
                       signature: str) -> bool:
    """Whether `signature` (the header; several, comma separated, when an account has several tokens) is Plivo's."""
    if not (auth_token and nonce and signature):
        return False
    expected = compute_signature(auth_token, method, url, params, nonce)
    return any(hmac.compare_digest(expected, given.strip()) for given in signature.split(","))


def stream_xml(ws_url: str) -> str:
    """Connect the call's audio, both ways, to OpenRing's media websocket."""
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Stream bidirectional="true" keepCallAlive="true" contentType="{CONTENT_TYPE}">{escape(ws_url)}</Stream>'
            "</Response>")


def dial_xml(number: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Dial><Number>{escape(number)}</Number></Dial></Response>'


def say_xml(text: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Speak>{escape(text)}</Speak><Hangup/></Response>'


def hangup_xml() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def _error(response: httpx.Response) -> PlivoError:
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    return PlivoError(f"{body.get('error') or body.get('message') or response.text or response.status_code} "
                      f"(Plivo error {response.status_code})")


async def create_call(auth_id: str, auth_token: str, *, to: str, from_: str, answer_url: str, hangup_url: str) -> str:
    """Start an outgoing call. Plivo fetches `answer_url` for instructions once it is answered, and posts to
    `hangup_url` when it ends. Returns the request's id."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{API}/{auth_id}/Call/", auth=(auth_id, auth_token), json={
            "from": from_, "to": to, "answer_url": answer_url, "answer_method": "POST",
            "hangup_url": hangup_url, "hangup_method": "POST"})
    if r.status_code >= 400:
        raise _error(r)
    return str((r.json() or {}).get("request_uuid") or "")


async def transfer_call(auth_id: str, auth_token: str, call_uuid: str, answer_url: str) -> None:
    """Give a live call new instructions: its stream ends and Plivo follows the XML at `answer_url`."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{API}/{auth_id}/Call/{call_uuid}/", auth=(auth_id, auth_token), json={
            "legs": "aleg", "aleg_url": answer_url, "aleg_method": "POST"})
    if r.status_code >= 400:
        raise _error(r)
