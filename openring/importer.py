"""Copy assistants, tools and phone numbers from any Vapi-compatible API into this server.

Point it at an account's base URL and private key. Documents keep their JSON, so nothing is lost:
fields OpenRing doesn't use yet stay in the document and start working when support lands. Each
imported document remembers where it came from (`importedFrom`), so importing again updates it
instead of creating a copy.
"""

from __future__ import annotations

from typing import Any

import httpx

from . import netguard
from .providers import ProviderError, resolve_plan
from .store import Store, is_phone_number
from .tools import SUPPORTED, ToolError, check_tool

PAGE = 100
IMPORTED_LINES = ("twilio", "telnyx", "vonage")  # Vapi's other kinds are its own numbers and SIP trunks
# Fields the source server owns, or secrets that should not be copied.
DROP = {"id", "orgId", "createdAt", "updatedAt", "isServerUrlSecretSet",
        "twilioAuthToken", "twilioApiSecret", "credentialId", "credentialIds"}


class SourceError(ValueError):
    """The source API refused the request or could not be reached."""


async def _list_all(client: httpx.AsyncClient, base_url: str, path: str) -> list[dict[str, Any]]:
    """Every document of a collection, newest first, paging back by creation time."""
    items: list[dict[str, Any]] = []
    params: dict[str, Any] = {"limit": PAGE}
    while True:
        try:
            r = await client.get(f"{base_url}/{path}", params=params)
        except httpx.HTTPError as exc:
            raise SourceError(f"could not reach {base_url}: {exc.__class__.__name__}") from exc
        if r.status_code in (401, 403):
            raise SourceError("the API key was refused; use the account's private key")
        if r.status_code >= 400:
            raise SourceError(f"{base_url}/{path} answered HTTP {r.status_code}")
        page = r.json()
        if not isinstance(page, list):
            raise SourceError(f"{base_url}/{path} did not return a list; is this a Vapi-compatible API?")
        items.extend(d for d in page if isinstance(d, dict))
        oldest = min((d.get("createdAt") for d in page if d.get("createdAt")), default=None)
        if len(page) < PAGE or not oldest:
            return items
        params = {"limit": PAGE, "createdAtLt": oldest}


def _clean(doc: dict[str, Any], source: str) -> dict[str, Any]:
    clean = {k: v for k, v in doc.items() if k not in DROP}
    clean["importedFrom"] = {"baseUrl": source, "id": doc.get("id")}
    return clean


def _upsert(store: Store, collection: str, doc: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Create the document, or update the one an earlier import made. Returns (doc, created)."""
    source = doc["importedFrom"]
    for existing in store.list(collection, limit=100_000):
        if existing.get("importedFrom") == source:
            # Keys the source no longer has are cleared, so the copy matches it.
            stale = {k: None for k in existing if k not in doc and k not in ("id", "createdAt", "updatedAt")}
            return store.update(collection, existing["id"], {**stale, **doc}), False
    return store.create(collection, doc), True


def _assistant_problems(assistant: dict[str, Any]) -> list[str]:
    """What keeps an imported assistant from taking calls here yet."""
    problems = []
    try:
        resolve_plan(assistant, check_keys=False)
    except ProviderError as exc:
        problems.append(str(exc))
    if (assistant.get("artifactPlan") or {}).get("recordingEnabled") is True:
        problems.append("it records its calls on this server: make sure callers are told, or turn it off")
    for tool in (assistant.get("model") or {}).get("tools") or []:
        try:
            check_tool(tool)
        except ToolError as exc:
            problems.append(f"tool: {exc}")
        if tool.get("type") not in SUPPORTED:
            problems.append(f"tool type '{tool.get('type')}' is not supported yet; calls run without it")
    return problems


async def import_account(
    base_url: str, api_key: str, store: Store, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    """Import every tool, assistant and phone number of the account; returns what happened to each."""
    base_url = base_url.rstrip("/")
    if not base_url.startswith(("https://", "http://")):
        raise SourceError("baseUrl must start with https://")
    if not api_key:
        raise SourceError("apiKey is required: the account's private API key")
    try:
        await netguard.check_url_async(base_url)
    except netguard.UnsafeURL as exc:
        raise SourceError(str(exc)) from exc
    owned = client is None
    client = client or netguard.client(timeout=30)
    client.headers["Authorization"] = f"Bearer {api_key}"
    try:
        tools = await _list_all(client, base_url, "tool")
        assistants = await _list_all(client, base_url, "assistant")
        numbers = await _list_all(client, base_url, "phone-number")
        try:
            squads = await _list_all(client, base_url, "squad")
        except SourceError:  # not every Vapi-compatible API has squads
            squads = []
    finally:
        if owned:
            await client.aclose()

    report: dict[str, Any] = {"tools": [], "assistants": [], "squads": [], "phoneNumbers": []}
    tool_ids: dict[str, str] = {}
    for tool in tools:
        doc, created = _upsert(store, "tools", _clean(tool, base_url))
        tool_ids[str(tool.get("id"))] = doc["id"]
        problems = []
        try:
            check_tool(doc)
        except ToolError as exc:
            problems.append(str(exc))
        if doc.get("type") not in SUPPORTED:
            problems.append(f"tool type '{doc.get('type')}' is not supported yet")
        if doc.get("type") == "apiRequest" and tool.get("credentialId"):
            problems.append("its credential was not copied: put the key in the tool's headers")
        if doc.get("type") == "query":
            problems.append("its files were not copied: upload them here and list the new ids in fileIds")
        report["tools"].append({"id": doc["id"], "name": _tool_label(doc), "created": created, "problems": problems})

    assistant_ids: dict[str, str] = {}
    for assistant in assistants:
        clean = _clean(assistant, base_url)
        model = dict(clean.get("model") or {})
        if model.get("toolIds"):
            model["toolIds"] = [tool_ids[str(t)] for t in model["toolIds"] if str(t) in tool_ids]
            clean["model"] = model
        doc, created = _upsert(store, "assistants", clean)
        assistant_ids[str(assistant.get("id"))] = doc["id"]
        report["assistants"].append({"id": doc["id"], "name": doc.get("name") or "Untitled", "created": created,
                                     "problems": _assistant_problems(doc)})

    # Handoffs name assistants by id, so they can only be pointed at the copies now.
    for collection in ("tools", "assistants"):
        for doc in store.list(collection, limit=100_000):
            if doc.get("importedFrom") and (mapped := _mapped(doc, assistant_ids)) != doc:
                store.update(collection, doc["id"], mapped)
    squad_ids: dict[str, str] = {}
    for squad in squads:
        clean = _mapped(_clean(squad, base_url), assistant_ids)
        doc, created = _upsert(store, "squads", clean)
        squad_ids[str(squad.get("id"))] = doc["id"]
        missing = [m["assistantId"] for m in doc.get("members") or []
                   if isinstance(m, dict) and m.get("assistantId") and not store.get("assistants", m["assistantId"])]
        report["squads"].append({"id": doc["id"], "name": doc.get("name") or "Untitled", "created": created,
                                 "problems": [f"assistant {m} was not imported" for m in missing]})

    for number in numbers:
        provider = str(number.get("provider") or "")
        label = number.get("number") or number.get("sipUri") or number.get("name") or number.get("id")
        if provider in IMPORTED_LINES and not is_phone_number(number.get("number")):
            report["phoneNumbers"].append({"number": label, "skipped": "not a phone number with its country code"})
            continue
        if provider not in IMPORTED_LINES or not number.get("number"):
            reason = f"{provider or 'these'} numbers are not supported yet"
            report["phoneNumbers"].append({"number": label, "skipped": reason})
            continue
        clean = _clean(number, base_url)
        clean["assistantId"] = assistant_ids.get(str(number.get("assistantId")))
        clean["squadId"] = squad_ids.get(str(number.get("squadId")))
        existing = store.find_phone_number(number["number"])
        if existing and existing.get("importedFrom") != clean["importedFrom"]:
            report["phoneNumbers"].append({"number": label, "skipped": "already on this server"})
            continue
        doc, created = _upsert(store, "phone_numbers", clean)
        report["phoneNumbers"].append({"id": doc["id"], "number": label, "created": created})
    return report


def _mapped(value: Any, assistant_ids: dict[str, str]) -> Any:
    """A document with its `assistantId`s turned into the ids of the copies here."""
    if isinstance(value, list):
        return [_mapped(item, assistant_ids) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: assistant_ids.get(str(item), item) if key == "assistantId" else _mapped(item, assistant_ids)
            for key, item in value.items()}


def _tool_label(tool: dict[str, Any]) -> str:
    return str((tool.get("function") or {}).get("name") or tool.get("name") or tool.get("type") or "tool")
