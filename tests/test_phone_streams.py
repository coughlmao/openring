"""A whole call over a Plivo or a Vonage stream, through Pipecat's real serializers and transport: what the voice
makes reaches the line in the provider's own format, and what the caller says reaches speech-to-text. Only
speech-to-text, the model and the voice are stubs."""

import base64
import json
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlsplit

import pytest
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameProcessor
from test_plivo import FORM, hook
from test_plivo import app as plivo_app
from test_vonage import QUERY, signed
from test_vonage import app as vonage_app

from openring import bot


def _wait(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def heard(monkeypatch):
    """Speech-to-text, model and voice that say their first message out loud, and keep what they hear."""
    frames = []

    def speech(rate):
        class Layer(FrameProcessor):
            openring_label, openring_price = "stub", ("model", "none", None, None)

            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                if isinstance(frame, InputAudioRawFrame):
                    frames.append(frame)
                if isinstance(frame, TTSSpeakFrame):
                    for _ in range(10):  # 200 ms of a tone
                        await self.push_frame(OutputAudioRawFrame(b"\x10\x20" * (rate // 50), sample_rate=rate,
                                                                  num_channels=1))
                    return
                await self.push_frame(frame, direction)

        return Layer

    monkeypatch.setattr(bot, "build_services", lambda plan: (speech(plan.rate)(), speech(plan.rate)(),
                                                             speech(plan.rate)()))
    return frames


@pytest.fixture(autouse=True)
def _rate(monkeypatch):
    """The stubs speak at the rate the call runs at."""
    from openring.providers import StackPlan

    monkeypatch.setattr(StackPlan, "rate", 8000, raising=False)
    real = bot.run_call

    async def run_call(transport, *, plan, call_id, store, sample_rate=None, **kwargs):
        plan.rate = sample_rate or 16000
        return await real(transport, plan=plan, call_id=call_id, store=store, sample_rate=sample_rate, **kwargs)

    monkeypatch.setattr(bot, "run_call", run_call)


def _short(client, store):
    """The assistant gives up after 10 s, so a stream that doesn't work fails the test instead of hanging it."""
    assistant = store.list("assistants")[0]
    store.update("assistants", assistant["id"], {"maxDurationSeconds": 10})


def test_a_plivo_call_talks_and_listens(monkeypatch, heard):
    client, store, _, _ = plivo_app(monkeypatch)
    _short(client, store)
    stream = ET.fromstring(hook(client, "/plivo/voice", form=FORM).text).find("./Stream")
    call_id = parse_qs(urlsplit(stream.text).query)["callId"][0]
    silence = base64.b64encode(b"\xff" * 160).decode()  # 20 ms of mu-law silence

    with client.websocket_connect(f"/ws/plivo?callId={call_id}") as ws:
        ws.send_text(json.dumps({"event": "start", "sequenceNumber": 1, "start": {"streamId": "st-1",
                                                                                 "callId": "uuid-1"},
                                 "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}}))
        for _ in range(5):
            ws.send_text(json.dumps({"event": "media", "streamId": "st-1", "media": {
                "track": "inbound", "timestamp": "20", "chunk": 1, "payload": silence}}))
        played = []
        while not played:
            message = json.loads(ws.receive_text())
            if message["event"] == "playAudio":
                played.append(message)
        assert played[0]["media"]["contentType"] == "audio/x-mulaw" and played[0]["media"]["sampleRate"] == 8000
        assert played[0]["streamId"] == "st-1" and len(base64.b64decode(played[0]["media"]["payload"])) >= 160
    assert _wait(lambda: heard) and heard[0].sample_rate == 8000 and heard[0].num_channels == 1
    assert _wait(lambda: store.get("calls", call_id)["status"] == "ended")
    assert store.get("calls", call_id)["endedReason"] == "customer-ended-call"


def test_a_vonage_call_talks_and_listens(monkeypatch, heard):
    hung_up = []

    async def hang_up(*_args):
        hung_up.append(_args[-1])

    monkeypatch.setattr("openring.vonage.hang_up", hang_up)
    client, store, _, _ = vonage_app(monkeypatch)
    _short(client, store)
    answered = client.get(f"/vonage/voice?{QUERY}", headers=signed()).json()
    call_id = parse_qs(urlsplit(answered[0]["endpoint"][0]["uri"]).query)["callId"][0]

    with client.websocket_connect(f"/ws/vonage?callId={call_id}") as ws:
        ws.send_text(json.dumps({"event": "websocket:connected", "content-type": "audio/l16;rate=16000"}))
        for _ in range(5):
            ws.send_bytes(b"\x00\x00" * 320)  # 20 ms of 16 kHz silence
        packets = []
        while len(packets) < 3:
            message = ws.receive()
            if message.get("bytes") is not None:
                packets.append(message["bytes"])
        assert all(len(p) == 640 for p in packets)  # 20 ms at a time, as Vonage expects
    assert _wait(lambda: heard) and heard[0].sample_rate == 16000 and heard[0].num_channels == 1
    assert _wait(lambda: store.get("calls", call_id)["status"] == "ended")
    assert store.get("calls", call_id)["endedReason"] == "customer-ended-call"
    assert hung_up == ["leg-1"]
