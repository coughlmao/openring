"""Whisper, the free local transcriber: model names, readiness, the shared model, and 8 kHz phone audio.

The last test transcribes real speech; it runs only where the Whisper tiny and Kokoro models are already
downloaded (a developer's machine), never in CI.
"""

import asyncio

import numpy as np
import pytest

from openring import providers, whisper
from openring.config import Settings
from openring.providers import ProviderError, build_services, resolve_plan

pytest.importorskip("faster_whisper")

LOCAL = {"transcriber": {"provider": "whisper", "language": "pt-BR"},
         "model": {"provider": "ollama", "model": "llama3.2"}, "voice": {"provider": "kokoro", "voiceId": "pf_dora"}}
# Building a Kokoro voice makes Pipecat download its model when it's missing (CI): build with a cloud voice.
BUILD = {**LOCAL, "voice": {"provider": "11labs", "voiceId": "v1"}}


class FakeModel:
    """Stands in for a loaded Whisper model and keeps what it was asked to transcribe."""

    supported_languages = ["en", "pt"]

    def __init__(self):
        self.heard = []

    def transcribe(self, audio, **options):
        self.heard.append((audio, options))
        segment = type("Segment", (), {"text": " Olá", "no_speech_prob": 0.0, "avg_logprob": -0.1})()
        return [segment], type("Info", (), {"language": "pt"})()


@pytest.fixture
def loaded(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake")
    model = FakeModel()
    monkeypatch.setitem(whisper._state["models"], "base", whisper._Greedy(model))
    return model


def test_whisper_needs_no_key_and_defaults_to_base(loaded):
    plan = resolve_plan(LOCAL, check_keys=False)
    assert (plan.stt_provider, plan.stt_model, plan.llm_provider) == ("whisper", "base", "ollama")
    assert resolve_plan({**LOCAL, "transcriber": {"provider": "faster-whisper"}}, check_keys=False).stt_model == "base"
    with pytest.raises(ProviderError, match="tiny, base"):
        resolve_plan({**LOCAL, "transcriber": {"provider": "whisper", "model": "huge"}}, check_keys=False)
    repo = resolve_plan({**LOCAL, "transcriber": {"provider": "whisper", "model": "org/my-whisper-ct2"}},
                        check_keys=False)
    assert whisper.model_id(repo.stt_model) == "org/my-whisper-ct2"


def test_a_call_waits_until_the_model_is_ready(monkeypatch):
    monkeypatch.setattr(providers, "kokoro_ready", lambda: True)
    started = []
    monkeypatch.setattr(whisper, "prepare", started.append)
    with pytest.raises(ProviderError, match="small model is still downloading"):
        resolve_plan({**LOCAL, "transcriber": {"provider": "whisper", "model": "small"}})
    assert started == ["small"]  # asking for it starts getting it


def test_saving_an_assistant_starts_getting_its_models(monkeypatch):
    started = []
    monkeypatch.setattr(whisper, "prepare", started.append)
    providers.prepare_local({**LOCAL, "transcriber": {"provider": "whisper", "model": "tiny", "fallbackPlan": {
        "transcribers": [{"provider": "whisper", "model": "small"}, {"provider": "deepgram"}]}}})
    assert started == ["tiny", "small"]


def test_calls_share_one_model_and_decode_greedily(loaded):
    plan = resolve_plan(BUILD, check_keys=False)
    first, _, _ = build_services(plan)
    second, _, _ = build_services(plan)
    assert type(first).__name__ == "LocalWhisper" and first._model is second._model
    assert first._settings.language == "pt"
    assert build_services(resolve_plan({**BUILD, "transcriber": {"provider": "whisper", "language": "multi"}},
                                       check_keys=False))[0]._settings.language is None  # Whisper detects it
    first._model.transcribe(np.zeros(16000, dtype=np.float32), language="pt")
    assert loaded.heard[-1][1]["beam_size"] == 1


def test_whisper_loads_on_apple_silicon_without_mlx(loaded, monkeypatch):
    """On macOS arm64, Pipecat 1.15 requires mlx_whisper just to import its Whisper module; ours uses
    faster-whisper, so a Mac without MLX must still get a transcriber (this failed on every M-series Mac)."""
    import importlib.util
    import platform
    import sys

    if importlib.util.find_spec("mlx_whisper") is not None:
        pytest.skip("MLX Whisper is installed here, so the import never needs the stand-in")
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    monkeypatch.delitem(sys.modules, "pipecat.services.whisper.stt", raising=False)
    monkeypatch.setattr(whisper, "_service", [])  # load Pipecat's module again, as on a fresh start
    stt = whisper.stt_service("base", "pt")
    assert type(stt).__name__ == "LocalWhisper"
    assert "mlx_whisper" not in sys.modules  # the stand-in is gone, so the MLX service still fails clearly


def test_words_to_recognize_reach_whisper_as_hotwords(loaded):
    plan = resolve_plan({**BUILD, "transcriber": {**BUILD["transcriber"], "keyterm": ["Clínica Sorriso"],
                                                  "keywords": ["Ana:2"]}}, check_keys=False)
    stt, _, _ = build_services(plan)
    assert stt._settings.hotwords == "Clínica Sorriso Ana"


def test_phone_audio_reaches_whisper_at_16_khz(loaded):
    stt, _, _ = build_services(resolve_plan(BUILD, check_keys=False))
    stt._sample_rate = 8000  # a phone call
    one_second = (np.sin(np.arange(8000) / 8000 * 2 * np.pi * 300) * 8000).astype(np.int16).tobytes()

    async def transcribe():
        return [frame async for frame in stt.run_stt(one_second)]

    frames = asyncio.run(transcribe())
    audio, options = loaded.heard[-1]
    assert len(audio) == pytest.approx(16000, abs=50) and options["language"] == "pt"
    assert any(getattr(f, "text", "").strip() == "Olá" for f in frames)


def test_the_providers_page_explains_whisper():
    row = next(r for r in providers.provider_status(Settings(db_path=":memory:")) if r["id"] == "whisper")
    assert row["kind"] == "transcriber" and row["free"] and row["env"] is None


def _cached(name: str) -> bool:
    try:
        from faster_whisper.utils import download_model

        download_model(name, local_files_only=True)
    except Exception:
        return False
    return providers.kokoro_installed() and providers.kokoro_ready()


@pytest.mark.skipif(not _cached("tiny"), reason="needs the Whisper tiny and Kokoro models on this machine")
def test_real_speech_from_a_phone_call_is_transcribed(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake")
    kokoro = providers.shared_kokoro()
    samples, rate = kokoro.create("Eu gostaria de marcar uma consulta para amanhã.", voice="pf_dora", lang="pt-br")
    phone = np.interp(np.arange(0, len(samples), rate / 8000), np.arange(len(samples)), samples)
    stt, _, _ = build_services(resolve_plan({**BUILD, "transcriber": {"provider": "whisper", "model": "tiny",
                                                                      "language": "pt-BR"}}, check_keys=False))
    stt._sample_rate = 8000

    async def transcribe():
        return [frame async for frame in stt.run_stt((phone * 32767).astype(np.int16).tobytes())]

    text = " ".join(getattr(f, "text", "") for f in asyncio.run(transcribe())).lower()
    assert "marcar uma consulta" in text  # tiny may hear "amanhã" as "a manhã"


def test_the_dashboard_lists_what_ollama_has(monkeypatch):
    import httpx
    from test_keys import signed_in

    from test_api import DASH

    client = signed_in()
    monkeypatch.setitem(providers.LLM_PROVIDERS, "ollama", ("http://127.0.0.1:9/v1", None))  # nothing listens
    assert client.get("/providers/ollama/models", headers=DASH).json() == {
        "running": False, "models": [], "url": "http://127.0.0.1:9"}

    async def tags(self, url, **_kw):
        assert url == "http://127.0.0.1:9/api/tags"
        return httpx.Response(200, json={"models": [{"name": "llama3.2:latest"}, {"name": "qwen2.5:3b"}]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", tags)
    assert client.get("/providers/ollama/models", headers=DASH).json()["models"] == ["llama3.2:latest", "qwen2.5:3b"]
    assert client.get("/providers/ollama/models").status_code == 401  # dashboard sessions only
