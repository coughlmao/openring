"""Turn a Vapi-style assistant document into a concrete STT / LLM / TTS stack.

`resolve_plan` only reads the assistant, the environment and the disk (no Pipecat
objects), so it is easy to test. `build_services` instantiates the Pipecat services
for a live call.
"""

from __future__ import annotations

import importlib.util
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import netguard, whisper


class ProviderError(ValueError):
    """Raised when an assistant asks for a provider we cannot build."""


# Provider names are Vapi's, so an assistant exported from Vapi keeps working; a few extras follow.
#
# LLMs, all reached through their OpenAI-compatible API: name -> (base_url, api key env var).
# Anything that speaks the OpenAI chat API can be added here in one line.
LLM_PROVIDERS: dict[str, tuple[str | None, str | None]] = {
    "openai": (None, "OPENAI_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1/", "ANTHROPIC_API_KEY"),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GOOGLE_API_KEY"),  # Gemini
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "together-ai": ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    "deepinfra": ("https://api.deepinfra.com/v1/openai", "DEEPINFRA_API_KEY"),
    "perplexity-ai": ("https://api.perplexity.ai", "PERPLEXITY_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "fireworks": ("https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
    # Local servers that need no key.
    "ollama": (os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"), None),
    # OmniRoute: free local gateway to 150+ free model providers.
    "omniroute": (os.environ.get("OMNIROUTE_BASE_URL", "http://localhost:20128/v1"), "OMNIROUTE_API_KEY"),
}
LOCAL_LLMS = {"ollama", "omniroute"}  # they run without a key
# Extra headers per LLM provider. OpenRouter credits the app named here on its public app rankings.
LLM_HEADERS = {
    "openrouter": {"HTTP-Referer": "https://github.com/elifalanrocha/openring", "X-Title": "OpenRing"},
}
STT_PROVIDERS = {
    "deepgram": "DEEPGRAM_API_KEY",
    "whisper": None,  # free, runs on this computer (see whisper.py)
    "assembly-ai": "ASSEMBLYAI_API_KEY",
    "gladia": "GLADIA_API_KEY",
    "soniox": "SONIOX_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
    "11labs": "ELEVENLABS_API_KEY",
}
TTS_PROVIDERS = {
    "11labs": "ELEVENLABS_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "kokoro": None,
    "cartesia": "CARTESIA_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "openai": "OPENAI_API_KEY",
    "rime-ai": "RIME_API_KEY",
    "neuphonic": "NEUPHONIC_API_KEY",
    "smallest-ai": "SMALLEST_API_KEY",
    "inworld": "INWORLD_API_KEY",
}
# Other spellings of the same provider.
ALIASES = {"deep-seek": "deepseek", "elevenlabs": "11labs", "gemini": "google", "assemblyai": "assembly-ai",
           "rime": "rime-ai", "smallest": "smallest-ai", "together": "together-ai", "perplexity": "perplexity-ai",
           "faster-whisper": "whisper"}
PROVIDER_NAMES = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google Gemini", "groq": "Groq", "openrouter": "OpenRouter",
    "deepseek": "DeepSeek", "xai": "xAI Grok", "cerebras": "Cerebras", "together-ai": "Together AI",
    "deepinfra": "DeepInfra", "perplexity-ai": "Perplexity", "mistral": "Mistral", "fireworks": "Fireworks AI",
    "ollama": "Ollama", "omniroute": "OmniRoute", "deepgram": "Deepgram", "assembly-ai": "AssemblyAI",
    "gladia": "Gladia", "soniox": "Soniox", "cartesia": "Cartesia", "11labs": "ElevenLabs", "kokoro": "Kokoro",
    "rime-ai": "Rime", "neuphonic": "Neuphonic", "smallest-ai": "Smallest AI", "inworld": "Inworld",
    "whisper": "Whisper",
}


def _canonical(name: Any) -> str:
    name = str(name or "").lower()
    return ALIASES.get(name, name)

DEFAULT_TRANSCRIBER = {"provider": "deepgram", "model": "nova-3", "language": "multi"}
DEFAULT_VOICE = {"provider": "kokoro", "voiceId": "af_heart"}

# The free Kokoro voice runs from two model files (about 120 MB), kept in Pipecat's cache folder.
# At startup OpenRing downloads them if needed and loads the model once, in the background, so a
# call never waits for the download and does not reload the model (seconds on a laptop CPU).
# The int8 model is a quarter of the full one's size and a little faster on CPU.
KOKORO_DIR = Path.home() / ".cache" / "pipecat" / "kokoro-onnx"
KOKORO_FILES = {  # model first, then voices
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}
_kokoro: dict[str, Any] = {"thread": None, "bytes": 0, "error": "", "model": None}
_kokoro_lock = threading.Lock()


def kokoro_installed() -> bool:
    return importlib.util.find_spec("kokoro_onnx") is not None


def kokoro_ready() -> bool:
    return all((KOKORO_DIR / name).exists() for name in KOKORO_FILES)


def warm_up() -> None:
    """Get the call machinery ready in the background, so the first call starts as fast as the rest."""

    def load_modules():  # importing them takes seconds on a laptop
        import pipecat.audio.vad.silero  # noqa: F401
        import pipecat.services.deepgram.stt  # noqa: F401
        import pipecat.services.openai.llm  # noqa: F401
        import pipecat.transports.smallwebrtc.transport  # noqa: F401

    threading.Thread(target=load_modules, name="warm-up", daemon=True).start()
    if kokoro_installed():
        prepare_kokoro()


def prepare_kokoro() -> None:
    """In the background: download the Kokoro model if it is missing, then load it. Safe to repeat."""
    thread = _kokoro["thread"]
    if thread and thread.is_alive():
        return
    _kokoro.update(bytes=0, error="")
    _kokoro["thread"] = threading.Thread(target=_prepare_kokoro, name="kokoro", daemon=True)
    _kokoro["thread"].start()


def _prepare_kokoro() -> None:
    from loguru import logger

    try:
        _download_kokoro()
        shared_kokoro().create("Hi", voice="af_heart", lang="en-us")  # the first synthesis is the slowest
        logger.info("the free Kokoro voice is ready")
    except Exception as exc:  # network errors, full disk...: report them and retry on the next call
        _kokoro["error"] = str(exc) or type(exc).__name__
        logger.error("preparing the Kokoro voice failed: {}", _kokoro["error"])


def _download_kokoro() -> None:
    import httpx

    KOKORO_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in KOKORO_FILES.items():
        dest = KOKORO_DIR / name
        if dest.exists():
            continue
        part = dest.with_name(name + ".part")  # renamed when complete, so a cut download starts over
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response, part.open("wb") as f:
            response.raise_for_status()
            for chunk in response.iter_bytes(1 << 20):
                f.write(chunk)
                _kokoro["bytes"] += len(chunk)
        part.replace(dest)


def shared_kokoro() -> Any:
    """The Kokoro model, loaded once per process and shared by every call."""
    with _kokoro_lock:
        if _kokoro["model"] is None:
            from kokoro_onnx import Kokoro

            model_path, voices_path = (str(KOKORO_DIR / name) for name in KOKORO_FILES)
            _kokoro["model"] = Kokoro(model_path, voices_path)
        return _kokoro["model"]


def kokoro_status() -> str:
    """Why a call with the Kokoro voice cannot start yet. Restarts a download that failed."""
    error = _kokoro["error"]
    prepare_kokoro()
    if error:
        return f"downloading the free Kokoro voice failed ({error}); trying again, check the internet connection"
    mb = _kokoro["bytes"] // 1_000_000
    return f"the free Kokoro voice is still downloading ({mb} of about 120 MB, one time only); try again in a minute"


def prepare_local(assistant: dict[str, Any]) -> None:
    """Start getting the local models an assistant uses (Whisper's, its fallbacks' too), so its first call
    doesn't wait for a download."""
    if not whisper.installed():
        return
    for transcriber in [assistant.get("transcriber") or {}, *fallback_layers(assistant)["transcriber"]]:
        if _canonical(transcriber.get("provider")) == "whisper":
            try:
                whisper.prepare(transcriber.get("model") or whisper.DEFAULT)
            except ValueError:
                pass  # an unknown model is reported when the assistant is saved


def _whisper_row() -> dict[str, Any]:
    # Ready once a model is loaded; None ("local") while installed but not needed yet, like Ollama.
    loaded = any(whisper.ready(m) for m in whisper.MODELS)
    row: dict[str, Any] = {"id": "whisper", "name": "Whisper", "kind": "transcriber", "env": None, "free": True,
                           "ready": True if loaded else None if whisper.installed() else False}
    loading = whisper.loading()
    if not whisper.installed():
        row["note"] = "not installed: pip install 'openring[whisper]'"
    elif loading:
        row["note"] = f"getting the {loading[0]} model (about {whisper.MODELS[loading[0]][1]}, one time)"
    elif not loaded:
        row["note"] = (f"Free, on this computer. Downloads its model the first time an assistant uses it "
                       f"({whisper.DEFAULT}: about {whisper.MODELS[whisper.DEFAULT][1]})")
    return row


def provider_status(settings: Any) -> list[dict[str, Any]]:
    """Each provider the dashboard lists, and whether it is ready to use. Keys are never returned."""
    def has(env: str | None) -> bool:
        return bool(env and os.environ.get(env, "").strip())

    rows = [{"id": pid, "name": PROVIDER_NAMES.get(pid, pid), "kind": "transcriber", "env": env, "ready": has(env)}
            if pid != "whisper" else _whisper_row() for pid, env in STT_PROVIDERS.items()]
    for pid, (base_url, env) in LLM_PROVIDERS.items():
        row = {"id": pid, "name": PROVIDER_NAMES.get(pid, pid), "kind": "model", "env": env, "ready": has(env)}
        if pid in LOCAL_LLMS:  # a local server that works without a key, when it is running
            row.update(env=None, free=True, ready=None,
                       note=f"Works while {PROVIDER_NAMES[pid]} runs at {base_url}")
        rows.append(row)
    for pid, env in TTS_PROVIDERS.items():
        if env and pid not in ALIASES:
            rows.append({"id": pid, "name": PROVIDER_NAMES.get(pid, pid), "kind": "voice", "env": env,
                         "ready": has(env)})
    kokoro = {"id": "kokoro", "name": "Kokoro", "kind": "voice", "env": None, "free": True,
              "ready": kokoro_installed() and kokoro_ready()}
    if not kokoro_installed():
        kokoro["note"] = "not installed: pip install 'openring[kokoro]'"
    elif not kokoro_ready():
        kokoro["note"] = (f"download failed: {_kokoro['error']}" if _kokoro["error"]
                          else f"downloading, {_kokoro['bytes'] // 1_000_000} of about 120 MB")
    rows.append(kokoro)
    rows.append({"id": "twilio", "name": "Twilio", "kind": "telephony", "env": "TWILIO_AUTH_TOKEN",
                 "ready": bool(settings.twilio_account_sid and settings.twilio_auth_token and settings.public_url),
                 "note": None if settings.public_url else "set PUBLIC_URL so Twilio can reach this server"})
    rows.append({"id": "telnyx", "name": "Telnyx", "kind": "telephony", "env": "TELNYX_API_KEY",
                 "ready": bool(settings.telnyx_api_key and settings.telnyx_public_key and settings.public_url),
                 "note": None if settings.public_url else "set PUBLIC_URL so Telnyx can reach this server"})
    rows.append({"id": "plivo", "name": "Plivo", "kind": "telephony", "env": "PLIVO_AUTH_TOKEN",
                 "ready": bool(settings.plivo_auth_id and settings.plivo_auth_token and settings.public_url),
                 "note": None if settings.public_url else "set PUBLIC_URL so Plivo can reach this server"})
    rows.append({"id": "vonage", "name": "Vonage", "kind": "telephony", "env": "VONAGE_APPLICATION_ID",
                 "ready": bool(settings.vonage_application_id and settings.vonage_private_key
                               and settings.vonage_signature_secret and settings.public_url),
                 "note": None if settings.public_url else "set PUBLIC_URL so Vonage can reach this server"})
    rows.append({"id": "genesys", "name": "Genesys Cloud", "kind": "telephony", "env": "GENESYS_API_KEY",
                 "ready": bool(settings.genesys_api_key and settings.genesys_client_secret and settings.public_url),
                 "note": None if settings.public_url else "set PUBLIC_URL so Genesys can reach this server"})
    return rows


def default_model() -> dict[str, Any]:
    return {
        "provider": os.environ.get("DEFAULT_LLM_PROVIDER", "openai"),
        "model": os.environ.get("DEFAULT_LLM_MODEL", "gpt-4.1-mini"),
    }


# Words to recognize. Each transcriber has its limits; these fit all of them (AssemblyAI's are the tightest).
KEYWORD = re.compile(r"[^\W_]+(:-?\d+)?")  # one word, and how much to boost it, as Deepgram's `keywords` takes it
MAX_WORDS = 100
MAX_WORD_CHARS = 50
OLD_DEEPGRAM = ("nova-2", "nova-1", "enhanced", "base")  # take `keywords`, refuse `keyterm`; nova-3 the reverse
CARTESIA_WORD_MODELS = ("ink-2", "ink-preview")


@dataclass
class StackPlan:
    stt_provider: str
    stt_model: str | None
    stt_language: str | None
    llm_provider: str
    llm_model: str
    llm_base_url: str | None
    llm_api_key_env: str | None
    llm_api_key: str | None
    temperature: float | None
    tts_provider: str
    tts_voice: str | None
    tts_model: str | None
    tts_language: str | None
    messages: list[dict[str, str]] = field(default_factory=list)
    first_message: str | None = None
    end_call_phrases: list[str] = field(default_factory=list)
    server_url: str | None = None
    assistant: dict[str, Any] = field(default_factory=dict)  # the saved document, for tools and webhooks
    # Stacks to fall back to, per layer ("transcriber", "model", "voice"); each differs from this plan in
    # that layer only. Fallbacks that can't be used (unknown provider, missing key) are left out, with
    # the reason in `warnings`.
    fallbacks: dict[str, list[StackPlan]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # A model slower than this to start answering (ms) hands the turn to the next fallback model.
    max_latency_ms: int | None = None
    # The model route the models came from (`routes.expand`): {"id", "name", "cooldownMinutes"}.
    route: dict[str, Any] | None = None
    # Vapi's call limits, in seconds: the call ends when it has lasted this long, or when the caller has said
    # nothing for this long; Vapi's defaults. `end_call_message` is said before the line is hung up.
    max_duration_seconds: float = 600
    silence_timeout_seconds: float = 30
    end_call_message: str | None = None
    # Vapi's messagePlan: said when the caller goes quiet after the assistant spoke (up to `idle_max_spoken`).
    idle_messages: list[str] = field(default_factory=list)
    idle_timeout_seconds: float = 10
    idle_max_spoken: int = 3
    idle_reset_on_speech: bool = False
    # A voicemail answered: say `first_message` (the assistant's `voicemailMessage`) and hang up.
    leave_message: bool = False
    # Vapi's artifactPlan.recordingEnabled, off unless asked for (see recordings.py).
    record: bool = False
    # Settings for the words the transcriber should listen for (`stt_words`).
    stt_words: dict[str, Any] = field(default_factory=dict)
    # The other assistants a handoff can reach on this call (squads.py).
    team: list[StackPlan] = field(default_factory=list)


def _require_key(env_name: str | None, what: str) -> None:
    if env_name and not os.environ.get(env_name):
        where = os.environ.get("OPENRING_ENV_FILE")  # set by the `openring` command
        hint = f": add it to {where} and restart OpenRing" if where else " environment variable"
        raise ProviderError(f"{what} needs the {env_name}{hint}")


def _with_defaults(given: dict[str, Any], defaults: dict[str, Any], default_provider: str) -> dict[str, Any]:
    """Fill in the defaults, but only use the default model or voice with the default provider."""
    merged = {**defaults, **given}
    if _canonical(merged.get("provider")) != default_provider:
        for key in ("model", "voiceId"):
            if key not in given:
                merged.pop(key, None)
    return merged


def _supported(table: dict[str, Any], extra: tuple[str, ...] = ()) -> list[str]:
    return sorted([*(k for k in table if k not in ALIASES), *extra])


# With "language": "multi" the assistant follows the caller's language: the transcriber detects it, this line
# tells the model to answer in it, and a multilingual voice reads whatever language it is given.
FOLLOW_LANGUAGE = "Reply in the language the caller is speaking, and switch whenever they switch."
MULTILINGUAL_VOICES = {"elevenlabs", "openai"}


# Vapi's ranges for the call limits: (field, lowest, highest).
LIMITS = (("maxDurationSeconds", 10, 43_200), ("silenceTimeoutSeconds", 5, 3_600))
IDLE_LIMITS = (("idleTimeoutSeconds", 5, 60), ("idleMessageMaxSpokenCount", 1, 10))


def call_limits(assistant: dict[str, Any]) -> dict[str, Any]:
    """The assistant's call limits and idle messages as StackPlan fields; ProviderError for values out of range."""
    def number(where: dict[str, Any], name: str, low: float, high: float) -> float | None:
        value = where.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ProviderError(f"{name} goes from {low} to {high}")
        return value

    plan = assistant.get("messagePlan") or {}
    if not isinstance(plan, dict):
        raise ProviderError("messagePlan must be an object")
    messages = plan.get("idleMessages") or []
    if not isinstance(messages, list) or not all(isinstance(m, str) for m in messages):
        raise ProviderError("messagePlan.idleMessages must be a list of sentences")
    detection = assistant.get("voicemailDetection")
    if detection is not None and detection != "off" and not isinstance(detection, dict):
        raise ProviderError('voicemailDetection is "off" or an object, like {"provider": "twilio"}')
    if isinstance(detection, dict):
        number(detection, "beepMaxAwaitSeconds", 0, 60)
        number(detection, "machineDetectionTimeout", 0, 600)
    if assistant.get("voicemailMessage") is not None and not isinstance(assistant["voicemailMessage"], str):
        raise ProviderError("voicemailMessage is a sentence, or the URL of a .wav or .mp3")
    if assistant.get("endCallMessage") is not None and not isinstance(assistant["endCallMessage"], str):
        raise ProviderError("endCallMessage must be a sentence")
    artifact = assistant.get("artifactPlan")
    if artifact is not None and not isinstance(artifact, dict):
        raise ProviderError("artifactPlan must be an object")
    limits = {f: number(assistant, f, low, high) for f, low, high in LIMITS}
    idle = {f: number(plan, f, low, high) for f, low, high in IDLE_LIMITS}
    out: dict[str, Any] = {"idle_messages": [m.strip() for m in messages if m.strip()],
                           "idle_reset_on_speech": bool(plan.get("idleMessageResetCountOnUserSpeechEnabled")),
                           "end_call_message": assistant.get("endCallMessage") or None,
                           "record": (artifact or {}).get("recordingEnabled") is True}
    for key, value in (("max_duration_seconds", limits["maxDurationSeconds"]),
                       ("silence_timeout_seconds", limits["silenceTimeoutSeconds"]),
                       ("idle_timeout_seconds", idle["idleTimeoutSeconds"]),
                       ("idle_max_spoken", idle["idleMessageMaxSpokenCount"])):
        if value is not None:
            out[key] = int(value) if key == "idle_max_spoken" else value
    return out


def fallback_layers(assistant: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The assistant's fallback providers in Vapi's shape: `transcriber.fallbackPlan.transcribers`,
    `voice.fallbackPlan.voices` and `model.fallbackModels`. A fallback model is a model name on the same
    provider (as on Vapi) or, OpenRing's addition, a `{"provider", "model"}` object for another provider.
    """
    transcriber = assistant.get("transcriber") or {}
    voice = assistant.get("voice") or {}
    model = assistant.get("model") or {}
    shared = {k: v for k, v in model.items() if k not in ("provider", "model", "url", "apiKey", "fallbackModels")}
    models = []
    for alt in model.get("fallbackModels") or []:
        if isinstance(alt, str) and alt:
            models.append({**model, "model": alt})
        elif isinstance(alt, dict) and alt.get("model"):
            models.append({**shared, **alt})
    def listed(layer: dict[str, Any], key: str) -> list[dict[str, Any]]:
        return [x for x in (layer.get("fallbackPlan") or {}).get(key) or [] if isinstance(x, dict)]

    return {"transcriber": listed(transcriber, "transcribers"), "model": models, "voice": listed(voice, "voices")}


def _word_list(transcriber: dict[str, Any], name: str) -> list[str]:
    words = transcriber.get(name)
    if words is None:
        return []
    if not isinstance(words, list) or not all(isinstance(word, str) for word in words):
        raise ProviderError(f"transcriber.{name} must be a list of words")
    words = [word.strip() for word in words if word.strip()]
    for word in words:
        if len(word) > MAX_WORD_CHARS or not word.isprintable():
            raise ProviderError(f"transcriber.{name}: each one has at most {MAX_WORD_CHARS} characters, on one line")
    return words


def stt_words(transcriber: dict[str, Any], provider: str, model: str | None) -> tuple[dict[str, Any], list[str]]:
    """The settings for Vapi's `keyterm`, `keytermsPrompt` and `keywords` on this transcriber, and what it can't use.

    Deepgram refuses the connection when a model gets the other field, which would end the call, so whichever
    field the assistant used goes to the one its model takes."""
    terms = _word_list(transcriber, "keyterm") + _word_list(transcriber, "keytermsPrompt")
    keywords = _word_list(transcriber, "keywords")
    for word in keywords:
        if not KEYWORD.fullmatch(word):
            raise ProviderError(f"transcriber.keywords: '{word}' must be one word, or a word and a whole number "
                                "like word:3; use transcriber.keyterm for phrases")
    if len(set(terms + keywords)) > MAX_WORDS:
        raise ProviderError(f"transcriber: at most {MAX_WORDS} words to recognize")
    if not terms and not keywords:
        return {}, []

    settings: dict[str, Any] = {}
    warnings: list[str] = []
    if provider == "deepgram" and (model == "nova" or (model or "").startswith(OLD_DEEPGRAM)):
        phrases = [term for term in terms if not KEYWORD.fullmatch(term)]
        if phrases:
            warnings.append(f"deepgram {model} recognizes single words only; ignoring {', '.join(phrases)}")
        settings["keywords"] = list(dict.fromkeys(keywords + [term for term in terms if term not in phrases]))
    else:
        plain = list(dict.fromkeys(terms + [word.split(":")[0] for word in keywords if ":-" not in word]))
        lowered = [word for word in keywords if ":-" in word]
        if lowered:
            warnings.append(f"only older deepgram models can lower a word; ignoring {', '.join(lowered)}")
        if provider == "deepgram" and (model or "nova-3").startswith("nova-3"):
            settings["keyterm"] = plain
        elif provider == "assembly-ai" and "multilingual" not in (model or ""):
            settings["keyterms_prompt"] = plain
        elif provider == "11labs":
            settings["keyterms"] = plain
        elif provider == "cartesia" and (model or "").startswith(CARTESIA_WORD_MODELS):
            settings["keyterm"] = plain
        elif provider == "whisper":
            settings["hotwords"] = " ".join(plain)
        else:
            warnings.append(f"transcriber {' '.join(filter(None, (provider, model)))} can't be given words to "
                            "recognize; ignoring keyterm and keywords")
    return {key: value for key, value in settings.items() if value}, warnings


def resolve_plan(assistant: dict[str, Any], *, check_keys: bool = True, with_fallbacks: bool = True) -> StackPlan:
    transcriber = _with_defaults(assistant.get("transcriber") or {}, DEFAULT_TRANSCRIBER, "deepgram")
    model = {**default_model(), **(assistant.get("model") or {})}
    voice = _with_defaults(assistant.get("voice") or {}, DEFAULT_VOICE, "kokoro")

    stt_provider = _canonical(transcriber["provider"])
    if stt_provider not in STT_PROVIDERS:
        raise ProviderError(f"transcriber provider '{stt_provider}' not supported yet: {_supported(STT_PROVIDERS)}")
    stt_model = transcriber.get("model")
    if stt_provider == "whisper":
        stt_model = stt_model or whisper.DEFAULT
        try:
            whisper.model_id(stt_model)
        except ValueError as exc:
            raise ProviderError(f"transcriber.model: {exc}") from exc
    words, word_warnings = stt_words(transcriber, stt_provider, stt_model)

    llm_provider = _canonical(model["provider"])
    llm_api_key = None
    if llm_provider == "custom-llm":
        base_url = model.get("url")
        if not base_url:
            raise ProviderError("model.provider 'custom-llm' requires model.url (an OpenAI-compatible base URL)")
        try:
            netguard.check_url(str(base_url))
        except netguard.UnsafeURL as exc:
            raise ProviderError(f"model.url: {exc}") from exc
        key_env = None
        llm_api_key = model.get("apiKey") or "not-needed"
    elif llm_provider in LLM_PROVIDERS:
        base_url, key_env = LLM_PROVIDERS[llm_provider]
    else:
        raise ProviderError(
            f"model provider '{llm_provider}' not supported yet: {_supported(LLM_PROVIDERS, ('custom-llm',))}"
        )

    tts_provider = _canonical(voice["provider"])
    if tts_provider not in TTS_PROVIDERS:
        raise ProviderError(f"voice provider '{tts_provider}' not supported yet: {_supported(TTS_PROVIDERS)}")

    if check_keys:
        _require_key(STT_PROVIDERS[stt_provider], f"transcriber '{stt_provider}'")
        if llm_provider not in LOCAL_LLMS:  # local servers may run without a key
            _require_key(key_env, f"model provider '{llm_provider}'")
        _require_key(TTS_PROVIDERS[tts_provider], f"voice provider '{tts_provider}'")
        if tts_provider == "kokoro" and not kokoro_ready():
            raise ProviderError(kokoro_status())
        if stt_provider == "whisper":
            if not whisper.installed():
                raise ProviderError("transcriber 'whisper' needs: pip install 'openring[whisper]'")
            if not whisper.ready(stt_model):
                raise ProviderError(whisper.status(stt_model))

    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in model.get("messages") or []
        if m.get("role") and m.get("content")
    ]
    if model.get("systemPrompt"):
        messages.insert(0, {"role": "system", "content": model["systemPrompt"]})
    multilingual = transcriber.get("language") == "multi"
    if multilingual:
        messages.append({"role": "system", "content": FOLLOW_LANGUAGE})

    server = assistant.get("server") or {}
    plan = StackPlan(
        stt_provider=stt_provider,
        stt_model=stt_model,
        stt_language=transcriber.get("language"),
        llm_provider=llm_provider,
        llm_model=model["model"],
        llm_base_url=base_url,
        llm_api_key_env=key_env,
        llm_api_key=llm_api_key,
        temperature=model.get("temperature"),
        tts_provider="elevenlabs" if tts_provider == "11labs" else tts_provider,
        tts_voice=voice.get("voiceId"),
        tts_model=voice.get("model"),
        tts_language=voice.get("language") or transcriber.get("language"),
        messages=messages,
        first_message=assistant.get("firstMessage"),
        end_call_phrases=list(assistant.get("endCallPhrases") or []),
        server_url=server.get("url") or assistant.get("serverUrl"),
        assistant=assistant,
        route=model.get("route"),
        max_latency_ms=(model.get("route") or {}).get("maxLatencyMs") or model.get("maxLatencyMs"),
        stt_words=words,
        warnings=word_warnings,
        **call_limits(assistant),
    )
    if multilingual and plan.tts_provider not in MULTILINGUAL_VOICES:
        plan.warnings.append(f"the {plan.tts_provider} voice speaks one language; to follow callers in several "
                             "languages, use an 11labs or openai voice")
    if with_fallbacks:
        for layer, alternatives in fallback_layers(assistant).items():
            for alt in alternatives:
                try:
                    fallback = resolve_plan({**assistant, layer: alt}, check_keys=check_keys, with_fallbacks=False)
                except ProviderError as exc:
                    plan.warnings.append(f"{layer} fallback '{alt.get('provider')}' skipped: {exc}")
                    continue
                plan.fallbacks.setdefault(layer, []).append(fallback)
    return plan


def _language(code: str | None):
    if not code or code == "multi":
        return code
    from pipecat.transcriptions.language import Language

    try:
        return Language(code)
    except ValueError:
        return code


# Speech-to-text services by provider: (module, class). Each takes api_key and Settings(model, language).
STT_SERVICES = {
    "assembly-ai": ("pipecat.services.assemblyai.stt", "AssemblyAISTTService"),
    "gladia": ("pipecat.services.gladia.stt", "GladiaSTTService"),
    "soniox": ("pipecat.services.soniox.stt", "SonioxSTTService"),
    "cartesia": ("pipecat.services.cartesia.stt", "CartesiaSTTService"),
    "11labs": ("pipecat.services.elevenlabs.stt", "ElevenLabsRealtimeSTTService"),
}
# Text-to-speech services by provider: (module, class, whether it takes the call's language).
TTS_SERVICES = {
    "cartesia": ("pipecat.services.cartesia.tts", "CartesiaTTSService", True),
    "deepgram": ("pipecat.services.deepgram.tts", "DeepgramTTSService", False),
    "openai": ("pipecat.services.openai.tts", "OpenAITTSService", False),
    "rime-ai": ("pipecat.services.rime.tts", "RimeTTSService", True),
    "neuphonic": ("pipecat.services.neuphonic.tts", "NeuphonicTTSService", True),
    "smallest-ai": ("pipecat.services.smallest.tts", "SmallestTTSService", True),
    "inworld": ("pipecat.services.inworld.tts", "InworldTTSService", True),
}
DEFAULT_VOICES = {"openai": "alloy", "deepgram": "aura-2-thalia-en"}


def _service_class(module: str, name: str, provider: str):
    import importlib

    try:
        return getattr(importlib.import_module(module), name)
    except ImportError as exc:  # an optional dependency of that provider
        raise ProviderError(f"provider '{provider}' needs an extra package: {exc}") from exc


def _build_stt(plan: StackPlan):
    lang = _language(plan.stt_language)
    if plan.stt_provider == "whisper":  # free, runs locally; without a language, it detects the caller's
        return whisper.stt_service(plan.stt_model or whisper.DEFAULT, None if lang == "multi" else lang,
                                   plan.stt_words.get("hotwords"))
    if plan.stt_provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService

        return DeepgramSTTService(
            api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
            settings=DeepgramSTTService.Settings(model=plan.stt_model or "nova-3", language=lang, **plan.stt_words),
        )
    service = _service_class(*STT_SERVICES[plan.stt_provider], plan.stt_provider)
    settings: dict[str, Any] = dict(plan.stt_words)
    if plan.stt_model:
        settings["model"] = plan.stt_model
    if lang and lang != "multi":  # without a language, these services detect it
        settings["language"] = lang
    return service(api_key=os.environ.get(STT_PROVIDERS[plan.stt_provider] or "", ""),
                   settings=service.Settings(**settings))


def _deepgram_voice(voice: str | None, model: str | None) -> str:
    """Deepgram names a voice with its model and language ('aura-2-thalia-en'); Vapi uses the short name."""
    if not voice:
        return DEFAULT_VOICES["deepgram"]
    return voice if voice.startswith("aura") else f"{model or 'aura-2'}-{voice}-en"


def _build_tts(plan: StackPlan):
    if plan.tts_provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        tts_settings: dict[str, Any] = {"voice": plan.tts_voice}
        if plan.tts_model:
            tts_settings["model"] = plan.tts_model
        return ElevenLabsTTSService(
            api_key=os.environ.get("ELEVENLABS_API_KEY", ""),
            settings=ElevenLabsTTSService.Settings(**tts_settings),
        )
    if plan.tts_provider == "kokoro":  # free, runs locally on CPU
        try:
            import pipecat.services.kokoro.tts as kokoro_tts
            from pipecat.services.kokoro.tts import KokoroTTSService
        except ImportError as exc:  # optional dependency
            raise ProviderError("voice provider 'kokoro' needs: pip install 'pipecat-ai[kokoro]'") from exc

        # Pipecat loads a fresh model for every service it creates; hand it the shared one instead.
        kokoro_tts.Kokoro = lambda *_paths: shared_kokoro()

        kokoro_settings: dict[str, Any] = {"voice": plan.tts_voice}
        lang = _language(plan.tts_language)
        if lang and lang != "multi":
            kokoro_settings["language"] = lang
        model_path, voices_path = (str(KOKORO_DIR / name) for name in KOKORO_FILES)
        return KokoroTTSService(
            model_path=model_path,
            voices_path=voices_path,
            settings=KokoroTTSService.Settings(**kokoro_settings),
        )

    module, name, takes_language = TTS_SERVICES[plan.tts_provider]
    service = _service_class(module, name, plan.tts_provider)
    settings: dict[str, Any] = {}
    if plan.tts_provider == "deepgram":
        settings["voice"] = _deepgram_voice(plan.tts_voice, plan.tts_model)
    else:
        if plan.tts_voice or plan.tts_provider in DEFAULT_VOICES:
            settings["voice"] = plan.tts_voice or DEFAULT_VOICES[plan.tts_provider]
        if plan.tts_model:
            settings["model"] = plan.tts_model
    lang = _language(plan.tts_language)
    if takes_language and lang and lang != "multi":
        settings["language"] = lang
    return service(api_key=os.environ.get(TTS_PROVIDERS[plan.tts_provider] or "", ""),
                   settings=service.Settings(**settings))


def _llm_headers(provider: str) -> dict[str, str] | None:
    """OpenRing's name on requests to providers that credit apps; OPENRING_ATTRIBUTION=false leaves it out."""
    if os.environ.get("OPENRING_ATTRIBUTION", "").lower() == "false":
        return None
    return LLM_HEADERS.get(provider)


def build_services(plan: StackPlan):
    """Instantiate Pipecat STT, LLM and TTS services for a call. A layer with fallbacks becomes one switcher
    that runs the first provider and moves to the next when it fails (see `fallback.py`)."""
    if plan.team:
        return _build_team(plan)
    stt = _labeled(_build_stt(plan), "transcriber", plan)
    llm = _labeled(_build_llm(plan), "model", plan)
    tts = _labeled(_build_tts(plan), "voice", plan)
    if not plan.fallbacks:
        return stt, llm, tts
    from . import fallback

    layers = {"transcriber": (stt, _build_stt), "model": (llm, _build_llm), "voice": (tts, _build_tts)}
    built = {}
    for layer, (service, build) in layers.items():
        alternatives = plan.fallbacks.get(layer) or []
        services = [service, *(_labeled(build(alt), layer, alt) for alt in alternatives)]
        built[layer] = fallback.switcher(services, llm=layer == "model") if alternatives else service
    return built["transcriber"], built["model"], built["voice"]


def _build_team(plan: StackPlan):
    """The services of everyone on the call: one switcher per layer, with each assistant's providers as a group. They
    share a transcriber or a voice they have in common. Each has a model of its own, for its own tools."""
    from . import fallback

    def stt_key(p: StackPlan) -> Any:
        return p.stt_provider, p.stt_model, p.stt_language, str(sorted(p.stt_words.items()))

    def tts_key(p: StackPlan) -> Any:
        return p.tts_provider, p.tts_voice, p.tts_model, p.tts_language

    built = []
    layers = (("transcriber", _build_stt, stt_key), ("model", _build_llm, id), ("voice", _build_tts, tts_key))
    for layer, build, key in layers:
        services: dict[Any, Any] = {}
        groups = []
        for member in (plan, *plan.team):
            group = []
            for option in (member, *member.fallbacks.get(layer, [])):
                if key(option) not in services:
                    services[key(option)] = _labeled(build(option), layer, option)
                group.append(services[key(option)])
            groups.append(group)
        if len(services) == 1:
            built.append(next(iter(services.values())))
        else:
            switcher = fallback.switcher(list(services.values()), llm=layer == "model")
            switcher.strategy.groups = groups
            built.append(switcher)
    return tuple(built)


def label(layer: str, plan: StackPlan) -> str:
    """How a provider shows up in the call record: 'deepgram nova-3', 'groq openai/gpt-oss-20b'."""
    if layer == "transcriber":
        return " ".join(x for x in (plan.stt_provider, plan.stt_model) if x)
    if layer == "model":
        return f"{plan.llm_provider} {plan.llm_model}"
    return " ".join(x for x in (plan.tts_provider, plan.tts_voice) if x)


def _labeled(service: Any, layer: str, plan: StackPlan) -> Any:
    service.openring_label = label(layer, plan)
    # What the call record prices this service by (prices.py); the model can be None: the provider's default.
    service.openring_price = {
        "transcriber": ("transcriber", plan.stt_provider, plan.stt_model, plan.stt_language),
        "model": ("model", plan.llm_provider, plan.llm_model, None),
        "voice": ("voice", plan.tts_provider, plan.tts_model, None),
    }[layer]
    if layer == "model":  # which model this is, so a route can rest it after it fails or is slow
        service.openring_model = (plan.llm_provider, plan.llm_model)
    return service


def _llm_class(provider: str | None) -> Any:
    """Pipecat's own service where a provider has rules for what it's sent (Mistral's roles, say); else OpenAI's."""
    if provider == "mistral":
        from pipecat.services.mistral.llm import MistralLLMService

        return MistralLLMService
    if provider == "deepseek":
        from pipecat.services.deepseek.llm import DeepSeekLLMService

        return DeepSeekLLMService
    if provider == "perplexity-ai":
        from pipecat.services.perplexity.llm import PerplexityLLMService

        return PerplexityLLMService
    from pipecat.services.openai.llm import OpenAILLMService

    return OpenAILLMService


def _build_llm(plan: StackPlan):
    service_class = _llm_class(plan.llm_provider)
    llm_settings: dict[str, Any] = {"model": plan.llm_model}
    if plan.temperature is not None:
        llm_settings["temperature"] = plan.temperature
    api_key = plan.llm_api_key or os.environ.get(plan.llm_api_key_env or "", "") or "not-needed"
    service = service_class(
        api_key=api_key,
        base_url=plan.llm_base_url,
        default_headers=_llm_headers(plan.llm_provider),
        settings=service_class.Settings(**llm_settings),
    )
    # Pipecat adds "developer" messages mid-call: a tool's late answer, a caller's empty turn. Providers that refuse the
    # role answer 400, which ends the call, so only OpenAI gets it. The others read those as "user" messages.
    service.supports_developer_role = plan.llm_provider == "openai"
    return service
