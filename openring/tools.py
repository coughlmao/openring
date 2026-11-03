"""Assistant tools in Vapi's format: functions on your server, plain HTTP requests, ending the call and
transferring it.

An assistant lists tools in `model.tools`, or saves them with `POST /tool` and lists their ids in
`model.toolIds`. When the model calls a `function` tool, OpenRing POSTs Vapi's `tool-calls` message
to the tool's server (or the assistant's) and hands the model the `results` it answers with, so a
server written for Vapi works unchanged. An `apiRequest` tool needs no such server: the model fills in
the `body`, and OpenRing sends it to the tool's `url` (an n8n, Make or Zapier webhook, say).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
from loguru import logger

from . import knowledge, netguard, overrides
from .store import Store

# Tool types OpenRing runs. Assistants imported with other types still load; those tools are skipped.
SUPPORTED = ("function", "apiRequest", "endCall", "transferCall", "query", "handoff")
DEFAULT_TIMEOUT = 20  # seconds, like Vapi's server.timeoutSeconds default
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_RESPONSE_BYTES = 100_000  # what's read of an apiRequest's answer; the rest is never downloaded
MAX_RESULT_CHARS = 4_000  # what the model reads of it: every word costs tokens, on every turn after
MAX_NAME_CHARS = 64  # what model APIs accept for a tool's name
MAX_HANDOFF_VALUE_CHARS = 500  # of one value a handoff passes along
HANDOFF_UNDER_WAY = "The call is already on its way to someone else."
TRANSFER_FAILED = "The transfer did not go through, and the caller is still on the line with you. Tell them so."
HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
# What a tool's server or a webhook has no need to see of an assistant: other tools' keys and addresses, its own.
PRIVATE_FIELDS = {"headers", "secret", "credentials", "credentialId", "credentialIds", "url", "server", "value"}


class ToolError(ValueError):
    """A tool definition OpenRing cannot use, or a tool server that failed."""


def destination_key(destination: dict[str, Any]) -> str:
    """What a handoff destination goes by: the assistant's name in a squad, or its id."""
    inline = destination.get("assistant") if isinstance(destination.get("assistant"), dict) else {}
    return str(destination.get("assistantName") or inline.get("name") or destination.get("assistantId") or "")


def tool_name(tool: dict[str, Any]) -> str:
    """The function name the model sees."""
    if tool.get("type") == "handoff":
        if name := (tool.get("function") or {}).get("name"):
            return str(name)
        destinations = [d for d in tool.get("destinations") or [] if isinstance(d, dict)]
        if len(destinations) == 1:  # so each of several handoff tools has a name of its own
            return ("handoff_to_" + re.sub(r"[^A-Za-z0-9_-]+", "_", destination_key(destinations[0])))[:MAX_NAME_CHARS]
        return "handoff"
    if tool.get("type") == "function":
        return str((tool.get("function") or {}).get("name") or "")
    if tool.get("type") == "apiRequest":
        return str(tool.get("name") or (tool.get("function") or {}).get("name") or "")
    if tool.get("type") == "query":
        return str((tool.get("function") or {}).get("name") or "knowledge-search")
    return str(tool.get("type"))  # Vapi exposes endCall and transferCall under their type names


def _check_name(name: str, where: str) -> None:
    if not name:
        raise ToolError(f"{where} is required")
    if not name.replace("_", "").replace("-", "").isalnum():
        raise ToolError(f"{where} may only have letters, digits, '_' and '-'")
    if len(name) > MAX_NAME_CHARS:
        raise ToolError(f"{where} has at most {MAX_NAME_CHARS} characters")


def _check_api_request(tool: dict[str, Any]) -> None:
    _check_name(tool_name(tool), "an apiRequest tool's name")
    if tool.get("credentialId"):
        raise ToolError("credentialId is not available here: put the key in the tool's headers, which stay on "
                        'your server: {"type": "object", "properties": {"Authorization": {"type": "string", '
                        '"value": "Bearer ..."}}}')
    if str(tool.get("method") or "").upper() not in METHODS:
        raise ToolError(f"an apiRequest tool's method is one of {', '.join(METHODS)}")
    url = str(tool.get("url") or "")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ToolError("an apiRequest tool's url is not a valid address") from exc
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ToolError("an apiRequest tool's url must start with https:// or http://")
    if "{{" in parts.netloc:  # a caller's data must never choose where the request goes
        raise ToolError("an apiRequest tool's url can use {{variables}} in its path and query, not in its host")
    timeout = tool.get("timeoutSeconds")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not 1 <= timeout <= 300):
        raise ToolError("an apiRequest tool's timeoutSeconds goes from 1 to 300")
    for field in ("body", "headers"):
        schema = {} if tool.get(field) is None else tool[field]
        if not (isinstance(schema, dict) and isinstance(schema.get("properties") or {}, dict)
                and isinstance(schema.get("required") or [], list)):
            raise ToolError(f"an apiRequest tool's {field} must be a JSON schema object")
    for name, header in ((tool.get("headers") or {}).get("properties") or {}).items():
        if not HEADER_NAME.fullmatch(name) or not isinstance(header, dict):
            raise ToolError(f"an apiRequest tool's headers need a valid name and a value: {name!r}")
    static = tool.get("parameters")
    if static is not None and not (isinstance(static, list)
                                   and all(isinstance(p, dict) and p.get("key") for p in static)):
        raise ToolError('an apiRequest tool\'s parameters must be a list of {"key": ..., "value": ...}')


def check_tool(tool: dict[str, Any]) -> None:
    """Refuse a tool that cannot work; unknown types are allowed (and skipped on calls)."""
    kind = tool.get("type")
    if not kind:
        raise ToolError("tool.type is required, e.g. 'function', 'apiRequest', 'endCall' or 'transferCall'")
    if kind == "function":
        fn = tool.get("function") or {}
        if not fn.get("name"):
            raise ToolError("a function tool needs function.name")
        if not str(fn["name"]).replace("_", "").replace("-", "").isalnum():
            raise ToolError("function.name may only have letters, digits, '_' and '-'")
    server = tool.get("server") if isinstance(tool.get("server"), dict) else {}
    for timeout in (tool.get("timeoutSeconds"), server.get("timeoutSeconds")):
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))):
            raise ToolError("a tool's timeoutSeconds must be a number")
    if kind == "apiRequest":
        _check_api_request(tool)
    if kind == "handoff":
        _check_name(tool_name(tool), "a handoff tool's function.name")
        destinations = tool.get("destinations")
        if not isinstance(destinations, list) or not destinations:
            raise ToolError("a handoff tool needs destinations: the assistants it can hand the call to")
        for destination in destinations:
            if not isinstance(destination, dict) or destination.get("type", "assistant") != "assistant":
                raise ToolError("handoff destinations of type 'assistant' only for now")
            if not destination_key(destination):
                raise ToolError("a handoff destination needs assistantName, assistantId or assistant")
    if kind == "query":
        _check_name(tool_name(tool), "a query tool's function.name")
        bases = tool.get("knowledgeBases")
        if not isinstance(bases, list) or not bases:
            raise ToolError("a query tool needs knowledgeBases: a list with the fileIds to search")
        try:
            for base in bases:
                knowledge.check(base)
        except knowledge.KnowledgeError as exc:
            raise ToolError(str(exc)) from exc
    if kind == "transferCall":
        if not any(d.get("number") for d in tool.get("destinations") or []):
            raise ToolError("a transferCall tool needs destinations with a phone number")


def assistant_tools(assistant: dict[str, Any], store: Store) -> list[dict[str, Any]]:
    """The tools a call can use: `model.tools` plus the saved tools in `model.toolIds`."""
    model = assistant.get("model") or {}
    tools = list(model.get("tools") or [])
    if base_id := model.get("knowledgeBaseId"):
        if base := store.get("knowledge_bases", str(base_id)):
            tools.append({"type": "query", "knowledgeBases": [base]})
        else:
            logger.warning("knowledge base {} was deleted; the call runs without it", base_id)
    for tool_id in model.get("toolIds") or []:
        if doc := store.get("tools", str(tool_id)):
            tools.append(doc)
        else:
            logger.warning("tool {} was deleted; the call runs without it", tool_id)
    usable = []
    for tool in tools:
        if tool.get("type") not in SUPPORTED:
            logger.warning("tool type '{}' is not supported yet; the call runs without it", tool.get("type"))
            continue
        try:
            check_tool(tool)
        except ToolError as exc:  # an import keeps what the other account has, and only reports what's wrong with it
            logger.warning("tool '{}' cannot work; the call runs without it: {}", tool_name(tool), exc)
            continue
        if tool_name(tool) in {tool_name(t) for t in usable}:
            continue  # the model needs unique names
        usable.append(tool)
    return usable


def function_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """name, description and JSON-schema parameters, as the model sees the tool."""
    if tool["type"] == "function":
        fn = tool["function"]
        params = fn.get("parameters") or {}
        return {
            "name": fn["name"],
            "description": fn.get("description") or "",
            "properties": params.get("properties") or {},
            "required": list(params.get("required") or []),
        }
    if tool["type"] == "apiRequest":  # Vapi derives what the model fills in from the body
        body = tool.get("body") or {}
        return {
            "name": tool_name(tool),
            "description": str(tool.get("description") or (tool.get("function") or {}).get("description") or ""),
            "properties": _fillable(tool),
            "required": [name for name in body.get("required") or [] if name in _fillable(tool)],
        }
    if tool["type"] == "handoff":
        fn = tool.get("function") or {}
        params = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {}
        destinations = tool["destinations"]
        options = "; ".join(f"{destination_key(d)}: {d.get('description') or 'no description'}" for d in destinations)
        extra = {k: v for k, v in (params.get("properties") or {}).items() if k != "destination"}
        return {
            "name": tool_name(tool),
            "description": fn.get("description")
            or f"Hand the caller to another assistant, when they are the right one for this. Options: {options}",
            "properties": {"destination": {"type": "string", "enum": [destination_key(d) for d in destinations],
                                           "description": "Who takes over the call."}, **extra},
            "required": ["destination", *[k for k in params.get("required") or [] if k != "destination"]],
        }
    if tool["type"] == "query":
        fn = tool.get("function") or {}
        about = "; ".join(str(b["description"]) for b in tool.get("knowledgeBases") or [] if b.get("description"))
        return {
            "name": tool_name(tool),
            "description": fn.get("description") or "Look up facts in the company's documents before answering a "
            "question about them." + (f" They hold: {about}" if about else ""),
            "properties": {"query": {"type": "string", "description": "The main words of what to find, in the "
                                                                      "documents' language, with synonyms."}},
            "required": ["query"],
        }
    if tool["type"] == "endCall":
        return {
            "name": "endCall",
            "description": (tool.get("function") or {}).get("description")
            or "End the call. Use it after saying goodbye, when the conversation is over.",
            "properties": {},
            "required": [],
        }
    destinations = [d for d in tool.get("destinations") or [] if d.get("number")]
    options = "; ".join(f"{d['number']}: {d.get('description') or 'no description'}" for d in destinations)
    return {
        "name": "transferCall",
        "description": (tool.get("function") or {}).get("description")
        or f"Transfer the caller to a person or another line. Destinations: {options}",
        "properties": {
            "destination": {
                "type": "string",
                "enum": [d["number"] for d in destinations],
                "description": "The number to transfer the call to.",
            }
        },
        "required": ["destination"],
    }


def _message(tool: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for message in tool.get("messages") or []:
        if isinstance(message, dict) and message.get("type") == kind and message.get("content"):
            return message
    return None


def tool_message(tool: dict[str, Any], kind: str) -> str | None:
    """What the assistant says at a moment of the tool call, e.g. 'request-start' or 'request-failed'."""
    message = _message(tool, kind)
    return str(message["content"]) if message else None


def _fillable(tool: dict[str, Any]) -> dict[str, Any]:
    """The fields of an apiRequest's body the model fills in. One with a `value` is yours, like a header."""
    properties = (tool.get("body") or {}).get("properties") or {}
    return {name: p for name, p in properties.items() if not (isinstance(p, dict) and "value" in p)}


def without_secrets(doc: Any) -> Any:
    """A call or an assistant as a tool's server or a webhook sees it: one tool's key is none of another's business."""
    if isinstance(doc, list):
        return [without_secrets(item) for item in doc]
    if not isinstance(doc, dict):
        return doc
    return {key: value if key == "variableValues" else without_secrets(value) for key, value in doc.items()
            if key not in PRIVATE_FIELDS and not (key == "parameters" and isinstance(value, list))}


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def tool_calls_message(
    tool: dict[str, Any], tool_call_id: str, arguments: dict[str, Any], *, call: dict | None, assistant: dict | None
) -> dict[str, Any]:
    """Vapi's server message for one tool call."""
    tool_call = {"id": tool_call_id, "type": "function", "function": {"name": tool_name(tool), "arguments": arguments}}
    return {
        "message": {
            "type": "tool-calls",
            "timestamp": int(time.time() * 1000),
            "toolCallList": [tool_call],
            "toolWithToolCallList": [{**tool, "toolCall": tool_call}],
            "call": without_secrets(call),
            "assistant": without_secrets(assistant),
        }
    }


def _result_text(body: Any, tool_call_id: str, raw: str) -> str:
    """The text the model reads back, from Vapi's {"results": [{"toolCallId", "result" | "error"}]}."""
    if isinstance(body, dict) and isinstance(body.get("results"), list):
        results = [r for r in body["results"] if isinstance(r, dict)]
        match = next((r for r in results if r.get("toolCallId") == tool_call_id), results[0] if results else None)
        if match is None:
            return "The tool returned no result."
        if match.get("error") is not None:
            raise ToolError(str(match["error"]))
        result = match.get("result")
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return raw.strip() or "Done."


async def call_server(
    tool: dict[str, Any],
    tool_call_id: str,
    arguments: dict[str, Any],
    *,
    default_url: str | None,
    call: dict | None = None,
    assistant: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Run a function tool on its server and return what the model should read."""
    server = tool.get("server") or {}
    url = server.get("url") or default_url
    if not url:
        raise ToolError(f"tool '{tool_name(tool)}' has no server.url, and the assistant has none either")
    headers = {str(k): str(v) for k, v in (server.get("headers") or {}).items()}
    if server.get("secret"):
        headers.setdefault("X-Vapi-Secret", str(server["secret"]))  # the header Vapi servers check
    # The model reads these errors and may say them to the caller: they never contain the URL, which often
    # carries a secret (a webhook path). Logs get the scheme and host only.
    try:
        await netguard.check_url_async(url)
    except netguard.UnsafeURL as exc:
        raise ToolError(f"the tool's server address is refused ({exc})") from exc
    payload = tool_calls_message(tool, tool_call_id, arguments, call=call, assistant=assistant)
    timeout = float(server.get("timeoutSeconds") or DEFAULT_TIMEOUT)
    owned = client is None
    client = client or netguard.client()
    try:
        r = await client.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ToolError(f"could not reach the tool's server ({exc.__class__.__name__})") from exc
    finally:
        if owned:
            await client.aclose()
    if r.status_code >= 400:
        raise ToolError(f"the tool's server answered HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        body = None
    return _result_text(body, tool_call_id, r.text)


def _one_line(value: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", value)


def _in_url(value: str) -> str:
    """A variable's value in a URL, unable to change what it means. `..` would climb the path: httpx resolves it."""
    if value and not value.strip("."):
        raise ToolError("a {{variable}} in the tool's url has no usable value")
    return quote(value, safe="")


def _with_query(url: str, arguments: dict[str, Any]) -> str:
    """Where a GET or DELETE, which have no body, carries what the model filled in. The URL's own query wins."""
    parts = urlsplit(url)
    fixed = {key for key, _ in parse_qsl(parts.query, keep_blank_values=True)}
    values = {k: v if isinstance(v, str) else json.dumps(v) for k, v in arguments.items()
              if v is not None and k not in fixed}
    query = "&".join(q for q in (parts.query, urlencode(values)) if q)
    return urlunsplit(parts._replace(query=query))


async def call_api(
    tool: dict[str, Any],
    arguments: dict[str, Any],
    *,
    call: dict | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Send an apiRequest tool's request and return what the model should read.

    The model only fills in what the body declares: a caller can talk it into other fields. The URL, the headers and
    the `parameters` (which win over the model's values) are yours. Errors never contain the URL or headers, which
    often carry secrets: the model reads them and may say them to the caller. Nothing is retried or followed, since a
    repeated POST could book something twice.
    """
    _check_api_request(tool)  # an import keeps what the other account has: its data must not choose the host either
    context = overrides.call_context(call)
    url = overrides.fill(str(tool["url"]), context, _in_url)
    method = str(tool["method"]).upper()
    # A value stays on one line, so a caller's data can't add headers; UTF-8, so "João" doesn't break the request.
    headers = {str(k): overrides.fill(str(p["value"]), context, _one_line).encode("utf-8")
               for k, p in ((tool.get("headers") or {}).get("properties") or {}).items()
               if isinstance(p, dict) and p.get("value") is not None}
    body = (tool.get("body") or {}).get("properties") or {}
    fixed = [{"key": name, "value": p["value"]} for name, p in body.items() if name not in _fillable(tool)]
    static = {str(p["key"]): overrides.fill(p["value"], context) if isinstance(p.get("value"), str) else p.get("value")
              for p in [*fixed, *(tool.get("parameters") or [])]}
    payload = {**{k: v for k, v in arguments.items() if k in _fillable(tool)}, **static}
    timeout = float(tool.get("timeoutSeconds") or DEFAULT_TIMEOUT)
    send: dict[str, Any] = {"json": payload} if method in ("POST", "PUT", "PATCH") else {}
    if not send:
        url = _with_query(url, payload)
    try:
        await netguard.check_url_async(url)
    except netguard.UnsafeURL as exc:
        raise ToolError("the tool's address is refused: it points to a cloud metadata or link-local address") from exc
    owned = client is None
    client = client or netguard.client()
    received = bytearray()
    try:
        async with asyncio.timeout(timeout):  # the whole exchange: a server that drips out bytes doesn't get longer
            async with client.stream(method, url, headers=headers, timeout=timeout, **send) as r:
                if not 200 <= r.status_code < 300:  # a redirect is not followed: the guard wouldn't have seen it
                    raise ToolError(f"the request failed with HTTP {r.status_code}")
                async for chunk in r.aiter_bytes():
                    received += chunk
                    if len(received) >= MAX_RESPONSE_BYTES:
                        break
    except ToolError:
        raise
    except TimeoutError as exc:
        raise ToolError(f"no answer after {timeout:g} s, so it may or may not have gone through") from exc
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        raise ToolError(f"could not send the request ({exc.__class__.__name__})") from exc
    finally:
        if owned:
            await client.aclose()
    if b"\0" in received:
        return "Done. The answer is not text."
    text = bytes(received).decode("utf-8", "replace").strip()
    if len(text) > MAX_RESULT_CHARS or len(received) >= MAX_RESPONSE_BYTES:
        return text[:MAX_RESULT_CHARS] + " [answer cut short]"
    return text or "Done."


async def _just_run(work: Coroutine[Any, Any, None]) -> None:
    await work


@dataclass
class CallControls:
    """What tools can do to the live call; run_call fills these in."""

    speak: Callable[[str], Awaitable[None]]
    end_call: Callable[[], Awaitable[None]]
    # Transfers need a phone line; browser test calls have none.
    transfer: Callable[[str, str | None], Awaitable[None]] | None
    current_call: Callable[[], dict | None]
    assistant: dict[str, Any]
    default_url: str | None
    store: Store | None = None  # where knowledge base files are searched
    messages: Callable[[], list[dict[str, Any]]] = list  # the conversation so far
    # Moves the call to another assistant; answers why it can't, or None. Only calls with more than one assistant.
    hand_off: Callable[[dict[str, Any], dict[str, Any], str | None, str], str | None] | None = None
    # Whether this assistant still has the call. A tool that finishes after a handoff answers no model.
    in_charge: Callable[[], bool] = lambda: True
    # Gives the model something to answer, in the caller's language.
    tell_model: Callable[[str], Awaitable[None]] | None = None
    # Runs what a caller's interruption must not stop, which cancels a tool's handler.
    uninterrupted: Callable[[Coroutine[Any, Any, None]], Awaitable[None]] = _just_run


def make_handler(tool: dict[str, Any], controls: CallControls):
    """The Pipecat function-call handler for one tool."""
    from pipecat.frames.frames import FunctionCallResultProperties

    async def say_when_late() -> None:
        late = _message(tool, "request-response-delayed") or {}
        await asyncio.sleep(float(late.get("timingMilliseconds") or 5000) / 1000)
        if controls.in_charge():
            await controls.speak(str(late["content"]))

    async def run_function(params) -> None:
        if start := tool_message(tool, "request-start"):
            await controls.speak(start)
        late = asyncio.ensure_future(say_when_late()) if _message(tool, "request-response-delayed") else None
        try:
            if tool["type"] == "apiRequest":
                result = await call_api(tool, _arguments(params.arguments), call=controls.current_call())
            elif tool["type"] == "query":
                result = await knowledge.answer(
                    tool, str(_arguments(params.arguments).get("query") or ""), store=controls.store,
                    messages=controls.messages(), call=without_secrets(controls.current_call()),
                )
            else:
                result = await call_server(
                    tool,
                    params.tool_call_id,
                    _arguments(params.arguments),
                    default_url=controls.default_url,
                    call=controls.current_call(),
                    assistant=controls.assistant,
                )
        except Exception as exc:  # whatever it was, the model must hear, or the caller waits in silence for the timeout
            explained = isinstance(exc, (ToolError, knowledge.KnowledgeError))
            if explained:
                logger.warning("tool {} failed: {}", tool_name(tool), exc)
            else:  # a bug: its text might carry what the model shouldn't read
                logger.exception("tool {} failed", tool_name(tool))
            what = f"The tool failed: {exc}." if explained else "The tool failed."
            result = f"{what} Tell the caller you could not complete it."
            failed = _message(tool, "request-failed")
            if failed and failed.get("role") == "system":  # written for the model: never read out to the caller
                result = f"{result} {failed['content']}"
            elif failed and controls.in_charge():
                await controls.speak(str(failed["content"]))
                result = f"{what} The caller was just told, so don't apologize again."
        finally:
            if late is not None:
                late.cancel()
        if controls.in_charge():
            await params.result_callback(result)
        else:  # for the record; the next assistant wouldn't know why it's being asked to speak
            await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=False))

    async def end_call(params) -> None:
        if start := tool_message(tool, "request-start") or controls.assistant.get("endCallMessage"):
            await controls.speak(start)
        await params.result_callback("The call is ending.", properties=FunctionCallResultProperties(run_llm=False))
        await controls.end_call()

    async def transfer_call(params) -> None:
        if not controls.in_charge():  # the caller spoke while the other line rings, and the model asked again
            await params.result_callback(HANDOFF_UNDER_WAY, properties=FunctionCallResultProperties(run_llm=False))
            return
        destinations = [d for d in tool.get("destinations") or [] if d.get("number")]
        wanted = str(_arguments(params.arguments).get("destination") or "")
        destination = next((d for d in destinations if d["number"] == wanted), None)
        if destination is None and len(destinations) == 1:
            destination = destinations[0]
        if destination is None:
            await params.result_callback(f"Unknown destination '{wanted}'. Choose one of the listed numbers.")
            return
        if controls.transfer is None:
            await params.result_callback(
                "This line can't transfer calls. Tell the caller you cannot transfer."
            )
            return
        await params.result_callback(
            "Transferring the call.", properties=FunctionCallResultProperties(run_llm=False)
        )
        message = destination.get("message") or tool_message(tool, "request-start")

        async def move() -> None:
            try:
                await controls.transfer(destination["number"], message)
            except Exception as exc:  # the caller is still here, waiting to hear something
                logger.warning("the transfer to {} failed: {}", destination["number"], exc)
                failed = _message(tool, "request-failed")
                if failed and failed.get("role") != "system":
                    await controls.speak(str(failed["content"]))
                elif controls.tell_model is not None:
                    await controls.tell_model(f"{TRANSFER_FAILED} {failed['content']}" if failed else TRANSFER_FAILED)

        await controls.uninterrupted(move())

    async def hand_off(params) -> None:
        arguments = _arguments(params.arguments)
        wanted = str(arguments.pop("destination", "") or "")
        destinations = tool.get("destinations") or []
        destination = next((d for d in destinations if destination_key(d) == wanted), None)
        if destination is None and len(destinations) == 1:
            destination = destinations[0]
        if destination is None:
            await params.result_callback(f"Unknown destination '{wanted}'. Choose one of the listed assistants.")
            return
        # These come from the caller's words and land in the next assistant's system prompt: only what's declared.
        declared = ((tool.get("function") or {}).get("parameters") or {}).get("properties") or {}
        arguments = {k: v[:MAX_HANDOFF_VALUE_CHARS] if isinstance(v, str) else v for k, v in arguments.items()
                     if k in declared and isinstance(v, (str, int, float, bool))}
        message = destination.get("message") or tool_message(tool, "request-start")
        problem = "No other assistant is on this call." if controls.hand_off is None \
            else controls.hand_off(destination, arguments, message, params.tool_call_id)
        if problem == HANDOFF_UNDER_WAY:  # it's on its way out: nothing more from this assistant
            await params.result_callback(problem, properties=FunctionCallResultProperties(run_llm=False))
            return
        if problem:
            await params.result_callback(f"{problem} Carry on with the caller yourself.")
            return
        await params.result_callback(f"The call went to {destination_key(destination)}.",
                                     properties=FunctionCallResultProperties(run_llm=False))

    return {"function": run_function, "apiRequest": run_function, "query": run_function, "endCall": end_call,
            "transferCall": transfer_call, "handoff": hand_off}[tool["type"]]


def register_tools(llm: Any, tools: list[dict[str, Any]], controls: CallControls) -> Any:
    """Give the model its tools; returns the ToolsSchema for the call's LLM context (None without tools)."""
    if not tools:
        return None
    from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
    from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema

    schemas = []
    for tool in tools:
        schemas.append(FunctionSchema(**function_schema(tool)))
        timeout = float(tool.get("timeoutSeconds") or (tool.get("server") or {}).get("timeoutSeconds")
                        or DEFAULT_TIMEOUT) + 5
        # Vapi's async tools don't hold the conversation while the server works. A caller who interrupts doesn't
        # cancel an apiRequest either: it may already have gone through, and the model would just send it again.
        llm.register_function(
            tool_name(tool),
            make_handler(tool, controls),
            cancel_on_interruption=not tool.get("async") and tool["type"] not in ("apiRequest", "handoff"),
            timeout_secs=timeout,
        )
    return ToolsSchema(standard_tools=schemas)
