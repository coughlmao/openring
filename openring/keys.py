"""Connect providers from the dashboard: check a key with the provider, then keep it in the settings file.

People paste a key (or sign in, for OpenRouter); OpenRing asks the provider whether the key works before
saving it, so a typo shows up here and not halfway through a call. Saved keys apply at once, without a
restart, and are never sent back to the browser.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .providers import LLM_PROVIDERS, LOCAL_LLMS, PROVIDER_NAMES, STT_PROVIDERS, TTS_PROVIDERS

# Where people get a key for each provider (as the dashboard lists them).
KEY_PAGES = {
    "deepgram": "https://console.deepgram.com/",
    "assembly-ai": "https://www.assemblyai.com/app/api-keys",
    "gladia": "https://app.gladia.io/",
    "soniox": "https://console.soniox.com/",
    "cartesia": "https://play.cartesia.ai/keys",
    "11labs": "https://elevenlabs.io/app/settings/api-keys",
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "google": "https://aistudio.google.com/apikey",
    "groq": "https://console.groq.com/keys",
    "openrouter": "https://openrouter.ai/settings/keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "xai": "https://console.x.ai/",
    "cerebras": "https://cloud.cerebras.ai/",
    "together-ai": "https://api.together.ai/settings/api-keys",
    "deepinfra": "https://deepinfra.com/dash/api_keys",
    "perplexity-ai": "https://www.perplexity.ai/account/api/keys",
    "mistral": "https://console.mistral.ai/api-keys",
    "fireworks": "https://app.fireworks.ai/settings/users/api-keys",
    "rime-ai": "https://app.rime.ai/",
    "neuphonic": "https://app.neuphonic.com/",
    "smallest-ai": "https://console.smallest.ai/",
    "inworld": "https://platform.inworld.ai/",
    "twilio": "https://console.twilio.com/",
    "telnyx": "https://portal.telnyx.com/#/api-keys",
    "plivo": "https://console.plivo.com/dashboard/",
    "vonage": "https://dashboard.nexmo.com/applications",
    "genesys": "https://help.mypurecloud.com/articles/configure-audio-connector/",
}


def _connectors() -> dict[str, dict[str, Any]]:
    """Provider id -> the settings it needs and where people get them, for every provider with a key."""
    envs = {**STT_PROVIDERS, **TTS_PROVIDERS,
            **{pid: env for pid, (_, env) in LLM_PROVIDERS.items() if pid not in LOCAL_LLMS}}
    found = {pid: {"fields": [(env, "API key")], "keysUrl": KEY_PAGES[pid]}
             for pid, env in envs.items() if env and pid in KEY_PAGES}
    found["openrouter"]["oauth"] = True
    found["twilio"] = {"fields": [("TWILIO_ACCOUNT_SID", "Account SID"), ("TWILIO_AUTH_TOKEN", "Auth token")],
                       "keysUrl": KEY_PAGES["twilio"]}
    # The public key (Keys & Credentials > Public Key) checks that webhooks come from Telnyx.
    found["telnyx"] = {"fields": [("TELNYX_API_KEY", "API key"), ("TELNYX_PUBLIC_KEY", "Public key")],
                       "keysUrl": KEY_PAGES["telnyx"]}
    found["plivo"] = {"fields": [("PLIVO_AUTH_ID", "Auth ID"), ("PLIVO_AUTH_TOKEN", "Auth token")],
                      "keysUrl": KEY_PAGES["plivo"]}
    # The private key is the file Vonage lets you download when you create the application; the signature secret is
    # under Settings > API settings.
    found["vonage"] = {"fields": [("VONAGE_APPLICATION_ID", "Application ID"),
                                  ("VONAGE_PRIVATE_KEY", "Private key file"),
                                  ("VONAGE_SIGNATURE_SECRET", "Signature secret")],
                       "keysUrl": KEY_PAGES["vonage"]}
    # You choose both (the dashboard can generate them) and paste the same into Genesys's Audio Connector.
    found["genesys"] = {"fields": [("GENESYS_API_KEY", "API key"), ("GENESYS_CLIENT_SECRET", "Client secret")],
                        "keysUrl": KEY_PAGES["genesys"], "generate": True}
    return found


CONNECTORS = _connectors()
NAMES = {**PROVIDER_NAMES, "twilio": "Twilio", "telnyx": "Telnyx", "plivo": "Plivo", "vonage": "Vonage",
         "genesys": "Genesys Cloud"}
OPENROUTER_EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"
# Keys are single tokens; anything else (spaces, quotes, line breaks) would break the .env file.
VALUE_RE = re.compile(r"^[A-Za-z0-9_\-.:+/=]{8,512}$")
# A file's path may also have backslashes (Windows), but no spaces or quotes: the settings file has no quoting.
PATH_FIELDS = {"VONAGE_PRIVATE_KEY"}
PATH_RE = re.compile(r"^[A-Za-z0-9_\-.:+/=\\]{3,512}$")
TIMEOUT = 15


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# The cheapest authenticated GET each provider answers: (url, headers for a key). Model providers not
# listed here are checked with their OpenAI-compatible model list.
CHECKS = {
    "deepgram": ("https://api.deepgram.com/v1/projects", lambda k: {"Authorization": f"Token {k}"}),
    "11labs": ("https://api.elevenlabs.io/v1/user", lambda k: {"xi-api-key": k}),
    "openrouter": ("https://openrouter.ai/api/v1/key", _bearer),  # its model list is public
    "anthropic": ("https://api.anthropic.com/v1/models", lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}),
    "perplexity-ai": ("https://api.perplexity.ai/async/chat/completions", _bearer),  # it has no model list
    "assembly-ai": ("https://api.assemblyai.com/v2/transcript?limit=1", lambda k: {"Authorization": k}),
    "gladia": ("https://api.gladia.io/v2/pre-recorded?limit=1", lambda k: {"x-gladia-key": k}),
    "soniox": ("https://api.soniox.com/v1/models", _bearer),
    "cartesia": ("https://api.cartesia.ai/voices", lambda k: {"X-API-Key": k, "Cartesia-Version": "2024-11-13"}),
    "neuphonic": ("https://api.neuphonic.com/voices", lambda k: {"X-API-KEY": k}),
    "inworld": ("https://api.inworld.ai/tts/v1/voices", lambda k: {"Authorization": f"Basic {k}"}),
    # Rime has no read-only endpoint; a GET to its speech endpoint only tells a rejected key apart.
    "rime-ai": ("https://users.rime.ai/v1/rime-tts", _bearer),
    "telnyx": ("https://api.telnyx.com/v2/balance", _bearer),
}
ONLY_REJECTIONS = {"rime-ai"}
# Providers with no endpoint that checks a key; their keys are saved as they are.
UNCHECKED = {"smallest-ai", "genesys"}


class ConnectError(ValueError):
    """The values cannot be used: malformed, rejected by the provider, or the provider is unreachable."""


def clean_values(provider: str, values: dict[str, Any]) -> dict[str, str]:
    if provider not in CONNECTORS:
        raise ConnectError(f"{provider} cannot be connected from the dashboard")
    cleaned = {}
    for env, label in CONNECTORS[provider]["fields"]:
        value = str(values.get(env) or "").strip()
        if not value:
            raise ConnectError(f"enter the {label.lower()}")
        if not (PATH_RE if env in PATH_FIELDS else VALUE_RE).match(value):
            raise ConnectError(f"that doesn't look like a {NAMES[provider]} {label.lower()}; paste it again")
        cleaned[env] = value
    if provider == "vonage":
        from . import vonage

        try:  # the file has to be there, and be a key, before a call finds out
            vonage.make_jwt(cleaned["VONAGE_APPLICATION_ID"], cleaned["VONAGE_PRIVATE_KEY"])
        except vonage.VonageError as exc:
            raise ConnectError(str(exc)) from exc
    if provider == "genesys":
        try:
            base64.b64decode(cleaned["GENESYS_CLIENT_SECRET"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ConnectError("the client secret must be base64, as Genesys expects; press Generate") from exc
    return cleaned


def current_values(provider: str) -> dict[str, str]:
    return {env: os.environ.get(env, "").strip() for env, _ in CONNECTORS[provider]["fields"]}


def env_of(provider: str) -> str:
    """The setting that holds a one-key provider's key."""
    return CONNECTORS[provider]["fields"][0][0]


def _check_request(provider: str, values: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """(url, httpx options) for checking these values with the provider."""
    if provider == "twilio":
        sid = values["TWILIO_ACCOUNT_SID"]
        return (f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
                {"auth": (sid, values["TWILIO_AUTH_TOKEN"])})
    if provider == "plivo":
        auth_id = values["PLIVO_AUTH_ID"]
        return f"https://api.plivo.com/v1/Account/{auth_id}/", {"auth": (auth_id, values["PLIVO_AUTH_TOKEN"])}
    if provider == "vonage":  # listing one call takes the same token as placing one
        from . import vonage

        token = vonage.make_jwt(values["VONAGE_APPLICATION_ID"], values["VONAGE_PRIVATE_KEY"])
        return f"{vonage.API}?page_size=1", {"headers": _bearer(token)}
    key = values[env_of(provider)]
    if provider in CHECKS:
        url, headers = CHECKS[provider]
        return url, {"headers": headers(key)}
    base_url = (LLM_PROVIDERS[provider][0] or "https://api.openai.com/v1").rstrip("/")
    return base_url + "/models", {"headers": _bearer(key)}


async def check(provider: str, values: dict[str, str], client: httpx.AsyncClient | None = None) -> bool:
    """Raise ConnectError unless the provider accepts these values. False: this provider can't be checked."""
    if provider in UNCHECKED:
        return False
    name = NAMES[provider]
    url, options = _check_request(provider, values)
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT) as own:
                res = await own.get(url, **options)
        else:
            res = await client.get(url, **options)
    except httpx.HTTPError as exc:
        raise ConnectError(f"could not reach {name} ({type(exc).__name__}); check your internet connection") from exc
    # Some providers answer a bad key with 400 ("please pass a valid API key") instead of 401.
    rejected = res.status_code in (401, 403) or (res.status_code == 400 and "key" in res.text.lower())
    # A restricted ElevenLabs key may not read the account, yet it still works for speech.
    if provider == "11labs" and res.status_code == 401 and "missing_permissions" in res.text:
        return True
    if rejected:
        raise ConnectError(f"{name} rejected this key; copy it again from {name}")
    if res.status_code == 200 or provider in ONLY_REJECTIONS:
        return True
    raise ConnectError(f"{name} answered with an error ({res.status_code}); try again in a minute")


# Providers whose API says how much is left on the account.
BALANCES = ("openrouter", "deepgram", "11labs", "twilio", "telnyx", "plivo")
LOW_MONEY = 2.0  # dollars; and under a tenth of a character allowance


def _money(left: float | None, unit: str, *, limit: float | None = None, used: float | None = None) -> dict[str, Any]:
    low = left is not None and left < LOW_MONEY
    return {"left": left, "unit": unit.upper(), "limit": limit, "used": used, "low": low}


async def _read_balance(provider: str, values: dict[str, str], client: httpx.AsyncClient) -> dict[str, Any] | None:
    if provider == "openrouter":
        auth = _bearer(values["OPENROUTER_API_KEY"])
        res = await client.get("https://openrouter.ai/api/v1/key", headers=auth)
        if res.status_code != 200:
            return None
        key = res.json().get("data") or {}
        if key.get("limit") is not None and key.get("limit_remaining") is not None:
            return _money(float(key["limit_remaining"]), "usd", limit=float(key["limit"]))
        # A key without a spending limit draws on the account's credits, which only a management key can read.
        res = await client.get("https://openrouter.ai/api/v1/credits", headers=auth)
        if res.status_code == 200:
            total = res.json().get("data") or {}
            bought, spent = float(total.get("total_credits") or 0), float(total.get("total_usage") or 0)
            return _money(bought - spent, "usd", limit=bought)
        return _money(None, "usd", used=float(key.get("usage") or 0))
    if provider == "deepgram":
        auth = {"Authorization": f"Token {values['DEEPGRAM_API_KEY']}"}
        res = await client.get("https://api.deepgram.com/v1/projects", headers=auth)
        projects = (res.json().get("projects") or []) if res.status_code == 200 else []
        if not projects:
            return None
        res = await client.get(f"https://api.deepgram.com/v1/projects/{projects[0]['project_id']}/balances",
                               headers=auth)
        if res.status_code == 403:  # keys made for speech only lack the billing scope
            return {"unavailable": "This key can't read the balance; a Deepgram key with billing:read can."}
        if res.status_code != 200:
            return None
        amounts = [b for b in res.json().get("balances") or [] if str(b.get("units", "usd")).lower() == "usd"]
        return _money(round(sum(float(b.get("amount") or 0) for b in amounts), 2), "usd")
    if provider == "11labs":
        res = await client.get("https://api.elevenlabs.io/v1/user/subscription",
                               headers={"xi-api-key": values["ELEVENLABS_API_KEY"]})
        if res.status_code != 200:
            return None
        sub = res.json()
        limit, used = int(sub.get("character_limit") or 0), int(sub.get("character_count") or 0)
        resets = sub.get("next_character_count_reset_unix")
        return {"left": max(limit - used, 0), "unit": "characters", "limit": limit, "used": used,
                "low": limit > 0 and limit - used < limit / 10,
                "resetsAt": datetime.fromtimestamp(resets, timezone.utc).isoformat() if resets else None}
    if provider == "twilio":
        sid = values["TWILIO_ACCOUNT_SID"]
        res = await client.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Balance.json",
                               auth=(sid, values["TWILIO_AUTH_TOKEN"]))
        if res.status_code != 200:
            return None
        body = res.json()
        return _money(float(body.get("balance") or 0), str(body.get("currency") or "usd"))
    if provider == "plivo":
        auth_id = values["PLIVO_AUTH_ID"]
        res = await client.get(f"https://api.plivo.com/v1/Account/{auth_id}/",
                               auth=(auth_id, values["PLIVO_AUTH_TOKEN"]))
        if res.status_code != 200:
            return None
        return _money(float(res.json().get("cash_credits") or 0), "usd")
    if provider == "telnyx":
        res = await client.get("https://api.telnyx.com/v2/balance", headers=_bearer(values["TELNYX_API_KEY"]))
        if res.status_code != 200:
            return None
        data = res.json().get("data") or {}
        left = data.get("available_credit", data.get("balance"))
        return _money(float(left or 0), str(data.get("currency") or "usd"))
    return None


async def balance(
    provider: str, values: dict[str, str], client: httpx.AsyncClient | None = None
) -> dict[str, Any] | None:
    """What's left on the account as the provider reports it: money or characters. None when it won't say."""
    if provider not in BALANCES:
        return None
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT) as own:
                return await _read_balance(provider, values, own)
        return await _read_balance(provider, values, client)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):  # unreachable, or an answer we can't read
        return None


async def exchange_openrouter_code(code: str, verifier: str, client: httpx.AsyncClient | None = None) -> str:
    """Trade the code from OpenRouter's sign-in page for an API key (OAuth PKCE)."""
    body = {"code": code, "code_verifier": verifier, "code_challenge_method": "S256"}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT) as own:
                res = await own.post(OPENROUTER_EXCHANGE_URL, json=body)
        else:
            res = await client.post(OPENROUTER_EXCHANGE_URL, json=body)
    except httpx.HTTPError as exc:
        raise ConnectError("could not reach OpenRouter; check your internet connection") from exc
    key = res.json().get("key") if res.status_code == 200 else None
    if not key:
        raise ConnectError("OpenRouter did not accept the sign-in (codes last 10 minutes); try again")
    return key


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file, keeping its other lines and comments; empty values clear a key."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines += [f"{key}={value}" for key, value in pending.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def env_file() -> Path | None:
    where = os.environ.get("OPENRING_ENV_FILE")  # set by the `openring` command
    return Path(where) if where else None


def apply(updates: dict[str, str]) -> bool:
    """Use the values now and, when there is a settings file, keep them for the next start."""
    for key, value in updates.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    path = env_file()
    if path is None:
        return False
    write_env(path, updates)
    return True
