"""Assistant tools in Vapi's format: definitions, the tool-calls request to your server, and the live-call handlers."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from openring import tools
from openring.bot import keep_phone_line_open, transcript_messages, twilio_transport
from openring.config import Settings
from openring.store import Store
from openring.tools import CallControls, ToolError, assistant_tools, call_server, check_tool, function_schema

from test_api import ASSISTANT, AUTH, make_client

BOOK = {
    "type": "function",
    "function": {
        "name": "book_appointment",
        "description": "Book a cleaning",
        "parameters": {
            "type": "object",
            "properties": {"day": {"type": "string"}, "time": {"type": "string"}},
            "required": ["day"],
        },
    },
    "server": {"url": "https://clinic.example.com/tools", "headers": {"X-Clinic": "1"}, "secret": "s3cret"},
    "messages": [
        {"type": "request-start", "content": "One moment, let me check the calendar."},
        {"type": "request-failed", "content": "Sorry, the calendar is not answering."},
    ],
}
END = {"type": "endCall"}
TRANSFER = {
    "type": "transferCall",
    "destinations": [
        {"type": "number", "number": "+5511900000001", "description": "front desk", "message": "Transferring you now."},
        {"type": "number", "number": "+5511900000002", "description": "billing"},
    ],
}


def test_check_tool_refuses_what_cannot_work():
    check_tool(BOOK)
    check_tool(END)
    check_tool(TRANSFER)
    check_tool({"type": "dtmf"})  # not supported yet, but an imported assistant still loads
    for bad in ({}, {"type": "function", "function": {}}, {"type": "function", "function": {"name": "two words"}},
                {"type": "transferCall", "destinations": []}):
        with pytest.raises(ToolError):
            check_tool(bad)


def test_assistant_tools_merges_saved_tools_and_skips_unsupported_ones():
    store = Store(":memory:")
    saved = store.create("tools", END)
    assistant = {"model": {"tools": [BOOK, {"type": "dtmf"}, BOOK], "toolIds": [saved["id"], "deleted-id"]}}
    names = [tools.tool_name(t) for t in assistant_tools(assistant, store)]
    assert names == ["book_appointment", "endCall"]


def test_function_schema_is_what_the_model_sees():
    assert function_schema(BOOK) == {
        "name": "book_appointment",
        "description": "Book a cleaning",
        "properties": {"day": {"type": "string"}, "time": {"type": "string"}},
        "required": ["day"],
    }
    assert function_schema(END)["name"] == "endCall"
    transfer = function_schema(TRANSFER)
    assert transfer["properties"]["destination"]["enum"] == ["+5511900000001", "+5511900000002"]
    assert "front desk" in transfer["description"] and transfer["required"] == ["destination"]


def _server(responder):
    """An httpx client whose requests go to `responder(request)` instead of the network."""
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


def _run(coro):
    return asyncio.run(coro)


def test_call_server_sends_vapis_tool_calls_message():
    client, seen = _server(lambda r: httpx.Response(200, json={"results": [
        {"toolCallId": "call_1", "result": "Booked for Friday 9:00"}]}))
    call = {"id": "c1", "type": "webCall"}
    text = _run(call_server(BOOK, "call_1", {"day": "friday"}, default_url=None, call=call,
                            assistant={"name": "Clinic"}, client=client))
    assert text == "Booked for Friday 9:00"
    request = seen[0]
    assert str(request.url) == "https://clinic.example.com/tools"
    assert request.headers["X-Clinic"] == "1" and request.headers["X-Vapi-Secret"] == "s3cret"
    message = json.loads(request.content)["message"]
    assert message["type"] == "tool-calls"
    assert message["toolCallList"] == [
        {"id": "call_1", "type": "function", "function": {"name": "book_appointment", "arguments": {"day": "friday"}}}]
    assert message["toolWithToolCallList"][0]["toolCall"]["id"] == "call_1"
    assert message["call"] == call and message["assistant"] == {"name": "Clinic"}


def test_call_server_reads_every_answer_shape():
    def ask(responder, tool=BOOK, default_url=None):
        client, seen = _server(responder)
        return _run(call_server(tool, "call_1", {}, default_url=default_url, client=client)), seen

    # A structured result reaches the model as JSON.
    structured = {"results": [{"toolCallId": "call_1", "result": {"slots": [9, 10]}}]}
    assert ask(lambda r: httpx.Response(200, json=structured))[0] == '{"slots": [9, 10]}'
    # A plain-text answer is passed along as it is.
    assert ask(lambda r: httpx.Response(200, text="ok, booked"))[0] == "ok, booked"
    # Without its own server.url the tool uses the assistant's.
    no_url = {**BOOK, "server": {}}
    _, seen = ask(lambda r: httpx.Response(200, json={"results": [{"toolCallId": "call_1", "result": "x"}]}),
                  tool=no_url, default_url="https://hooks.example.com/vapi")
    assert str(seen[0].url) == "https://hooks.example.com/vapi"
    with pytest.raises(ToolError, match="no server.url"):
        ask(lambda r: httpx.Response(200), tool=no_url)
    with pytest.raises(ToolError, match="slot taken"):
        ask(lambda r: httpx.Response(200, json={"results": [{"toolCallId": "call_1", "error": "slot taken"}]}))
    with pytest.raises(ToolError, match="HTTP 500"):
        ask(lambda r: httpx.Response(500))


class FakeLLM:
    def __init__(self):
        self.registered = {}

    def register_function(self, name, handler, **options):
        self.registered[name] = (handler, options)


class Params:
    """What Pipecat hands a function-call handler."""

    def __init__(self, arguments, tool_call_id="call_1"):
        self.arguments = arguments
        self.tool_call_id = tool_call_id
        self.results = []

    async def result_callback(self, result, properties=None):
        self.results.append((result, properties))


def _controls(transfer=True):
    log = []

    async def speak(text):
        log.append(("speak", text))

    async def end_call():
        log.append(("end",))

    async def do_transfer(number, message):
        log.append(("transfer", number, message))

    controls = CallControls(speak=speak, end_call=end_call, transfer=do_transfer if transfer else None,
                            current_call=lambda: {"id": "c1"}, assistant={"name": "Clinic"},
                            default_url="https://hooks.example.com/vapi")
    return controls, log


def test_register_tools_gives_the_model_every_tool():
    llm = FakeLLM()
    schema = tools.register_tools(llm, [BOOK, END, {**TRANSFER, "async": True}], _controls()[0])
    assert sorted(llm.registered) == ["book_appointment", "endCall", "transferCall"]
    assert [f.name for f in schema.standard_tools] == ["book_appointment", "endCall", "transferCall"]
    assert llm.registered["transferCall"][1]["cancel_on_interruption"] is False  # Vapi's async tools
    assert llm.registered["book_appointment"][1]["timeout_secs"] == 25
    assert tools.register_tools(llm, [], _controls()[0]) is None


def test_function_handler_says_its_messages_and_returns_the_result(monkeypatch):
    controls, log = _controls()

    async def fake_call_server(tool, tool_call_id, arguments, **kw):
        assert (tool_call_id, arguments, kw["default_url"], kw["call"]) == (
            "call_1", {"day": "friday"}, "https://hooks.example.com/vapi", {"id": "c1"})
        return "Booked"

    monkeypatch.setattr(tools, "call_server", fake_call_server)
    params = Params('{"day": "friday"}')  # some models send the arguments as a JSON string
    _run(tools.make_handler(BOOK, controls)(params))
    assert log == [("speak", "One moment, let me check the calendar.")]
    assert params.results[0][0] == "Booked"

    async def failing(*_a, **_kw):
        raise ToolError("could not reach the server")

    monkeypatch.setattr(tools, "call_server", failing)
    params = Params({})
    _run(tools.make_handler(BOOK, controls)(params))
    assert log[-1] == ("speak", "Sorry, the calendar is not answering.")
    assert "could not reach the server" in params.results[0][0]


def test_end_call_handler_hangs_up_without_another_answer():
    controls, log = _controls()
    params = Params({})
    _run(tools.make_handler(END, controls)(params))
    assert log == [("end",)]
    assert params.results[0][1].run_llm is False


def test_end_call_says_the_end_call_message_unless_the_tool_has_its_own():
    controls, log = _controls()
    controls.assistant = {"endCallMessage": "Thanks for calling!"}
    _run(tools.make_handler(END, controls)(Params({})))
    assert log == [("speak", "Thanks for calling!"), ("end",)]
    log.clear()
    own = {**END, "messages": [{"type": "request-start", "content": "Bye."}]}
    _run(tools.make_handler(own, controls)(Params({})))
    assert log == [("speak", "Bye."), ("end",)]


def test_transfer_handler_moves_the_call_or_explains_why_not():
    controls, log = _controls()
    params = Params({"destination": "+5511900000001"})
    _run(tools.make_handler(TRANSFER, controls)(params))
    assert log == [("transfer", "+5511900000001", "Transferring you now.")]
    assert params.results[0][1].run_llm is False

    params = Params({"destination": "+19999"})
    _run(tools.make_handler(TRANSFER, controls)(params))
    assert "Unknown destination" in params.results[0][0] and len(log) == 1

    browser, log = _controls(transfer=False)
    params = Params({"destination": "+5511900000002"})
    _run(tools.make_handler(TRANSFER, browser)(params))
    assert "can't transfer" in params.results[0][0] and log == []


def test_a_transfer_that_fails_gets_an_answer():
    told = []

    async def fail(_number, _message):
        raise RuntimeError("busy")

    async def tell(text):
        told.append(text)

    controls, log = _controls()
    controls.transfer, controls.tell_model = fail, tell
    _run(tools.make_handler(TRANSFER, controls)(Params({"destination": "+5511900000001"})))
    assert len(told) == 1 and "did not go through" in told[0] and log == []

    # The tool's own words for it, as with any tool: said to the caller, or, with role system, for the model.
    told.clear()
    said = {**TRANSFER, "messages": [{"type": "request-failed", "content": "That line is busy."}]}
    _run(tools.make_handler(said, controls)(Params({"destination": "+5511900000001"})))
    assert log == [("speak", "That line is busy.")] and told == []
    log.clear()
    written = {**TRANSFER, "messages": [{"type": "request-failed", "role": "system", "content": "Offer a callback."}]}
    _run(tools.make_handler(written, controls)(Params({"destination": "+5511900000001"})))
    assert log == [] and "Offer a callback." in told[0]


def test_a_result_after_the_call_moved_on_wakes_no_model(monkeypatch):
    async def answer(*_args, **_kwargs):
        return "Found"

    monkeypatch.setattr(tools, "call_server", answer)
    controls, log = _controls()
    params = Params({})
    _run(tools.make_handler(BOOK, controls)(params))
    assert params.results == [("Found", None)]  # the assistant is still on: it reads the answer as usual

    controls.in_charge = lambda: False
    params = Params({})
    _run(tools.make_handler(BOOK, controls)(params))
    assert params.results[0][0] == "Found" and params.results[0][1].run_llm is False

    async def fails(*_args, **_kwargs):
        raise tools.ToolError("down")

    monkeypatch.setattr(tools, "call_server", fails)
    said = {**BOOK, "messages": [{"type": "request-failed", "content": "It failed."}]}
    _run(tools.make_handler(said, controls)(Params({})))
    assert ("speak", "It failed.") not in log  # another assistant is talking now: not in this one's words


def test_tools_register_on_the_real_llm_service():
    from pipecat.services.openai.llm import OpenAILLMService

    llm = OpenAILLMService(api_key="fake", settings=OpenAILLMService.Settings(model="gpt-4.1-mini"))
    schema = tools.register_tools(llm, [BOOK, END], _controls()[0])
    assert {"book_appointment", "endCall"} <= set(llm._functions)
    assert len(schema.standard_tools) == 2


def test_transcript_keeps_tool_calls_and_results():
    messages = [
        {"role": "system", "content": "You are a receptionist."},
        {"role": "assistant", "content": "Hi!"},
        {"role": "user", "content": "Friday at nine?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "book_appointment", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": '"Booked"'},  # how Pipecat stores a text result
        {"role": "assistant", "content": "Done, see you Friday."},
    ]
    assert transcript_messages(messages) == [
        {"role": "assistant", "message": "Hi!"},
        {"role": "user", "message": "Friday at nine?"},
        {"role": "tool_calls", "toolCalls": messages[3]["tool_calls"]},
        {"role": "tool_call_result", "toolCallId": "call_1", "result": "Booked"},
        {"role": "assistant", "message": "Done, see you Friday."},
    ]


def test_a_transferred_phone_call_is_not_hung_up(monkeypatch):
    from pipecat.frames.frames import EndFrame

    settings = Settings(twilio_account_sid="AC1", twilio_auth_token="tok", db_path=":memory:")
    transport = twilio_transport(SimpleNamespace(), {"stream_id": "MZ1", "call_id": "CA1"}, settings)
    serializer = transport._params.serializer
    hung_up = []

    async def hang_up():
        hung_up.append(True)

    monkeypatch.setattr(serializer, "_hang_up_call", hang_up)
    keep_phone_line_open(transport)
    _run(serializer.serialize(EndFrame()))
    assert hung_up == []


def test_tool_api_and_assistants_that_use_saved_tools():
    client = make_client()
    assert client.post("/tool", json={"type": "function", "function": {}}, headers=AUTH).status_code == 400
    tool = client.post("/tool", json=BOOK, headers=AUTH)
    assert tool.status_code == 201
    tool_id = tool.json()["id"]
    assert client.get(f"/tool/{tool_id}", headers=AUTH).json()["function"]["name"] == "book_appointment"
    assert [t["id"] for t in client.get("/tool", headers=AUTH).json()] == [tool_id]
    patched = client.patch(f"/tool/{tool_id}", json={"async": True}, headers=AUTH).json()
    assert patched["async"] is True and patched["function"]["name"] == "book_appointment"

    model = {**ASSISTANT["model"], "toolIds": [tool_id], "tools": [END]}
    assert client.post("/assistant", json={**ASSISTANT, "model": model}, headers=AUTH).status_code == 201
    missing = {**ASSISTANT["model"], "toolIds": ["nope"]}
    r = client.post("/assistant", json={**ASSISTANT, "model": missing}, headers=AUTH)
    assert r.status_code == 400 and "nope" in r.json()["detail"]
    broken = {**ASSISTANT["model"], "tools": [{"type": "transferCall"}]}
    assert client.post("/assistant", json={**ASSISTANT, "model": broken}, headers=AUTH).status_code == 400

    assert client.delete(f"/tool/{tool_id}", headers=AUTH).status_code == 200
    assert client.get(f"/tool/{tool_id}", headers=AUTH).status_code == 404
    assert client.get("/tool").status_code == 401
