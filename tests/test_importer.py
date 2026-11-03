"""Importing an account from a Vapi-compatible API. The source API is a stub served through httpx."""

import asyncio
import json

import httpx
import pytest

from openring import importer
from openring.importer import SourceError, import_account
from openring.store import Store

from test_api import AUTH, make_client

SOURCE = "https://api.example-voice.com"
TOOL = {"id": "t-1", "orgId": "org", "type": "function", "createdAt": "2026-10-01T00:00:00Z",
        "function": {"name": "book", "parameters": {"type": "object", "properties": {}}},
        "server": {"url": "https://clinic.example.com/tools"}}
ODD_TOOL = {"id": "t-2", "type": "dtmf", "createdAt": "2026-10-01T00:00:00Z"}
ASSISTANT = {
    "id": "a-1", "orgId": "org", "name": "Front desk", "createdAt": "2026-10-02T00:00:00Z",
    "isServerUrlSecretSet": False, "firstMessage": "Hi!",
    "model": {"provider": "groq", "model": "openai/gpt-oss-20b", "toolIds": ["t-1", "t-gone"],
              "messages": [{"role": "system", "content": "Be nice."}]},
    "voice": {"provider": "11labs", "voiceId": "v1"},
    "transcriber": {"provider": "deepgram", "language": "en"},
    "analysisPlan": {"summaryPlan": {"enabled": True}},  # not used here yet, kept in the document
}
UNSUPPORTED = {"id": "a-2", "name": "Fancy", "createdAt": "2026-10-03T00:00:00Z",
               "model": {"provider": "groq", "model": "openai/gpt-oss-20b"},
               "voice": {"provider": "playht", "voiceId": "x"}}
NUMBERS = [
    {"id": "p-1", "provider": "twilio", "number": "+15551234567", "assistantId": "a-1",
     "twilioAccountSid": "AC1", "twilioAuthToken": "secret-token", "createdAt": "2026-10-04T00:00:00Z"},
    {"id": "p-2", "provider": "vonage", "number": "+15557654321", "createdAt": "2026-10-04T00:00:00Z"},
    {"id": "p-3", "provider": "twilio", "number": "not-a-number", "createdAt": "2026-10-04T00:00:00Z"},
]


def source_api(data, seen=None, key="sk-private"):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.headers.get("Authorization") != f"Bearer {key}":
            return httpx.Response(401, json={"message": "Unauthorized"})
        return httpx.Response(200, json=data.get(request.url.path.strip("/"), []))

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def run(store, data, key="sk-private", seen=None):
    return asyncio.run(import_account(SOURCE, key, store, client=source_api(data, seen)))


DATA = {"tool": [TOOL, ODD_TOOL], "assistant": [ASSISTANT, UNSUPPORTED], "phone-number": NUMBERS}


def test_import_copies_tools_assistants_and_twilio_numbers():
    store = Store(":memory:")
    report = run(store, DATA)

    tools = {t["function"]["name"] if t.get("function") else t["type"]: t for t in store.list("tools")}
    assert set(tools) == {"book", "dtmf"} and "orgId" not in tools["book"]
    assert tools["book"]["importedFrom"] == {"baseUrl": SOURCE, "id": "t-1"}

    front = next(a for a in store.list("assistants") if a["name"] == "Front desk")
    assert front["model"]["toolIds"] == [tools["book"]["id"]]  # remapped; the deleted tool is dropped
    assert front["analysisPlan"] == ASSISTANT["analysisPlan"] and "orgId" not in front
    by_name = {a["name"]: a for a in report["assistants"]}
    assert by_name["Front desk"]["problems"] == [] and by_name["Front desk"]["created"] is True
    assert "playht" in by_name["Fancy"]["problems"][0]  # imported anyway, flagged for a fix
    assert next(t for t in report["tools"] if t["name"] == "dtmf")["problems"]

    number = store.find_phone_number("+15551234567")
    assert number["assistantId"] == front["id"] and "twilioAuthToken" not in number
    assert "+15557654321" in [n["number"] for n in store.list("phone_numbers")]  # a Vonage number works here too
    assert {"number": "not-a-number", "skipped": "not a phone number with its country code"} in report["phoneNumbers"]


def test_import_again_updates_instead_of_duplicating():
    store = Store(":memory:")
    run(store, DATA)
    renamed = {**ASSISTANT, "name": "Reception", "firstMessage": None}
    renamed.pop("analysisPlan")
    report = run(store, {**DATA, "assistant": [renamed, UNSUPPORTED]})
    assert len(store.list("assistants")) == 2 and len(store.list("tools")) == 2
    assert len(store.list("phone_numbers")) == 2
    front = next(a for a in store.list("assistants") if a["importedFrom"]["id"] == "a-1")
    assert front["name"] == "Reception" and front.get("analysisPlan") is None
    assert all(not a["created"] for a in report["assistants"])


def test_import_pages_back_through_long_lists(monkeypatch):
    monkeypatch.setattr(importer, "PAGE", 2)
    assistants = [{**UNSUPPORTED, "id": f"a-{i}", "name": f"A{i}", "createdAt": f"2026-10-{20 - i:02d}T00:00:00Z"}
                  for i in range(5)]
    # Newest first; each next page is asked for with createdAtLt = the oldest date seen so far.
    pages = {None: assistants[:2], assistants[1]["createdAt"]: assistants[2:4],
             assistants[3]["createdAt"]: assistants[4:]}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/assistant":
            return httpx.Response(200, json=pages[request.url.params.get("createdAtLt")])
        return httpx.Response(200, json=[])

    store = Store(":memory:")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    report = asyncio.run(import_account(SOURCE, "k", store, client=client))
    assert len(report["assistants"]) == 5 and len(store.list("assistants")) == 5


def test_import_explains_what_went_wrong():
    store = Store(":memory:")
    with pytest.raises(SourceError, match="private key"):
        run(store, DATA, key="wrong")
    with pytest.raises(SourceError, match="apiKey is required"):
        asyncio.run(import_account(SOURCE, "", store))
    with pytest.raises(SourceError, match="https://"):
        asyncio.run(import_account("ftp://x", "k", store))
    assert store.list("assistants") == []


def test_import_endpoint(monkeypatch):
    calls = []

    async def fake_import(base_url, api_key, store):
        calls.append((base_url, api_key))
        return {"tools": [], "assistants": [], "phoneNumbers": []}

    monkeypatch.setattr("openring.api.import_account", fake_import)
    client = make_client()
    assert client.post("/import", json={"apiKey": "k"}).status_code == 401
    r = client.post("/import", json={"apiKey": "sk-1"}, headers=AUTH)
    assert r.status_code == 200 and calls == [("https://api.vapi.ai", "sk-1")]

    async def refused(*_a):
        raise SourceError("the API key was refused; use the account's private key")

    monkeypatch.setattr("openring.api.import_account", refused)
    r = client.post("/import", json={"apiKey": "bad"}, headers=AUTH)
    assert r.status_code == 400 and "private key" in json.loads(r.text)["detail"]


def test_an_imported_api_request_tool_says_its_credential_stayed_behind():
    store = Store(":memory:")
    tool = {"id": "t-9", "type": "apiRequest", "name": "crm", "method": "POST", "url": "https://crm.example.com/x",
            "credentialId": "cred-1", "createdAt": "2026-10-01T00:00:00Z"}
    report = run(store, {"tool": [tool], "assistant": [], "phone-number": []})
    assert "credential" in report["tools"][0]["problems"][0]
    assert "credentialId" not in store.list("tools")[0]  # never copied


def test_an_imported_assistant_that_records_says_so():
    store = Store(":memory:")
    recording = {**ASSISTANT, "artifactPlan": {"recordingEnabled": True}}
    report = run(store, {"tool": [], "assistant": [recording], "phone-number": []})
    assert any("records its calls" in p for p in report["assistants"][0]["problems"])
