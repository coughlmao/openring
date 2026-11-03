"""Runs one live call, by phone or from the browser: audio in -> STT -> LLM -> TTS -> audio out."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket
from loguru import logger

from . import overrides, prices, recordings, routes, server_messages, squads
from .config import Settings
from .providers import StackPlan, build_services
from .store import Store, now_iso
from .tools import HANDOFF_UNDER_WAY, CallControls, assistant_tools, register_tools

# Pipecat's named parts of a user-to-bot turn, mapped to Vapi's performanceMetrics fields.
LATENCY_PARTS = {
    "endpointing_wait": "endpointingLatency",
    "turn_detection": "endpointingLatency",
    "transcription": "transcriberLatency",
    "llm_inference": "modelLatency",
    "speech_synthesis": "voiceLatency",
}
LATENCY_FIELDS = (*dict.fromkeys(LATENCY_PARTS.values()), "turnLatency")
# Phone audio is 8 kHz; browser calls keep Pipecat's defaults (16 kHz in, 24 kHz out).
PHONE_SAMPLE_RATE = 8000
WIDEBAND_SAMPLE_RATE = 16000  # Vonage's streams
# How a call ended, as long as nothing has said otherwise.
CALLER_ENDED = "customer-ended-call"
# How long a call past its limit may take to finish what it is saying: a provider that hangs must not keep the
# line, and the bill, open.
END_GRACE_SECONDS = 30
# How long the limits wait for a transfer: the line going quiet (30 s at most), the carrier's answer (a SIP transfer
# rings the other line, sip.REFER_SECONDS), the stream closing.
TRANSFER_SECONDS = 90


def turn_latency(breakdown: Any) -> dict[str, int]:
    """One turn the way Vapi reports it: milliseconds per stage, plus the whole turn."""
    turn = dict.fromkeys(LATENCY_FIELDS, 0)
    for part in breakdown.contributions:
        if part.key in LATENCY_PARTS:
            turn[LATENCY_PARTS[part.key]] += round(part.duration_secs * 1000)
    turn["turnLatency"] = round(breakdown.total_secs * 1000)
    return turn


def performance_metrics(turns: list[dict[str, int]], first_word_ms: int | None = None) -> dict[str, Any]:
    """Vapi-style `artifact.performanceMetrics`: every turn and the average of each field.

    `firstWordLatency` (OpenRing's addition) is how long the caller waited for the assistant's first word.
    """
    metrics: dict[str, Any] = {"turnLatencies": turns}
    if first_word_ms is not None:
        metrics["firstWordLatency"] = first_word_ms
    if turns:
        for name in LATENCY_FIELDS:
            metrics[name + "Average"] = round(sum(t[name] for t in turns) / len(turns))
    return metrics


class CallLimits:
    """Vapi's `maxDurationSeconds` and `silenceTimeoutSeconds`: the reason a call should end now, if any.

    The clock starts when the caller connects. Silence is time without activity, as on Vapi: it doesn't run while
    someone is talking, the model is working on an answer or a tool is running, and it starts over when they stop,
    so the caller always gets the whole limit to answer.
    """

    def __init__(self, max_duration: float, silence_timeout: float, now: float):
        self.max_duration, self.silence_timeout = max_duration, silence_timeout
        self.started = self.last_activity = now
        self.user_speaking = self.bot_speaking = self.model_working = False
        self.tools: set[str] = set()  # the tool calls still running

    def user(self, speaking: bool, now: float) -> None:
        self.user_speaking, self.last_activity = speaking, now
        if speaking:  # a caller who starts talking interrupts the model
            self.model_working = False

    def bot(self, speaking: bool, now: float) -> None:
        self.bot_speaking, self.last_activity = speaking, now

    def model(self, working: bool, now: float) -> None:
        self.model_working, self.last_activity = working, now

    def tool(self, call_id: str, running: bool, now: float) -> None:
        (self.tools.add if running else self.tools.discard)(call_id)
        self.last_activity = now

    def due(self, now: float) -> str | None:
        if self.max_duration and now - self.started >= self.max_duration:
            return "exceeded-max-duration"
        working = self.user_speaking or self.bot_speaking or self.model_working or self.tools
        if self.silence_timeout and not working and now - self.last_activity >= self.silence_timeout:
            return "silence-timed-out"
        return None


class Usage:
    """What a call used of each provider, from Pipecat's usage metrics: what providers bill by."""

    def __init__(self) -> None:
        self.prompt_tokens = self.completion_tokens = self.tts_characters = 0
        self.audio_seconds = 0.0
        # Per service (Pipecat's processor name), so a call that switched providers is priced per provider.
        self.by_service: dict[str, dict[str, Any]] = {}

    def _row(self, item: Any) -> dict[str, Any]:
        row = self.by_service.setdefault(item.processor, {"model": None, "prompt": 0, "completion": 0,
                                                         "characters": 0, "seconds": 0.0})
        row["model"] = item.model or row["model"]
        return row

    def add(self, data: list[Any]) -> None:
        from pipecat.metrics.metrics import LLMUsageMetricsData, STTUsageMetricsData, TTSUsageMetricsData

        for item in data:
            if isinstance(item, LLMUsageMetricsData):
                self.prompt_tokens += item.value.prompt_tokens
                self.completion_tokens += item.value.completion_tokens
                row = self._row(item)
                row["prompt"] += item.value.prompt_tokens
                row["completion"] += item.value.completion_tokens
            elif isinstance(item, TTSUsageMetricsData):
                self.tts_characters += item.value
                self._row(item)["characters"] += item.value
            elif isinstance(item, STTUsageMetricsData):
                self.audio_seconds += item.value.audio_seconds
                self._row(item)["seconds"] += item.value.audio_seconds

    def costs(self, services: list[Any]) -> dict[str, Any]:
        """US$ at list prices (prices.py), in Vapi's `stt`, `llm`, `tts` and `total`. A provider without a known
        price is named in `unpriced` and left out of the total."""
        by_name = {s.name: s for s in services}
        totals = {"stt": 0.0, "llm": 0.0, "tts": 0.0}
        unpriced: list[str] = []
        for name, row in self.by_service.items():
            service = by_name.get(name)
            if service is None or not hasattr(service, "openring_price"):
                continue
            layer, provider, model, language = service.openring_price
            model = model or row["model"]
            if layer == "model":
                price = prices.llm(provider, model)
                field, cost = "llm", None if price is None else (
                    row["prompt"] * price[0] + row["completion"] * price[1]) / 1_000_000
            elif layer == "transcriber":
                rate = prices.stt(provider, model, language)
                field, cost = "stt", None if rate is None else row["seconds"] / 60 * rate
            else:
                rate = prices.tts(provider, model)
                field, cost = "tts", None if rate is None else row["characters"] / 1_000_000 * rate
            if cost is None:
                unpriced.append(service.openring_label)
            else:
                totals[field] += cost
        result: dict[str, Any] = {k: round(v, 6) for k, v in totals.items()}
        result["total"] = round(sum(totals.values()), 6)
        if unpriced:
            result["unpriced"] = unpriced
        return result

    def cost_breakdown(self, services: list[Any] | None = None) -> dict[str, Any]:
        """Vapi's `costBreakdown`: usage (plus the seconds of audio sent to speech-to-text) and, given the
        call's services, what it cost."""
        return {
            "llmPromptTokens": self.prompt_tokens,
            "llmCompletionTokens": self.completion_tokens,
            "ttsCharacters": self.tts_characters,
            "transcriberAudioSeconds": round(self.audio_seconds, 1),
            **(self.costs(services) if services is not None else {}),
        }


def members(service: Any) -> list[Any]:
    """The provider services behind one pipeline step: a switcher's, or the service itself."""
    return list(getattr(service, "services", None) or [service]) if service is not None else []


def what_failed(processor: Any) -> str:
    """'the voice (11labs rachel)': the call's layer a failing service (or a switcher out of fallbacks) stands for."""
    labeled = [s for s in members(processor) if hasattr(s, "openring_price")]
    if not labeled:
        return str(processor)
    return f"the {labeled[0].openring_price[0]} ({', '.join(s.openring_label for s in labeled)})"


def short_error(error: Any) -> str:
    """A provider's error without the source location Pipecat adds to it ('... (client.py:474): ...')."""
    return re.sub(r" \([^()]*\.py:\d+\)", "", str(error))[:200]


def transcript_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """The call's transcript: what each side said, plus the tools the assistant used and their results."""
    transcript: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            transcript.append({"role": role, "message": content})
        if role == "assistant" and m.get("tool_calls"):
            transcript.append({"role": "tool_calls", "toolCalls": m["tool_calls"]})
        elif role == "tool":
            result = _plain(content)
            transcript.append({"role": "tool_call_result", "toolCallId": m.get("tool_call_id"), "result": result})
    return transcript


def opening(plan: StackPlan) -> str | None:
    """What an assistant does on taking a call, by Vapi's `firstMessageMode`: "speak" its first message, "generate"
    one with the model, or None to wait for the caller."""
    mode = plan.assistant.get("firstMessageMode")
    if mode == "assistant-waits-for-user":
        return None
    if plan.first_message and mode != "assistant-speaks-first-with-model-generated-message":
        return "speak"
    return "generate"


def _plain(content: Any) -> Any:
    """Pipecat stores a text result JSON-encoded ('"Booked"'); keep the text itself."""
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
        except ValueError:
            return content
        if isinstance(decoded, str):
            return decoded
    return content


def keep_phone_line_open(transport: Any, keep: bool = True) -> None:
    """Pipecat's Twilio serializer hangs up when the pipeline stops; a transferred call must stay up."""
    serializer = getattr(getattr(transport, "_params", None), "serializer", None)
    if serializer is not None and hasattr(serializer, "_hangup_attempted"):
        serializer._hangup_attempted = keep


def twilio_transport(websocket: WebSocket, call_data: dict[str, Any], settings: Settings) -> Any:
    """Audio of a phone call, carried by a Twilio media stream."""
    from pipecat.serializers.twilio import TwilioFrameSerializer
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    has_creds = bool(settings.twilio_account_sid and settings.twilio_auth_token)
    serializer = TwilioFrameSerializer(
        stream_sid=call_data["stream_id"],
        call_sid=call_data.get("call_id"),
        account_sid=settings.twilio_account_sid or None,
        auth_token=settings.twilio_auth_token or None,
        # Auto hang-up uses the Twilio REST API, so it needs credentials.
        params=TwilioFrameSerializer.InputParams(auto_hang_up=has_creds),
    )
    return FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )


def telnyx_transport(websocket: WebSocket, call_data: dict[str, Any], settings: Settings) -> Any:
    """Audio of a phone call, carried by a Telnyx media stream (TeXML <Stream bidirectionalMode="rtp">)."""
    from pipecat.serializers.telnyx import TelnyxFrameSerializer
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    # Auto hang-up uses the Telnyx API, so it needs the key and the call's control id.
    can_hang_up = bool(settings.telnyx_api_key and call_data.get("call_id"))
    serializer = TelnyxFrameSerializer(
        stream_id=call_data["stream_id"],
        call_control_id=call_data.get("call_id"),
        outbound_encoding=call_data.get("outbound_encoding") or "PCMU",
        inbound_encoding="PCMU",
        api_key=settings.telnyx_api_key or None,
        params=TelnyxFrameSerializer.InputParams(auto_hang_up=can_hang_up),
    )
    return FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )


def plivo_transport(websocket: WebSocket, call_data: dict[str, Any], settings: Settings) -> Any:
    """Audio of a phone call, carried by a Plivo audio stream (XML <Stream bidirectional="true">)."""
    from pipecat.serializers.plivo import PlivoFrameSerializer
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    # Auto hang-up uses the Plivo REST API, so it needs the auth id and token and the call's id.
    can_hang_up = bool(settings.plivo_auth_id and settings.plivo_auth_token and call_data.get("call_id"))
    serializer = PlivoFrameSerializer(
        stream_id=call_data["stream_id"],
        call_id=call_data.get("call_id"),
        auth_id=settings.plivo_auth_id or None,
        auth_token=settings.plivo_auth_token or None,
        params=PlivoFrameSerializer.InputParams(auto_hang_up=can_hang_up),
    )
    return FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )


def vonage_transport(websocket: WebSocket) -> Any:
    """Audio of a Vonage call, carried by a websocket the NCCO connected: 16 kHz linear PCM, 20 ms at a time."""
    from pipecat.serializers.vonage import VonageFrameSerializer
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    from .vonage import PACKET_BYTES

    return FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=VonageFrameSerializer(),
            fixed_audio_packet_size=PACKET_BYTES,
        ),
    )


def genesys_transport(websocket: WebSocket, serializer: Any) -> Any:
    """Audio of a Genesys Cloud call, carried by its Audio Connector. The serializer has already answered the
    session's `open`, so it knows the caller and Architect's variables."""
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    return FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
            # 200 ms of PCMU per message: Genesys rate-limits (HTTP 429) a stream of small packets.
            fixed_audio_packet_size=1600,
        ),
    )


def web_transport(connection: Any) -> Any:
    """Audio of a browser test call, over the WebRTC connection the dashboard opened."""
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    return SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )


async def run_call(
    transport: Any,
    *,
    plan: StackPlan,
    call_id: str,
    store: Store,
    sample_rate: int | None = None,
    transfer: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Run a call until it ends. `transfer(number)` moves a phone call to another line (transferCall tool)."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame,
        BotStoppedSpeakingFrame,
        CancelFrame,
        EndFrame,
        FunctionCallCancelFrame,
        FunctionCallResultFrame,
        FunctionCallsStartedFrame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMMessagesAppendFrame,
        LLMRunFrame,
        MetricsFrame,
        TTSSpeakFrame,
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.metrics.metrics import TTFATMetricsData
    from pipecat.observers.base_observer import BaseObserver
    from pipecat.observers.user_bot_latency_observer import MeasuredFrom, UserBotLatencyObserver
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.service_switcher import ServiceSwitcher
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.pipeline.worker import ProcessorUnusablePolicy
    from pipecat.processors.aggregators.llm_context import NOT_GIVEN, LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )

    started = time.monotonic()  # the caller already marked the call in-progress
    ended_reason = CALLER_ENDED
    context = None
    turns: list[dict[str, int]] = []
    usage = Usage()
    switches: list[dict[str, str]] = []  # providers that took over from one that failed
    services: list[Any] = []  # every provider service the call built, to price its usage
    first_word: dict[str, float | None] = {"connected": None, "spoke": None}
    limits: CallLimits | None = None  # set when the caller connects
    watching: asyncio.Task | None = None
    transfer_until = 0.0  # the limits wait for a transfer in progress
    idle = {"spoken": 0}
    recorder = recordings.Recorder(call_id) if plan.record else None
    recording = None
    recording_over = asyncio.Event()
    team = [plan, *plan.team]  # everyone on this call; the first answers it
    who = {"plan": plan}  # whose turn it is
    handoffs: list[dict[str, str]] = []
    said_before: list[dict[str, Any]] = []  # the conversation with the assistants before the current one
    inherited = 0  # how much of the current assistant's context came from them
    variables: dict[str, Any] = {}  # what handoffs added to the call's variables
    handing_off: set[asyncio.Task] = set()  # a handoff or a transfer under way
    passed_over: set[str] = set()  # handoffs asked for while another was under way
    outbox =server_messages.Outbox(plan.assistant, lambda: store.get("calls", call_id))
    # The setup runs inside the try as well, so the call record ends however the call does: a caller
    # who hangs up while the services are still being built, or a provider that fails to start.
    try:
        # Loading a local voice model takes a moment; doing it off the event loop keeps other calls flowing.
        stt, llm, tts = await asyncio.to_thread(build_services, plan)
        services = [*members(stt), *members(llm), *members(tts)]
        by_name = {s.name: s for s in services}
        for warning in plan.warnings:
            logger.warning("call {}: {}", call_id, warning)
        speech = {"speaking": False, "last_stop": 0.0}

        # With fallback models, a watch after the model closes what a replaced model left open and, with a
        # latency limit, hands a slow turn to the next model.
        watch = None
        why = {"model": ""}  # why the model is about to switch, when it isn't an error
        if isinstance(llm, ServiceSwitcher):
            from . import fallback

            async def too_slow(seconds: float) -> None:
                why["model"] = f"slow: no answer after {seconds:.1f} s"
                slow = llm.strategy.active_service
                if getattr(slow, "openring_model", None):  # at least this slow: "fastest" routes rank it lower
                    routes.record_speed(*slow.openring_model, seconds * 1000)
                if await llm.strategy.skip(slow) is None:
                    why["model"] = ""
                    logger.warning("call {}: model slow ({:.1f} s) and no faster one left", call_id, seconds)

            watch = fallback.model_watch(lambda: who["plan"].max_latency_ms, too_slow)

        def watch_fallback(layer: str, service: Any) -> None:
            """Record each provider that takes over; a new model answers the turn the failed one dropped."""
            if not isinstance(service, ServiceSwitcher):
                return
            current = {"service": service.strategy.active_service}

            @service.strategy.event_handler("on_service_switched")
            async def _switched(strategy, active):
                if getattr(strategy, "entered", None) is active:  # a handoff, not a failure
                    strategy.entered = None
                    current["service"] = active
                    return
                to = getattr(active, "openring_label", active.name)
                reason, why[layer] = why.get(layer) or "failed", ""
                switches.append({"layer": layer, "to": to, "at": now_iso(), "reason": reason})
                logger.warning("call {}: {} switched to {} ({})", call_id, layer, to, reason)
                left, current["service"] = current["service"], active
                if layer == "model":
                    routes.let_down(who["plan"].route, left)  # so the next calls start on the next model
                    if watch is not None:
                        await watch.replaced(left)
                    await task.queue_frames([LLMRunFrame()])

        for layer, service in (("transcriber", stt), ("model", llm), ("voice", tts)):
            watch_fallback(layer, service)

        async def speak(text: str) -> None:
            await task.queue_frames([TTSSpeakFrame(text, append_to_context=False)])

        async def end_call() -> None:
            nonlocal ended_reason
            ended_reason = "assistant-ended-call"
            await task.queue_frames([EndFrame()])  # after whatever is still being said

        async def wait_until_quiet(expect_speech: bool) -> None:
            """Until the assistant finishes talking, so a transfer never cuts a sentence."""
            asked = time.monotonic()
            while time.monotonic() - asked < 30:
                await asyncio.sleep(0.2)
                if speech["speaking"]:
                    continue
                if expect_speech and speech["last_stop"] < asked and time.monotonic() - asked < 15:
                    continue  # a local voice takes a moment to synthesize before it starts
                if time.monotonic() - max(speech["last_stop"], asked) >= 0.8:
                    break
            await asyncio.sleep(0.3)

        async def transfer_to(number: str, message: str | None) -> None:
            nonlocal ended_reason, transfer_until
            transfer_until = time.monotonic() + TRANSFER_SECONDS  # a limit must not hang up on a caller being moved
            try:
                if message:
                    await speak(message)
                await wait_until_quiet(expect_speech=bool(message))
                keep_phone_line_open(transport)
                ended_reason = "assistant-forwarded-call"
                logger.info("call {} transferred to {}", call_id, number)
                outbox.send("status-update", status="forwarding", destination={"type": "number", "number": number})
                await transfer(number)
            except Exception:  # the caller is still here with the assistant, and the limits apply again
                keep_phone_line_open(transport, False)
                ended_reason, transfer_until = CALLER_ENDED, 0.0
                outbox.send("status-update", status="in-progress")
                raise

        async def tell_model(text: str) -> None:
            # "developer", which goes out as what each provider takes: some refuse "system" in mid-conversation.
            await task.queue_frames([LLMMessagesAppendFrame([{"role": "developer", "content": text}], run_llm=True)])

        async def uninterrupted(work: Any) -> None:
            """Apart from the tool call that asked for it: a caller's "okay" cancels the tool's handler, and the
            transfer has to go on. Ends with the call, like a handoff."""
            moving = asyncio.ensure_future(work)
            handing_off.add(moving)
            moving.add_done_callback(handing_off.discard)
            await asyncio.shield(moving)

        def conversation() -> list[dict[str, Any]]:
            """Everything said on this call, whatever each assistant was given of it."""
            said = [m for m in context.get_messages() if isinstance(m, dict) and m.get("role") != "system"]
            return [*said_before, *said[inherited:]]

        def hand_off(destination: dict[str, Any], arguments: dict[str, Any], message: str | None,
                     tool_call_id: str) -> str | None:
            """Move the call to another assistant, once this one has finished talking. Says why it can't, or None."""
            if handing_off:  # two in one answer, or another while the caller waits: the first one counts
                passed_over.add(tool_call_id)
                return HANDOFF_UNDER_WAY
            position = squads.find(team, destination)
            if position is None:
                return "That assistant is not on this call."
            if team[position] is who["plan"]:
                return "You already are that assistant."
            if len(handoffs) >= squads.MAX_HANDOFFS:
                return "This call has been handed off too many times."
            moving = asyncio.ensure_future(finish_hand_off(position, destination, arguments, message, tool_call_id))
            handing_off.add(moving)
            moving.add_done_callback(handing_off.discard)
            return None

        async def finish_hand_off(position: int, destination: dict[str, Any], arguments: dict[str, Any],
                                  message: str | None, tool_call_id: str) -> None:
            nonlocal said_before, inherited
            target = team[position]
            try:
                if message:
                    await speak(message)
                await wait_until_quiet(expect_speech=bool(message))
                for layer in (stt, llm, tts):
                    if isinstance(layer, ServiceSwitcher) and hasattr(layer.strategy, "enter"):
                        left = layer.strategy.active_service
                        await layer.strategy.enter(position)
                        # If the caller spoke during the wait, its answer would go on in the next assistant's voice.
                        if layer is llm and watch is not None and layer.strategy.active_service is not left:
                            await watch.replaced(left)
                given =(destination.get("assistantOverrides") or {}).get("variableValues")
                variables.update(arguments, **(given if isinstance(given, dict) else {}))
                values = {**overrides.call_context(store.get("calls", call_id)), **variables}
                everything = conversation()
                kept = squads.carry_over(
                    [m for m in context.get_messages() if isinstance(m, dict) and m.get("role") != "system"],
                    said_before, destination.get("contextEngineeringPlan"), {tool_call_id, *passed_over},
                )
                prompt = [{**m, "content": overrides.fill(m["content"], values)} for m in target.messages]
                # The new assistant gets its own prompt, tools and voice, and the part of the conversation it should.
                said_before, inherited = everything, len(kept)
                context.set_messages([*prompt, *kept])
                context.set_tools(schemas[position] or NOT_GIVEN)
                came_from = who["plan"]
                who["plan"] = target
                handoffs.append({"from": squads.name_of(came_from.assistant), "to": squads.name_of(target.assistant),
                                 "at": now_iso()})
                logger.info("call {}: {} handed off to {}", call_id, handoffs[-1]["from"], handoffs[-1]["to"])
                how = opening(target)
                first = overrides.fill(target.first_message or "", values)
                if how == "speak" and first:
                    context.add_message({"role": "assistant", "content": first})
                    await speak(first)
                elif how:
                    await task.queue_frames([LLMRunFrame()])
            except Exception:  # the caller stays on the line with whoever has them
                logger.exception("call {}: handoff failed", call_id)

        schemas: list[Any] = []
        for position, member in enumerate(team):
            controls = CallControls(
                speak=speak,
                end_call=end_call,
                transfer=transfer_to if transfer else None,
                current_call=lambda: store.get("calls", call_id),
                assistant=member.assistant,
                default_url=member.server_url or plan.server_url,
                store=store,
                messages=lambda: [m for m in context.get_messages() if isinstance(m, dict)],
                hand_off=hand_off if plan.team else None,
                in_charge=lambda member=member: who["plan"] is member and not handing_off,
                tell_model=tell_model,
                uninterrupted=uninterrupted,
            )
            groups = getattr(getattr(llm, "strategy", None), "groups", None)
            schema = None
            # A model with fallbacks registers on all of them; on a team, each assistant's model gets its own tools.
            for model_service in (groups[position] if groups else [llm]):
                schema = register_tools(model_service, assistant_tools(member.assistant, store), controls)
            schemas.append(schema)
        tools = schemas[0]
        context = LLMContext(messages=list(plan.messages), **({"tools": tools} if tools else {}))
        aggregators = LLMContextAggregatorPair(
            context, user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(),
                # Pipecat times the caller's silence after the assistant speaks (not during a tool call).
                user_idle_timeout=plan.idle_timeout_seconds if plan.idle_messages else 0,
            )
        )
        if recorder:
            from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

            recording = AudioBufferProcessor(num_channels=2, buffer_size=recordings.BUFFER_BYTES)

            @recording.event_handler("on_audio_data")
            async def _on_audio(_buffer, audio, audio_sample_rate, channels):
                recorder.write(bytes(audio), audio_sample_rate, channels)

            @recording.event_handler("on_recording_stopped")  # comes after the last on_audio_data
            async def _on_recording_over(_buffer):
                recording_over.set()

        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                aggregators.user(),
                llm,
                *([watch] if watch is not None else []),
                tts,
                transport.output(),
                *([recording] if recording is not None else []),  # after the output: what the caller really heard
                aggregators.assistant(),
            ]
        )
        latency = UserBotLatencyObserver()

        class SpeechObserver(BaseObserver):
            async def on_push_frame(self, data):
                if isinstance(data.frame, BotStartedSpeakingFrame):
                    speech["speaking"] = True
                    if limits:
                        limits.bot(True, time.monotonic())
                    if first_word["connected"] and not first_word["spoke"]:
                        first_word["spoke"] = time.monotonic()
                elif isinstance(data.frame, BotStoppedSpeakingFrame):
                    speech.update(speaking=False, last_stop=time.monotonic())
                    if limits:
                        limits.bot(False, time.monotonic())
                elif isinstance(data.frame, (UserStartedSpeakingFrame, UserStoppedSpeakingFrame)):
                    started = isinstance(data.frame, UserStartedSpeakingFrame)
                    if limits:
                        limits.user(started, time.monotonic())
                    if started and plan.idle_reset_on_speech:
                        idle["spoken"] = 0
                elif isinstance(data.frame, (LLMFullResponseStartFrame, LLMFullResponseEndFrame)):
                    if limits:
                        limits.model(isinstance(data.frame, LLMFullResponseStartFrame), time.monotonic())
                elif isinstance(data.frame, FunctionCallsStartedFrame):
                    for function_call in data.frame.function_calls if limits else ():
                        limits.tool(function_call.tool_call_id, True, time.monotonic())
                elif isinstance(data.frame, (FunctionCallResultFrame, FunctionCallCancelFrame)):
                    if limits:
                        limits.tool(data.frame.tool_call_id, False, time.monotonic())
                    if isinstance(data.frame, FunctionCallResultFrame):
                        # A tool from before a handoff: the next assistant may not have it, the record must.
                        for row in said_before:
                            if row.get("role") == "tool" and row.get("tool_call_id") == data.frame.tool_call_id:
                                row["content"] = json.dumps(data.frame.result, ensure_ascii=False)
                elif isinstance(data.frame, MetricsFrame):
                    usage.add(data.frame.data)
                    for item in data.frame.data:  # how long each model took to start answering
                        model = getattr(by_name.get(item.processor), "openring_model", None)
                        if isinstance(item, TTFATMetricsData) and model:
                            routes.record_speed(*model, item.ttfat * 1000)

        @latency.event_handler("on_latency_breakdown")
        async def _on_latency(_observer, breakdown):
            if breakdown.measured_from == MeasuredFrom.USER_SILENCE:  # the greeting is not a turn
                turns.append(turn_latency(breakdown))
                logger.info("call {} turn latency {} ms", call_id, turns[-1]["turnLatency"])

        rates = {"audio_in_sample_rate": sample_rate, "audio_out_sample_rate": sample_rate} if sample_rate else {}
        task = PipelineTask(
            pipeline,
            params=PipelineParams(enable_metrics=True, enable_usage_metrics=True, **rates),
            observers=[latency, SpeechObserver(observe_every_push=False)],
            # Pipecat gives up on a call with no activity after 5 minutes: never before the silence limit.
            idle_timeout_secs=max(300, plan.silence_timeout_seconds + 60),
            # A layer that can no longer work (a rejected key, no credit, an outage) with no fallback left to take
            # over ends the call: Pipecat's default carries on, leaving the caller in silence.
            processor_unusable_policy=ProcessorUnusablePolicy.END,
        )

        @task.event_handler("on_pipeline_error")
        async def _on_error(_task, frame):
            nonlocal ended_reason
            failed = getattr(frame, "processor", None)
            if failed is not None and not failed.is_usable and not ended_reason.startswith("pipeline-error"):
                ended_reason = f"pipeline-error: {what_failed(failed)} stopped working: {short_error(frame.error)}"
                logger.warning("call {} ends: {}", call_id, ended_reason)

        @task.event_handler("on_idle_timeout")
        async def _on_idle_timeout(_task):
            nonlocal ended_reason
            if ended_reason == CALLER_ENDED:
                ended_reason = "silence-timed-out"

        async def watch_limits() -> None:
            """End the call when it has lasted too long or had no activity for too long, and see that it does end."""
            nonlocal ended_reason
            while True:
                await asyncio.sleep(0.5)
                reason = limits.due(time.monotonic()) if limits else None
                if reason and time.monotonic() >= transfer_until:
                    break
            if ended_reason == CALLER_ENDED:  # a call already ending for another reason keeps that reason
                ended_reason = reason
                logger.info("call {} ending: {}", call_id, reason)
                if who["plan"].end_call_message:
                    await speak(who["plan"].end_call_message)
                await task.queue_frames([EndFrame()])  # after whatever is still being said
            await asyncio.sleep(END_GRACE_SECONDS)
            logger.warning("call {}: not finished {} s after {}; cutting it off", call_id, END_GRACE_SECONDS, reason)
            await task.cancel()
            # That waits forever behind an EndFrame a provider is holding up, so the pipeline hears it directly too.
            await task.pipeline.queue_frame(CancelFrame())

        if plan.idle_messages:
            @aggregators.user().event_handler("on_user_turn_idle")
            async def _on_idle(_aggregator):
                if idle["spoken"] < plan.idle_max_spoken:
                    idle["spoken"] += 1
                    await task.queue_frames([TTSSpeakFrame(random.choice(plan.idle_messages))])

        @transport.event_handler("on_client_connected")
        async def _on_connected(_transport, _client):
            nonlocal ended_reason, limits, watching
            first_word["connected"] = time.monotonic()
            if recording is not None:
                await recording.start_recording()
            limits = CallLimits(plan.max_duration_seconds, plan.silence_timeout_seconds, time.monotonic())
            watching = asyncio.ensure_future(watch_limits())
            outbox.send("status-update", status="in-progress")
            if plan.leave_message or opening(plan) == "speak":
                # In the context (and transcript) right away, even if the voice fails to say it.
                context.add_message({"role": "assistant", "content": plan.first_message})
                greeting = TTSSpeakFrame(plan.first_message, append_to_context=False)
                if plan.leave_message:  # the beep or noise on the line must not cut the message off
                    greeting.interruptible = False
                    ended_reason = "voicemail"
                await task.queue_frames([greeting, *([EndFrame()] if plan.leave_message else [])])
            elif opening(plan):
                await task.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_client_disconnected")
        async def _on_disconnected(_transport, _client):
            await task.cancel()

        @aggregators.assistant().event_handler("on_assistant_turn_stopped")
        async def _conversation_update(_aggregator, _message):
            messages = conversation()
            outbox.send("conversation-update", messages=transcript_messages(messages), messagesOpenAIFormatted=messages)

        if any(member.end_call_phrases for member in team):

            @aggregators.assistant().event_handler("on_assistant_turn_stopped")
            async def _maybe_hang_up(_aggregator, message):
                nonlocal ended_reason
                text = str(getattr(message, "content", message) or "").lower()
                if any(p.lower() in text for p in who["plan"].end_call_phrases):
                    ended_reason = "assistant-said-end-call-phrase"
                    await task.queue_frames([EndFrame()])

        await PipelineRunner(handle_sigint=False).run(task)
    except Exception as exc:  # keep the call record honest
        logger.exception("call {} failed", call_id)
        ended_reason = f"pipeline-error: {exc}"
    finally:
        if watching is not None:
            watching.cancel()
        for moving in list(handing_off):
            moving.cancel()
        transcript = transcript_messages(conversation()) if context is not None else []
        breakdown = usage.cost_breakdown(services)
        routes.save_speed(store)
        recorded = False
        if recorder:
            if recording is not None and first_word["connected"]:
                # What's left of the audio goes out in a task of its own: a caller who hangs up shouldn't lose it.
                try:
                    await recording.stop_recording()
                    await asyncio.wait_for(recording_over.wait(), 2)
                except (Exception, asyncio.CancelledError) as exc:  # the call's record below has to be saved anyway
                    logger.warning("call {}: the end of the recording may be missing: {!r}", call_id, exc)
            recorded = recorder.close()
        recording_urls = {key: recordings.url_for(call_id) for key in ("recordingUrl", "stereoRecordingUrl")}
        spoke, connected = first_word["spoke"], first_word["connected"]
        first_word_ms = round((spoke - connected) * 1000) if spoke and connected else None
        call = store.update(
            "calls",
            call_id,
            {
                "status": "ended",
                "endedAt": now_iso(),
                "endedReason": ended_reason,
                "durationSeconds": round(time.monotonic() - started, 1),
                "messages": transcript,
                "artifact": {"messages": transcript, "performanceMetrics": performance_metrics(turns, first_word_ms),
                             **(recording_urls if recorded else {})},
                "cost": breakdown["total"],
                "costBreakdown": breakdown,
                **({"providerFallbacks": switches} if switches else {}),
                **({"handoffs": handoffs} if handoffs else {}),
            },
        )
        if call:
            outbox.send("status-update", status="ended", endedReason=ended_reason)

            async def report() -> None:
                await outbox.flush()
                await server_messages.send(
                    plan.assistant, "end-of-call-report", call, endedReason=ended_reason,
                    artifact=call.get("artifact") or {"messages": transcript},
                )

            # When the caller hangs up, the server cancels this call's task; the report still goes out.
            reporting = asyncio.ensure_future(report())
            _reports.add(reporting)
            reporting.add_done_callback(_reports.discard)
            await asyncio.shield(reporting)


# End-of-call reports still on their way, kept so they are not collected before they finish.
_reports: set[asyncio.Future] = set()
