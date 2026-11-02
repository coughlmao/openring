"""What providers charge: a dollar figure for each call, and the order of a "cheapest" model route.

List prices, pay as you go, in US dollars. Models come from `llm_prices.json` (refreshed by
`scripts/update_prices.py` from LiteLLM's price list and OpenRouter's model list); speech-to-text and voices
are below, from each provider's pricing page. Local providers cost nothing. A price we don't know is None,
never a guess. `~/.openring/prices.json` overrides any of them, in the same shape:

    {"llm": {"custom-llm": {"my-model": [0.2, 0.6]}},   # per 1M tokens: [input, output]
     "stt": {"deepgram": {"nova-3": 0.0077}},           # per minute of audio
     "tts": {"cartesia": {"*": 37.38}}}                 # per 1M characters; "*" is any model
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from loguru import logger

UPDATED = "2026-11-01"  # when the speech-to-text and voice prices below were checked
# Per minute of audio sent to the transcriber. "<model>:multi" is the price with "language": "multi".
STT: dict[str, dict[str, float]] = {
    "deepgram": {"*": 0.0077, "nova-3:multi": 0.0092, "flux-general-multi": 0.0078},  # deepgram.com/pricing
    "assembly-ai": {"*": 0.0025},  # Universal-Streaming, $0.15/h: assemblyai.com/pricing
    "soniox": {"*": 0.002},  # real-time, $0.12/h: soniox.com/pricing
    "11labs": {"*": 0.0065},  # Scribe v2 Realtime, $0.39/h: elevenlabs.io/pricing/api
    "cartesia": {"*": 0.0071},  # Ink: 3 credits/s on the Startup plan ($49 for 1.25M credits): cartesia.ai/pricing
    "whisper": {"*": 0.0},  # runs on this computer
    "gladia": {"*": 0.0125},  # gladia.io/pricing (real-time: $0.75/hour)
}
# Per million characters the voice reads.
TTS: dict[str, dict[str, float]] = {
    "11labs": {"*": 40.0, "eleven_multilingual_v2": 80.0, "eleven_v3": 80.0},  # elevenlabs.io/pricing/api
    "deepgram": {"*": 30.0},  # Aura-2: deepgram.com/pricing
    "openai": {"*": 15.0, "tts-1-hd": 30.0},  # tts-1; gpt-4o-mini-tts bills about the same per minute of speech
    "cartesia": {"*": 39.2},  # Sonic: 1 credit per character on the Startup plan: cartesia.ai/pricing
    "inworld": {"inworld-tts-2": 25.0, "inworld-tts-2-flash": 15.0},  # on demand: inworld.ai/pricing
    "kokoro": {"*": 0.0},  # runs on this computer
    "rime-ai": {"coda": 50.0, "mistv2": 30.0},  # rime.ai/pricing
    "smallest-ai": {"lightning_v3.1": 17.5, "lightning_v3.1_pro": 19.5},  # smallest.ai/pricing/models
}
FREE_LLMS = {"ollama"}  # runs on this computer (OmniRoute may route to paid providers, so it isn't here)
# How a route's "cheapest" order weighs input against output: a voice call sends its whole conversation on
# every turn and gets a sentence or two back, so input tokens are most of the bill.
INPUT_SHARE = 0.75
USER_FILE = Path.home() / ".openring" / "prices.json"


@cache
def _shipped() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("llm_prices.json").read_text(encoding="utf-8"))


@cache
def _tables() -> dict[str, dict[str, dict[str, Any]]]:
    tables = {"llm": _shipped()["llm"], "stt": STT, "tts": TTS}
    tables = {kind: {p: dict(models) for p, models in table.items()} for kind, table in tables.items()}
    try:
        user = json.loads(USER_FILE.read_text(encoding="utf-8")) if USER_FILE.exists() else {}
    except (OSError, ValueError) as exc:
        logger.warning("{} ignored: {}", USER_FILE, exc)
        user = {}
    for kind, table in tables.items():
        for provider, models in (user.get(kind) or {}).items() if isinstance(user, dict) else ():
            if isinstance(models, dict):
                shipped = table.get(provider, {})
                merged = {**shipped, **models}
                if "*" in models:
                    for key in shipped:
                        if key not in models:
                            merged[key] = models["*"]

                table[provider] = merged

    return tables


def table() -> dict[str, Any]:
    """Every price, for the dashboard: {"updated", "llm", "stt", "tts"}."""
    return {"updated": {"llm": _shipped()["updated"], "stt": UPDATED, "tts": UPDATED}, **_tables()}


def reload() -> None:
    """Read the price files again (after ~/.openring/prices.json changes, say)."""
    _tables.cache_clear()


def _find(kind: str, provider: str, model: str | None) -> Any:
    models = _tables()[kind].get(provider) or {}
    for key in (model, (model or "").lower(), "*"):
        if key and key in models:
            return models[key]
    return None


def llm(provider: str, model: str | None) -> tuple[float, float] | None:
    """US$ per 1M tokens, (input, output); None when unknown."""
    if provider in FREE_LLMS:
        return (0.0, 0.0)
    found = _find("llm", provider, model)
    if isinstance(found, (list, tuple)) and len(found) == 2:
        return (float(found[0]), float(found[1]))
    return None


def stt(provider: str, model: str | None, language: str | None = None) -> float | None:
    """US$ per minute of audio; None when unknown."""
    if language == "multi" and (found := _find("stt", provider, f"{model}:multi")) is not None:
        return float(found)
    found = _find("stt", provider, model)
    return None if found is None else float(found)


def tts(provider: str, model: str | None) -> float | None:
    """US$ per 1M characters; None when unknown."""
    found = _find("tts", "11labs" if provider == "elevenlabs" else provider, model)
    return None if found is None else float(found)


def blended(provider: str, model: str | None) -> float | None:
    """One US$-per-1M-tokens figure to rank models by, weighted like a voice call's bill."""
    price = llm(provider, model)
    return None if price is None else price[0] * INPUT_SHARE + price[1] * (1 - INPUT_SHARE)
