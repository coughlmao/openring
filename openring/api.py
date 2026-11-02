"""FastAPI app: Vapi-compatible REST API + Twilio voice webhook + media websocket."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from loguru import logger

from . import (
    __version__,
    auth,
    campaigns,
    genesys,
    keys,
    knowledge,
    netguard,
    overrides,
    prices,
    providers,
    recordings,
    routes,
    sip,
    squads,
    stats,
    telnyx,
    twilio,
)
from .config import Settings, get_settings, is_placeholder_key
from .importer import SourceError, import_account
from .providers import ProviderError, resolve_plan
from .server_messages import AssistantRequestError, request_assistant, server_of
from .store import Store, is_phone_number, now_iso
from .tools import ToolError, check_tool
from .twilio import TwilioError, is_valid_signature, say_twiml, stream_twiml

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
# How an outbound call that never reached the assistant ended, from Twilio's status to Vapi's endedReason.
UNREACHED = {
    "busy": "customer-busy",
    "no-answer": "customer-did-not-answer",
    "canceled": "customer-did-not-answer",
    "failed": "twilio-failed-to-connect-call",
    "completed": "twilio-failed-to-connect-call",  # answered, but the media stream never started
}
# Tunnels (ngrok, cloudflared) and reverse proxies connect from 127.0.0.1 too,
# so the peer address alone does not prove the request came from this computer.
PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "cf-connecting-ip")
# Twilio sends its "connected" and "start" messages right after the media websocket opens.
HANDSHAKE_TIMEOUT = 10


def _is_direct_local_request(request: Request) -> bool:
    peer = request.client.host if request.client else ""
    if peer not in LOOPBACK:
        return False
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    hostname = urlsplit("//" + request.headers.get("host", "")).hostname or ""
    return hostname in LOOPBACK


def _e164(number: str) -> str:
    return "+" + "".join(ch for ch in number if ch.isdigit())


class Refused(Exception):
    """The phone provider would not place a call; the call is already recorded as ended."""

    def __init__(self, call: dict[str, Any], detail: str):
        super().__init__(detail)
        self.call = call
        self.detail = detail


# Campaign fields the server keeps up to date; a client's copy of them is ignored.
CAMPAIGN_OWNED = {"id", "orgId", "createdAt", "updatedAt", "status", "endedReason", "endedAt", "error", "calls",
                  "callsCounterScheduled", "callsCounterQueued", "callsCounterInProgress",
                  "callsCounterEndedVoicemail", "callsCounterEnded"}
CAMPAIGN_UNSUPPORTED = ("workflowId", "squadId", "squadOverrides", "predialPlan", "duplicateFromCampaignId")


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = store or Store(settings.db_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        routes.load_speed(store)  # how fast each model answered on earlier calls, for "fastest" routes
        dialer.resume()
        for assistant in store.list("assistants", limit=10_000):  # their local models load in the background
            providers.prepare_local(assistant)
        for squad in store.list("squads", limit=10_000):
            _prepare_squad(squad)
        if store.list("credentials", limit=1):
            await sip_server.start()
        cleaning = asyncio.create_task(clean_recordings_forever())
        yield
        cleaning.cancel()
        await sip_server.stop()
        await dialer.close()

    app = FastAPI(title="OpenRing", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store

    if settings.api_key and is_placeholder_key(settings.api_key):
        logger.error("OPENRING_API_KEY is an example value anyone can guess; it is ignored. Set a long random key.")
        settings.api_key = ""
    if not settings.api_key:
        logger.warning("OPENRING_API_KEY is not set: the REST API only answers requests from this computer.")
    if not settings.validate_twilio_signature:
        logger.warning("VALIDATE_TWILIO_SIGNATURE=false: /twilio/voice accepts requests from anyone. Local tests only.")

    sessions = auth.Sessions(store)
    throttle = auth.Throttle()
    if settings.password and not sessions.password_set:
        if problem := auth.password_problem(settings.password):
            logger.error("OPENRING_PASSWORD ignored: {}", problem)
        else:
            sessions.set_password(settings.password)

    def signed_in(request: Request) -> bool:
        """A dashboard session: the login cookie plus the header only the dashboard sends."""
        if request.headers.get(auth.DASHBOARD_HEADER) != "dashboard":
            return False
        return sessions.valid(request.cookies.get(auth.COOKIE, ""))

    def require_key(request: Request, authorization: str = Header(default="")) -> None:
        if not settings.api_key:
            # No key to check: only this computer may call the API (tunnels and the network may not).
            if _is_direct_local_request(request) or signed_in(request):
                return
            raise HTTPException(
                status_code=401,
                detail="this server has no API key (OPENRING_API_KEY), so its API only answers this computer",
            )
        token = authorization.removeprefix("Bearer ").strip()
        if token and hmac.compare_digest(token.encode(), settings.api_key.encode()):
            return
        if signed_in(request):
            return
        raise HTTPException(status_code=401, detail="invalid or missing API key")

    def require_session(request: Request) -> None:
        if not signed_in(request):
            raise HTTPException(status_code=401, detail="sign in to the dashboard")

    auth_deps = [Depends(require_key)]

    def with_session_cookie(body: dict[str, Any], request: Request) -> JSONResponse:
        response = JSONResponse(body)
        response.set_cookie(
            auth.COOKIE,
            sessions.start(),
            max_age=auth.SESSION_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    def _get_or_404(collection: str, item_id: str) -> dict[str, Any]:
        doc = store.get(collection, item_id)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"{collection[:-1].replace('_', ' ')} not found")
        return doc

    def _plan(assistant: dict[str, Any], **options: Any) -> providers.StackPlan:
        """The assistant's stack, with its model route (if any) filled in."""
        return resolve_plan(routes.expand(assistant, lambda route_id: store.get("routes", route_id)), **options)

    def _call_plan(target: dict[str, Any], extra: dict[str, Any] | None = None, customer: dict[str, Any] | None = None,
                   **options: Any) -> providers.StackPlan:
        """The stack of a call to a squad or an assistant, with everyone a handoff can reach."""
        first, *others = squads.cast(target, store, extra, customer)
        plan = _plan(first, **options)
        plan.team = [_plan(other, **options) for other in others]
        return plan

    def _call_target(call: dict[str, Any]) -> dict[str, Any] | None:
        """Who a call record says should answer: its squad or its assistant."""
        return (call.get("squad") or store.get("squads", call.get("squadId") or "") or call.get("assistant")
                or store.get("assistants", call.get("assistantId") or ""))

    def _prepare_squad(squad: dict[str, Any]) -> None:
        try:
            for member in squads.cast(squad, store):
                providers.prepare_local(member)
        except ProviderError as exc:
            logger.warning("squad {} can't take calls: {}", squad.get("name") or squad.get("id"), exc)

    def _validate_squad(doc: dict[str, Any]) -> None:
        if not isinstance(doc.get("members"), list):
            raise HTTPException(status_code=400, detail="members is required: the assistants of the squad, in a list")
        try:
            squads.check(doc, store)
            for member in squads.cast(doc, store):
                _validate_assistant(member)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _validate_assistant(doc: dict[str, Any], current: dict[str, Any] | None = None) -> None:
        route_id = (doc.get("model") or {}).get("routeId")
        kept = current is not None and route_id == (current.get("model") or {}).get("routeId")
        if route_id and not kept and store.get("routes", str(route_id)) is None:
            raise HTTPException(status_code=400, detail=f"model.routeId: model route {route_id} not found")
        try:
            _plan(doc, check_keys=False)
            model = doc.get("model") or {}
            for tool in model.get("tools") or []:
                check_tool(tool)
        except (ProviderError, ToolError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for tool_id in (doc.get("model") or {}).get("toolIds") or []:
            if store.get("tools", str(tool_id)) is None:
                raise HTTPException(status_code=400, detail=f"model.toolIds: tool {tool_id} not found")
        for tool in (doc.get("model") or {}).get("tools") or []:
            _check_files(tool)
            for destination in (tool.get("destinations") or []) if tool.get("type") == "handoff" else []:
                wanted = destination.get("assistantId")
                if wanted and not destination.get("assistant") and store.get("assistants", str(wanted)) is None:
                    raise HTTPException(status_code=400, detail=f"handoff destination: assistant {wanted} not found")
        base_id = (doc.get("model") or {}).get("knowledgeBaseId")
        if base_id and store.get("knowledge_bases", str(base_id)) is None:
            raise HTTPException(status_code=400, detail=f"model.knowledgeBaseId: knowledge base {base_id} not found")

    def _check_files(tool: dict[str, Any]) -> None:
        if tool.get("type") != "query":
            return
        for base in tool.get("knowledgeBases") or []:
            for file_id in base.get("fileIds") or []:
                if store.get("files", str(file_id)) is None:
                    raise HTTPException(status_code=400, detail=f"knowledgeBases.fileIds: file {file_id} not found")

    def _validate_tool(doc: dict[str, Any]) -> None:
        try:
            check_tool(doc)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _check_files(doc)

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        # Revalidate every time, so an upgraded OpenRing never shows yesterday's dashboard.
        return FileResponse(Path(__file__).parent / "static" / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # ---- Dashboard login ---------------------------------------------------------
    @app.get("/auth/state", include_in_schema=False)
    def auth_state(request: Request) -> dict[str, Any]:
        return {
            "passwordSet": sessions.password_set,
            "signedIn": signed_in(request),
            # Before a password exists, only someone at this computer may choose it: through a
            # tunnel, a stranger with the URL could otherwise take the dashboard first.
            "canSetUp": _is_direct_local_request(request),
            "version": __version__,
        }

    @app.post("/auth/setup", include_in_schema=False)
    def auth_setup(body: dict[str, Any], request: Request) -> JSONResponse:
        if sessions.password_set:
            raise HTTPException(status_code=409, detail="a password is already set; sign in instead")
        if not _is_direct_local_request(request):
            raise HTTPException(
                status_code=403,
                detail="choose the password from the server's own computer (http://localhost), "
                "or set OPENRING_PASSWORD and restart",
            )
        password = str(body.get("password") or "")
        if problem := auth.password_problem(password):
            raise HTTPException(status_code=400, detail=problem)
        sessions.set_password(password)
        return with_session_cookie({"signedIn": True}, request)

    @app.post("/auth/login", include_in_schema=False)
    def auth_login(body: dict[str, Any], request: Request) -> JSONResponse:
        # Wrong passwords slow down the address that sent them (behind a tunnel, uvicorn takes it from
        # X-Forwarded-For). Never this computer: its owner could lock themselves out with typos, and a program
        # running here can read the settings and the database anyway.
        who = None if _is_direct_local_request(request) else (request.client.host if request.client else "?")
        if who is not None and throttle.blocked(who):
            raise HTTPException(status_code=429, detail="too many wrong passwords; wait a few minutes")
        if not sessions.check(str(body.get("password") or "")):
            if who is not None:
                throttle.fail(who)
            raise HTTPException(status_code=401, detail="wrong password")
        if who is not None:
            throttle.clear(who)
        return with_session_cookie({"signedIn": True}, request)

    @app.post("/auth/logout", include_in_schema=False)
    def auth_logout(request: Request) -> JSONResponse:
        sessions.end(request.cookies.get(auth.COOKIE, ""))
        response = JSONResponse({"signedIn": False})
        response.delete_cookie(auth.COOKIE)
        return response

    @app.post("/auth/password", include_in_schema=False, dependencies=[Depends(require_session)])
    def auth_change_password(body: dict[str, Any], request: Request) -> JSONResponse:
        if not sessions.check(str(body.get("current") or "")):
            raise HTTPException(status_code=400, detail="the current password is wrong")
        password = str(body.get("password") or "")
        if problem := auth.password_problem(password):
            raise HTTPException(status_code=400, detail=problem)
        sessions.set_password(password)  # signs out every other browser
        return with_session_cookie({"signedIn": True}, request)

    @app.get("/dashboard/info", include_in_schema=False, dependencies=[Depends(require_session)])
    def dashboard_info(request: Request) -> dict[str, Any]:
        """What the Settings and Providers pages show. Provider keys are reported as set or not, never shown."""
        base = settings.public_url or f"{request.url.scheme}://{request.url.netloc}"
        return {
            "version": __version__,
            "apiKey": settings.api_key,
            "publicUrl": settings.public_url,
            "baseUrl": base,
            "twilioVoiceUrl": base + "/twilio/voice",
            "telnyxVoiceUrl": base + "/telnyx/voice",
            "genesysUrl": re.sub(r"^http", "ws", base) + "/ws/genesys",
            "sipPort": sip_server.port or settings.sip_port,
            "sipRtpPorts": settings.sip_rtp_ports,
            "envFile": os.environ.get("OPENRING_ENV_FILE", ""),
            "providers": [_with_connect(p) for p in providers.provider_status(settings)],
        }

    def _with_connect(row: dict[str, Any]) -> dict[str, Any]:
        """Tell the Providers page what to ask for. Says which fields are set, never their values."""
        spec = keys.CONNECTORS.get(row["id"])
        if spec:
            set_now = keys.current_values(row["id"])
            row["connect"] = {
                "fields": [{"env": env, "label": label, "set": bool(set_now[env])} for env, label in spec["fields"]],
                "keysUrl": spec["keysUrl"],
                "oauth": bool(spec.get("oauth")),
                "generate": bool(spec.get("generate")),
                "balance": row["id"] in keys.BALANCES,
            }
        return row

    def _use(provider: str, values: dict[str, str]) -> dict[str, Any]:
        saved = keys.apply(values)
        if provider == "twilio":  # read once at startup, so refresh the live settings too
            settings.twilio_account_sid = values["TWILIO_ACCOUNT_SID"]
            settings.twilio_auth_token = values["TWILIO_AUTH_TOKEN"]
        if provider == "telnyx":
            settings.telnyx_api_key = values["TELNYX_API_KEY"]
            settings.telnyx_public_key = values["TELNYX_PUBLIC_KEY"]
        if provider == "genesys":
            settings.genesys_api_key = values["GENESYS_API_KEY"]
            settings.genesys_client_secret = values["GENESYS_CLIENT_SECRET"]
        where = str(keys.env_file()) if saved else None
        return {"connected": bool(all(values.values())), "saved": saved, "envFile": where}

    @app.post("/providers/{provider}/connect", include_in_schema=False, dependencies=[Depends(require_session)])
    async def connect_provider(provider: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            values = keys.clean_values(provider, body.get("values") or {})
            checked = await keys.check(provider, values)
        except keys.ConnectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {**_use(provider, values), "checked": checked}

    @app.post("/providers/{provider}/test", include_in_schema=False, dependencies=[Depends(require_session)])
    async def test_provider(provider: str) -> dict[str, Any]:
        if provider not in keys.CONNECTORS:
            raise HTTPException(status_code=404, detail="unknown provider")
        values = keys.current_values(provider)
        if not all(values.values()):
            raise HTTPException(status_code=400, detail=f"{keys.NAMES[provider]} is not connected")
        try:
            checked = await keys.check(provider, values)
        except keys.ConnectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "checked": checked}

    @app.get("/providers/{provider}/balance", include_in_schema=False, dependencies=[Depends(require_session)])
    async def provider_balance(provider: str) -> dict[str, Any]:
        """What's left on the provider account (money or characters); null when the provider won't say."""
        if provider not in keys.BALANCES:
            raise HTTPException(status_code=404, detail="this provider doesn't report a balance")
        values = keys.current_values(provider)
        if not all(values.values()):
            raise HTTPException(status_code=400, detail=f"{keys.NAMES[provider]} is not connected")
        return {"balance": await keys.balance(provider, values)}

    @app.get("/providers/ollama/models", include_in_schema=False, dependencies=[Depends(require_session)])
    async def ollama_models() -> dict[str, Any]:
        """The models pulled into the local Ollama, or `running: false` when it isn't up."""
        base = (providers.LLM_PROVIDERS["ollama"][0] or "").rstrip("/").removesuffix("/v1")
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                res = await client.get(base + "/api/tags")
                res.raise_for_status()
                found = res.json().get("models") or []
        except (httpx.HTTPError, ValueError, AttributeError):
            return {"running": False, "models": [], "url": base}
        return {"running": True, "models": [m["name"] for m in found if isinstance(m, dict) and m.get("name")],
                "url": base}

    @app.delete("/providers/{provider}/connect", include_in_schema=False, dependencies=[Depends(require_session)])
    def disconnect_provider(provider: str) -> dict[str, Any]:
        if provider not in keys.CONNECTORS:
            raise HTTPException(status_code=404, detail="unknown provider")
        return _use(provider, {env: "" for env, _ in keys.CONNECTORS[provider]["fields"]})

    @app.post("/providers/openrouter/oauth", include_in_schema=False, dependencies=[Depends(require_session)])
    async def openrouter_oauth(body: dict[str, Any]) -> dict[str, Any]:
        code, verifier = str(body.get("code") or ""), str(body.get("codeVerifier") or "")
        if not code or not verifier:
            raise HTTPException(status_code=400, detail="the OpenRouter sign-in did not finish; try again")
        try:
            key = await keys.exchange_openrouter_code(code, verifier)
            values = keys.clean_values("openrouter", {"OPENROUTER_API_KEY": key})
        except keys.ConnectError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _use("openrouter", values)

    @app.get("/stats", dependencies=auth_deps)
    def call_stats(days: int | None = 30) -> dict[str, Any]:
        """Totals for the dashboard's overview; `days=0` covers every call."""
        return stats.summarize(store.list("calls", limit=100_000), days or None)

    # ---- Assistants (same JSON shape as Vapi's /assistant) -------------------
    @app.post("/assistant", status_code=201, dependencies=auth_deps)
    def create_assistant(body: dict[str, Any]) -> dict[str, Any]:
        _validate_assistant(body)
        providers.prepare_local(body)
        return store.create("assistants", body)

    @app.get("/assistant", dependencies=auth_deps)
    def list_assistants(
        limit: int = 100,
        createdAtGt: str | None = None,
        createdAtLt: str | None = None,
        createdAtGe: str | None = None,
        createdAtLe: str | None = None,
        ) -> list[dict[str, Any]]:
        return store.list(
            "assistants",
            limit,
            created_at_gt=createdAtGt,
            created_at_lt=createdAtLt,
            created_at_ge=createdAtGe,
            created_at_le=createdAtLe,
            )

    @app.get("/assistant/{assistant_id}", dependencies=auth_deps)
    def get_assistant(assistant_id: str) -> dict[str, Any]:
        return _get_or_404("assistants", assistant_id)

    @app.patch("/assistant/{assistant_id}", dependencies=auth_deps)
    def update_assistant(assistant_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = _get_or_404("assistants", assistant_id)
        _validate_assistant({**current, **body}, current)  # an older server may have left a deleted route's id
        providers.prepare_local({**current, **body})
        return store.update("assistants", assistant_id, body)

    @app.delete("/assistant/{assistant_id}", dependencies=auth_deps)
    def delete_assistant(assistant_id: str) -> dict[str, Any]:
        _get_or_404("assistants", assistant_id)
        for squad in store.list("squads", limit=100_000):
            if any(isinstance(m, dict) and m.get("assistantId") == assistant_id for m in squad.get("members") or []):
                raise HTTPException(status_code=409, detail=f"squad '{squad.get('name') or squad['id']}' uses this "
                                                            "assistant: take it out of the squad first")
        return store.delete("assistants", assistant_id)

    # ---- Squads (Vapi's /squad): several assistants on one call ----------------------
    @app.post("/squad", status_code=201, dependencies=auth_deps)
    def create_squad(body: dict[str, Any]) -> dict[str, Any]:
        _validate_squad(body)
        _prepare_squad(body)
        return store.create("squads", body)

    @app.get("/squad", dependencies=auth_deps)
    def list_squads(limit: int = 100) -> list[dict[str, Any]]:
        return store.list("squads", limit)

    @app.get("/squad/{squad_id}", dependencies=auth_deps)
    def get_squad(squad_id: str) -> dict[str, Any]:
        return _get_or_404("squads", squad_id)

    @app.patch("/squad/{squad_id}", dependencies=auth_deps)
    def update_squad(squad_id: str, body: dict[str, Any]) -> dict[str, Any]:
        _validate_squad({**_get_or_404("squads", squad_id), **body})
        _prepare_squad({**store.get("squads", squad_id), **body})
        return store.update("squads", squad_id, body)

    @app.delete("/squad/{squad_id}", dependencies=auth_deps)
    def delete_squad(squad_id: str) -> dict[str, Any]:
        """Numbers it answered go back to having nobody attached."""
        _get_or_404("squads", squad_id)
        for number in store.list("phone_numbers", limit=100_000):
            if number.get("squadId") == squad_id:
                store.update("phone_numbers", number["id"], {"squadId": None})
        return store.delete("squads", squad_id)

    # ---- Model routes: models in order, shared by assistants (an OpenRing addition, like OmniRoute's combos)
    def _route_doc(body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            doc = routes.clean({**(current or {}), **body})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for i, model in enumerate(doc["models"]):
            try:
                resolve_plan({"model": model}, check_keys=False, with_fallbacks=False)
            except ProviderError as exc:
                raise HTTPException(status_code=400, detail=f"models[{i}]: {exc}") from exc
        return doc

    @app.post("/model-route", status_code=201, dependencies=auth_deps)
    def create_route(body: dict[str, Any]) -> dict[str, Any]:
        return routes.with_status(store.create("routes", _route_doc(body)))

    @app.get("/model-route", dependencies=auth_deps)
    def list_routes() -> list[dict[str, Any]]:
        return [routes.with_status(r) for r in store.list("routes", limit=1000)]

    @app.get("/model-route/{route_id}", dependencies=auth_deps)
    def get_route(route_id: str) -> dict[str, Any]:
        return routes.with_status(_get_or_404("routes", route_id))

    @app.patch("/model-route/{route_id}", dependencies=auth_deps)
    def update_route(route_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = _get_or_404("routes", route_id)
        if "preset" not in body and any(k in body for k in routes.SETTINGS):
            body = {**body, "preset": "custom"}  # changing what a preset decides makes the route custom
        return routes.with_status(store.update("routes", route_id, _route_doc(body, current)))

    @app.post("/model-route/{route_id}/measure", dependencies=auth_deps)
    async def measure_route(route_id: str) -> dict[str, Any]:
        """Ask each model of the route for one short answer and time its first word, as a call would (a few
        tokens each). "fastest" routes order by these times and by the ones calls measure."""
        route = _get_or_404("routes", route_id)

        async def measure(model: dict[str, Any]) -> dict[str, Any]:
            try:  # a model saved before a rule it breaks now (a refused model.url) gets its reason, not a 500
                plan = resolve_plan({"model": model}, check_keys=False, with_fallbacks=False)
            except ProviderError as exc:
                return {"error": str(exc)}
            return await routes.probe(model, plan)

        results = await asyncio.gather(*(measure(m) for m in route.get("models") or []))
        routes.save_speed(store)
        return {**routes.with_status(route), "measured": list(results)}

    @app.get("/prices", dependencies=auth_deps)
    def price_list() -> dict[str, Any]:
        """The list prices calls are priced at: models per 1M tokens, speech-to-text per minute, voices per 1M
        characters (with ~/.openring/prices.json applied)."""
        return prices.table()

    @app.delete("/model-route/{route_id}", dependencies=auth_deps)
    def delete_route(route_id: str) -> dict[str, Any]:
        """Assistants on a deleted route go back to the model saved in their own `model`."""
        _get_or_404("routes", route_id)
        for assistant in store.list("assistants", limit=100_000):
            model = assistant.get("model") or {}
            if model.get("routeId") == route_id:
                own = {k: v for k, v in model.items() if k != "routeId"}
                store.update("assistants", assistant["id"], {"model": own})
        return store.delete("routes", route_id)

    # ---- Tools (Vapi's /tool: saved once, used by assistants through model.toolIds) ----
    @app.post("/tool", status_code=201, dependencies=auth_deps)
    def create_tool(body: dict[str, Any]) -> dict[str, Any]:
        _validate_tool(body)
        return store.create("tools", body)

    @app.get("/tool", dependencies=auth_deps)
    def list_tools(
        limit: int = 100,
        createdAtGt:str | None = None,
        createdAtLt:str | None = None,
        createdAtGe:str | None = None,
        createdAtLe:str | None = None,
) -> list[dict[str, Any]]:
        return store.list(
            "tools",
            limit,
            created_at_gt=createdAtGt,
            created_at_lt=createdAtLt,
            created_at_ge=createdAtGe,
            created_at_le=createdAtLe,
            )

    @app.get("/tool/{tool_id}", dependencies=auth_deps)
    def get_tool(tool_id: str) -> dict[str, Any]:
        return _get_or_404("tools", tool_id)

    @app.patch("/tool/{tool_id}", dependencies=auth_deps)
    def update_tool(tool_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = _get_or_404("tools", tool_id)
        _validate_tool({**current, **body})
        return store.update("tools", tool_id, body)

    @app.delete("/tool/{tool_id}", dependencies=auth_deps)
    def delete_tool(tool_id: str) -> dict[str, Any]:
        _get_or_404("tools", tool_id)
        return store.delete("tools", tool_id)

    # ---- Files and knowledge bases (Vapi's /file and /knowledge-base) --------------------
    @app.post("/file", status_code=201, dependencies=auth_deps)
    async def upload_file(file: UploadFile) -> dict[str, Any]:
        """Keep a file's text, searchable by `query` tools. The file itself isn't kept."""
        if not store.can_search:
            raise HTTPException(status_code=400, detail="this Python's SQLite has no full-text search (FTS5)")
        data = await file.read(knowledge.MAX_FILE_BYTES + 1)
        if len(data) > knowledge.MAX_FILE_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"a file has at most {knowledge.MAX_FILE_BYTES // 1_000_000} MB")
        name = (file.filename or "file").replace("\\", "/").rsplit("/", 1)[-1][:200]
        try:
            text = await asyncio.to_thread(knowledge.extract_text, name, data)
        except knowledge.KnowledgeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        passages = await asyncio.to_thread(knowledge.chunks, text)
        doc = store.create("files", {"name": name, "originalName": name, "bytes": len(data), "status": "done",
                                     "mimetype": file.content_type, "parsedTextBytes": len(text.encode()),
                                     "passages": len(passages)})
        await asyncio.to_thread(store.index_file, doc["id"], passages)
        return doc

    @app.get("/file", dependencies=auth_deps)
    def list_files(limit: int = 100) -> list[dict[str, Any]]:
        return store.list("files", limit)

    @app.get("/file/{file_id}", dependencies=auth_deps)
    def get_file(file_id: str) -> dict[str, Any]:
        return _get_or_404("files", file_id)

    @app.patch("/file/{file_id}", dependencies=auth_deps)
    def update_file(file_id: str, body: dict[str, Any]) -> dict[str, Any]:
        _get_or_404("files", file_id)
        return store.update("files", file_id, {k: v for k, v in body.items() if k == "name"})

    @app.delete("/file/{file_id}", dependencies=auth_deps)
    def delete_file(file_id: str) -> dict[str, Any]:
        """Its text goes for good; tools listing it go on with their other files."""
        _get_or_404("files", file_id)
        store.forget_file(file_id)
        return store.delete("files", file_id)

    def _validate_base(doc: dict[str, Any]) -> None:
        try:
            knowledge.check(doc, "a knowledge base")
        except knowledge.KnowledgeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _check_files({"type": "query", "knowledgeBases": [doc]})

    @app.post("/knowledge-base", status_code=201, dependencies=auth_deps)
    def create_knowledge_base(body: dict[str, Any]) -> dict[str, Any]:
        _validate_base(body)
        return store.create("knowledge_bases", body)

    @app.get("/knowledge-base", dependencies=auth_deps)
    def list_knowledge_bases(limit: int = 100) -> list[dict[str, Any]]:
        return store.list("knowledge_bases", limit)

    @app.get("/knowledge-base/{base_id}", dependencies=auth_deps)
    def get_knowledge_base(base_id: str) -> dict[str, Any]:
        return _get_or_404("knowledge_bases", base_id)

    @app.patch("/knowledge-base/{base_id}", dependencies=auth_deps)
    def update_knowledge_base(base_id: str, body: dict[str, Any]) -> dict[str, Any]:
        _validate_base({**_get_or_404("knowledge_bases", base_id), **body})
        return store.update("knowledge_bases", base_id, body)

    @app.delete("/knowledge-base/{base_id}", dependencies=auth_deps)
    def delete_knowledge_base(base_id: str) -> dict[str, Any]:
        _get_or_404("knowledge_bases", base_id)
        return store.delete("knowledge_bases", base_id)

    # ---- SIP trunks (Vapi's /credential with provider byo-sip-trunk) ----------------------
    def _trunk_view(doc: dict[str, Any]) -> dict[str, Any]:
        view = {**sip.without_password(doc), "sipListening": sip_server.listening}
        if sip_server.error:
            view["sipError"] = sip_server.error
        if doc["id"] in sip_server.registered:
            view["sipRegistered"] = sip_server.registered[doc["id"]]
        return view

    def _check_trunk(doc: dict[str, Any]) -> None:
        try:
            sip.check_trunk(doc)
        except sip.SipError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/credential", status_code=201, dependencies=auth_deps)
    async def create_credential(body: dict[str, Any]) -> dict[str, Any]:
        _check_trunk(body)
        doc = store.create("credentials", body)
        await sip_server.start()
        return _trunk_view(doc)

    @app.get("/credential", dependencies=auth_deps)
    def list_credentials(limit: int = 100) -> list[dict[str, Any]]:
        return [_trunk_view(doc) for doc in store.list("credentials", limit)]

    @app.get("/credential/{credential_id}", dependencies=auth_deps)
    def get_credential(credential_id: str) -> dict[str, Any]:
        return _trunk_view(_get_or_404("credentials", credential_id))

    @app.patch("/credential/{credential_id}", dependencies=auth_deps)
    async def update_credential(credential_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = _get_or_404("credentials", credential_id)
        plan = body.get("outboundAuthenticationPlan")
        if isinstance(plan, dict) and "authPassword" not in plan:  # the API never shows it, so it stays unless replaced
            old = (current.get("outboundAuthenticationPlan") or {}).get("authPassword")
            body = {**body, "outboundAuthenticationPlan": {**plan, **({"authPassword": old} if old else {})}}
        _check_trunk({**current, **body})
        doc = store.update("credentials", credential_id, body)
        await sip_server.start()
        return _trunk_view(doc)

    @app.delete("/credential/{credential_id}", dependencies=auth_deps)
    async def delete_credential(credential_id: str) -> dict[str, Any]:
        _get_or_404("credentials", credential_id)
        for number in store.list("phone_numbers", limit=100_000):
            if number.get("credentialId") == credential_id:
                raise HTTPException(status_code=409, detail=f"{number.get('number')} uses this trunk: delete it first")
        doc = store.delete("credentials", credential_id)
        await sip_server.refresh()
        return sip.without_password(doc)

    # ---- Import from another Vapi-compatible API ------------------------------------
    @app.post("/import", dependencies=auth_deps)
    async def import_from_api(body: dict[str, Any]) -> dict[str, Any]:
        """Copy an account's tools, assistants and Twilio numbers. The key is used once, never stored."""
        try:
            return await import_account(str(body.get("baseUrl") or "https://api.vapi.ai"),
                                        str(body.get("apiKey") or ""), store)
        except SourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- Phone numbers --------------------------------------------------------
    def _check_number(doc: dict[str, Any]) -> None:
        trunk = doc.get("provider") == "byo-phone-number"
        if trunk:
            if store.get("credentials", str(doc.get("credentialId") or "")) is None:
                raise HTTPException(status_code=400, detail="credentialId: a byo-phone-number needs a saved SIP trunk")
            # An extension or a carrier's own format, as on Vapi, when the number says so.
            if doc.get("numberE164CheckEnabled") is False and sip.NUMBER.fullmatch(str(doc.get("number") or "")):
                return
        if not is_phone_number(doc.get("number")):
            raise HTTPException(status_code=400, detail="number must be a phone number with its country code "
                                                        "(E.164, e.g. +15551234567)")

    @app.post("/phone-number", status_code=201, dependencies=auth_deps)
    def create_phone_number(body: dict[str, Any]) -> dict[str, Any]:
        _check_number(body)
        if body.get("assistantId"):
            _get_or_404("assistants", body["assistantId"])
        if body.get("squadId"):
            _get_or_404("squads", body["squadId"])
        body.setdefault("provider", "twilio")
        return store.create("phone_numbers", body)

    @app.get("/phone-number", dependencies=auth_deps)
    def list_phone_numbers(
        limit: int = 100,
        createdAtGt: str | None = None,
        createdAtLt: str | None = None,
        createdAtGe: str | None = None,
        createdAtLe: str | None = None,
    ) -> list[dict[str, Any]]:
        return store.list(
        "phone_numbers",
        limit,
        created_at_gt=createdAtGt,
        created_at_lt=createdAtLt,
        created_at_ge=createdAtGe,
        created_at_le=createdAtLe,
    )

    @app.get("/phone-number/{number_id}", dependencies=auth_deps)
    def get_phone_number(number_id: str) -> dict[str, Any]:
        return _get_or_404("phone_numbers", number_id)

    @app.patch("/phone-number/{number_id}", dependencies=auth_deps)
    def update_phone_number(number_id: str, body: dict[str, Any]) -> dict[str, Any]:
        current = _get_or_404("phone_numbers", number_id)
        if "number" in body or "credentialId" in body or "provider" in body:
            _check_number({**current, **body})
        if body.get("assistantId"):
            _get_or_404("assistants", body["assistantId"])
        if body.get("squadId"):
            _get_or_404("squads", body["squadId"])
        return store.update("phone_numbers", number_id, body)

    @app.delete("/phone-number/{number_id}", dependencies=auth_deps)
    def delete_phone_number(number_id: str) -> dict[str, Any]:
        _get_or_404("phone_numbers", number_id)
        return store.delete("phone_numbers", number_id)

    # ---- Calls ------------------------------------------------------------------
    @app.get("/call", dependencies=auth_deps)
    def list_calls(
        limit: int = 100,
        createdAtGt:str | None = None,
        createdAtLt:str | None = None,
        createdAtGe:str | None = None,
        createdAtLe:str | None = None,
        ) -> list[dict[str, Any]]:
        return store.list(
            "calls",
            limit,
            created_at_gt=createdAtGt,
            created_at_lt=createdAtLt,
            created_at_ge=createdAtGe,
            created_at_le=createdAtLe,
            )

    def _finished_recording(call_id: str) -> tuple[dict[str, Any], Path]:
        call = _get_or_404("calls", call_id)
        if recordings.is_being_written(call["id"]):
            raise HTTPException(status_code=409, detail="the call is still going on, so its recording isn't finished")
        path = recordings.path_for(call["id"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="this call has no recording")
        return call, path

    @app.get("/call/{call_id}/recording", dependencies=auth_deps)
    def call_recording(call_id: str) -> Response:
        _, path = _finished_recording(call_id)
        # A conversation: no copies in caches along the way, and browsers play it as audio, nothing else.
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        return FileResponse(path, media_type="audio/wav", headers=headers)

    @app.delete("/call/{call_id}/recording", dependencies=auth_deps)
    def delete_call_recording(call_id: str) -> dict[str, Any]:
        """Delete the audio for good (someone asked, or the disk is filling up). The transcript stays."""
        call, path = _finished_recording(call_id)
        try:
            return _forget_recording(call, path)
        except OSError as exc:  # Windows refuses while someone is downloading it
            raise HTTPException(status_code=409, detail="the recording is in use; try again in a moment") from exc

    def _forget_recording(call: dict[str, Any], path: Path) -> dict[str, Any]:
        path.unlink()
        # A store update replaces the field: the transcript and the latency stay in the artifact.
        artifact = {**(call.get("artifact") or {}), "recordingUrl": None, "stereoRecordingUrl": None}
        return store.update("calls", call["id"], {"artifact": artifact})

    def clean_recordings() -> int:
        """Delete the recordings past the retention period. Returns how many went."""
        days = recordings.retention_days(store)
        deleted = 0
        for call_id in recordings.old_recordings(days, lambda c: store.get("calls", c) is not None):
            try:
                _forget_recording(store.get("calls", call_id), recordings.path_for(call_id))
                deleted += 1
            except OSError as exc:  # in use right now: the next round gets it
                logger.warning("recording of call {} not deleted yet: {}", call_id, exc)
        if deleted:
            logger.info("deleted {} recordings older than {} days", deleted, days)
        return deleted

    async def clean_recordings_forever() -> None:
        while True:
            try:
                await asyncio.to_thread(clean_recordings)
            except Exception:
                logger.exception("cleaning old recordings failed; trying again later")
            await asyncio.sleep(recordings.CLEANUP_SECONDS)

    def _recordings_settings() -> dict[str, Any]:
        return {"retentionDays": recordings.retention_days(store), **recordings.usage()}

    @app.get("/settings/recordings", dependencies=auth_deps)
    def recordings_settings() -> dict[str, Any]:
        return _recordings_settings()

    @app.patch("/settings/recordings", dependencies=auth_deps)
    def update_recordings_settings(body: dict[str, Any]) -> dict[str, Any]:
        """How many days recordings are kept; 0 keeps them for good. Old ones go in the next cleaning, within hours."""
        days = body.get("retentionDays")
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= recordings.MAX_RETENTION_DAYS:
            raise HTTPException(status_code=400, detail=f"retentionDays is a whole number from 0 (keep them for good) "
                                                        f"to {recordings.MAX_RETENTION_DAYS}")
        store.set_setting(recordings.RETENTION_KEY, str(days))
        return _recordings_settings()

    @app.get("/call/{call_id}", dependencies=auth_deps)
    def get_call(call_id: str) -> dict[str, Any]:
        return _get_or_404("calls", call_id)

    def _outbound_number(number_id: str) -> dict[str, Any]:
        number = _get_or_404("phone_numbers", number_id)
        if (number.get("provider") or "twilio") not in ("twilio", "byo-phone-number"):
            raise HTTPException(status_code=400, detail="outbound calls go through Twilio numbers and SIP trunks")
        return number

    def _customer(value: Any, where: str, *, sip_address: bool = False) -> dict[str, Any]:
        """A Vapi customer with its number in E.164. Its `assistantOverrides` go to the call, not the record."""
        customer = dict(value) if isinstance(value, dict) else {}
        if customer.get("sipUri") and not customer.get("number"):
            if not sip_address:
                raise HTTPException(status_code=400, detail=f"{where}.sipUri: a campaign calls numbers; use number")
            if not sip.is_address(str(customer["sipUri"])):
                raise HTTPException(status_code=400, detail=f"{where}.sipUri must be like sip:name@host")
            return customer
        if not is_phone_number(customer.get("number")):
            raise HTTPException(status_code=400, detail=f"{where}.number must be a phone number with its country code "
                                                        "(E.164, e.g. +5511999999999)")
        customer["number"] = _e164(str(customer["number"]))
        return customer

    def _check_outbound(assistant: dict[str, Any], number: dict[str, Any]) -> None:
        """Everything an outbound call needs before it's dialed: a working assistant, and Twilio with a public URL or
        a SIP trunk."""
        _validate_assistant(assistant)
        try:
            _plan(assistant)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if number.get("provider") == "byo-phone-number":
            if store.get("credentials", str(number.get("credentialId") or "")) is None:
                raise HTTPException(status_code=400, detail="the number's SIP trunk was deleted")
            return
        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            raise HTTPException(status_code=400, detail="outbound calls need TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN")
        if not settings.public_url:
            raise HTTPException(status_code=400, detail="outbound calls need PUBLIC_URL (where Twilio reaches you)")

    async def dial(
        number: dict[str, Any], customer: dict[str, Any], record: dict[str, Any], assistant: dict[str, Any]
    ) -> dict[str, Any]:
        """Has Twilio or the SIP trunk call `customer` from `number`. `record` names the assistant (and overrides) for
        the call; `assistant` is it with those applied, for what Twilio does before anyone talks to it."""
        person = {k: v for k, v in customer.items() if k != "assistantOverrides"}
        call = store.create("calls", {"type": "outboundPhoneCall", "status": "queued", **record,
                                      "phoneNumberId": number["id"], "customer": person})
        to = person.get("number") or person["sipUri"]
        if number.get("provider") == "byo-phone-number":
            trunk = store.get("credentials", str(number.get("credentialId") or "")) or {}
            try:
                if not sip_server.listening:
                    await sip_server.start()
                await sip_server.place_call(call["id"], to, str(number["number"]), trunk)
            except (sip.SipError, OSError) as exc:
                patch = {"status": "ended", "endedReason": "sip-failed-to-connect-call", "endedAt": now_iso()}
                raise Refused(store.update("calls", call["id"], patch), str(exc)) from exc
            return store.update("calls", call["id"], {"phoneCallProvider": "sip"})
        try:
            sid = await twilio.create_call(
                settings.twilio_account_sid,
                settings.twilio_auth_token,
                to=to,
                from_=_e164(number["number"]),
                url=f"{settings.public_url}/twilio/voice?callId={call['id']}",
                status_callback=f"{settings.public_url}/twilio/status?callId={call['id']}",
                machine_detection=twilio.machine_detection(assistant),
            )
        except (TwilioError, httpx.HTTPError) as exc:
            patch = {"status": "ended", "endedReason": "twilio-failed-to-connect-call", "endedAt": now_iso()}
            raise Refused(store.update("calls", call["id"], patch), str(exc)) from exc
        return store.update("calls", call["id"], {"phoneCallProviderId": sid})

    @app.post("/call", status_code=201, dependencies=auth_deps)
    async def create_call(body: dict[str, Any]) -> dict[str, Any]:
        """Outbound phone call, Vapi-style: the assistant calls `customer.number` from one of your numbers."""
        number = _outbound_number(str(body.get("phoneNumberId") or ""))
        customer = _customer(body.get("customer"), "customer", sip_address=True)
        extra = overrides.combine(customer.get("assistantOverrides"), body.get("assistantOverrides"))
        transient = body.get("assistant") if isinstance(body.get("assistant"), dict) else None
        squad = body.get("squad") if isinstance(body.get("squad"), dict) else None
        squad_id = body.get("squadId") or (None if transient or body.get("assistantId") or number.get("assistantId")
                                           else number.get("squadId"))
        # A transient assistant or squad lives in the call record, as on Vapi.
        record: dict[str, Any]
        if squad is not None:
            _validate_squad(squad)
            target, record = squad, {"squad": squad}
        elif squad_id:
            target = _get_or_404("squads", str(squad_id))
            record = {"squadId": target["id"]}
        elif transient is not None:
            _validate_assistant(transient)
            target, record = transient, {"assistant": transient}
        else:
            target = _get_or_404("assistants", str(body.get("assistantId") or number.get("assistantId") or ""))
            record = {"assistantId": target["id"]}
        try:
            team = squads.cast(target, store, extra, customer)
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        applied = team[0]
        for member in team:
            _check_outbound(member, number)
        if extra:
            record["assistantOverrides"] = extra
        try:
            return await dial(number, customer, record, applied)
        except Refused as exc:
            carrier = "The SIP trunk" if number.get("provider") == "byo-phone-number" else "Twilio"
            raise HTTPException(status_code=400, detail=f"{carrier} did not place the call: {exc.detail}") from exc

    # ---- Campaigns (Vapi's /campaign): one assistant calls a list of people --------------
    def _campaign_doc(body: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        """The campaign as stored: checked the way a run would trip on it, numbers in E.164."""
        doc = {k: v for k, v in {**(current or {}), **body}.items() if k not in CAMPAIGN_OWNED}
        if not str(doc.get("name") or "").strip():
            raise HTTPException(status_code=400, detail="name is required")
        for key in CAMPAIGN_UNSUPPORTED:
            if doc.get(key):
                raise HTTPException(status_code=400, detail=f"{key} is not supported yet")
        if doc.get("dialPlan"):
            if doc.get("phoneNumberId") or doc.get("customers"):
                raise HTTPException(status_code=400, detail="use phoneNumberId with customers, or dialPlan, not both")
            if not isinstance(doc["dialPlan"], list):
                raise HTTPException(status_code=400, detail="dialPlan must be a list of {phoneNumberId, customers}")
            entries = [e if isinstance(e, dict) else {} for e in doc["dialPlan"]]
            doc["dialPlan"] = [
                {**entry, "phoneNumberId": str(entry.get("phoneNumberId") or ""),
                 "customers": [_customer(c, f"dialPlan[{i}].customers[{j}]")
                               for j, c in enumerate(entry.get("customers") or [])]}
                for i, entry in enumerate(entries)
            ]
        else:
            if not doc.get("phoneNumberId"):
                raise HTTPException(status_code=400, detail="phoneNumberId is required: the number that calls")
            if not isinstance(doc.get("customers"), list) or not doc["customers"]:
                raise HTTPException(status_code=400,
                                    detail='customers is required: who to call, e.g. [{"number": "+5511912345678"}]')
            doc["customers"] = [_customer(c, f"customers[{i}]") for i, c in enumerate(doc["customers"])]
        # The same person twice in one campaign would get two automated calls: keep the first.
        seen: set[str] = set()

        def first_time(customer: dict[str, Any]) -> bool:
            fresh = customer["number"] not in seen
            seen.add(customer["number"])
            return fresh

        if doc.get("dialPlan"):
            doc["dialPlan"] = [{**entry, "customers": [c for c in entry["customers"] if first_time(c)]}
                               for entry in doc["dialPlan"]]
        else:
            doc["customers"] = [c for c in doc["customers"] if first_time(c)]
        planned = campaigns.dials(doc)
        if not planned:
            raise HTTPException(status_code=400, detail="the campaign has nobody to call")
        if len(planned) > campaigns.MAX_CUSTOMERS:
            raise HTTPException(status_code=400, detail=f"a campaign calls at most {campaigns.MAX_CUSTOMERS:,} people")
        try:
            concurrency = int(doc.get("maxConcurrency") or campaigns.DEFAULT_CONCURRENCY)
        except (TypeError, ValueError):
            concurrency = 0
        if not 1 <= concurrency <= campaigns.MAX_CONCURRENCY:
            raise HTTPException(status_code=400, detail=f"maxConcurrency goes from 1 to {campaigns.MAX_CONCURRENCY}")
        doc["maxConcurrency"] = concurrency
        if (schedule := doc.get("schedulePlan")) is not None:
            if not isinstance(schedule, dict) or not schedule.get("earliestAt"):
                raise HTTPException(status_code=400, detail="schedulePlan.earliestAt is required")
            try:
                earliest = campaigns.parse_time(schedule["earliestAt"])
                latest = campaigns.parse_time(schedule.get("latestAt"))
            except ValueError as exc:
                raise HTTPException(status_code=400,
                                    detail="schedulePlan times are ISO 8601, e.g. 2026-11-03T13:00:00Z") from exc
            if latest and earliest and latest <= earliest:
                raise HTTPException(status_code=400, detail="schedulePlan.latestAt must come after earliestAt")
        base = doc.get("assistantOverrides")
        checked: set[str] = set()
        for number_id, customer in planned:
            number = _outbound_number(number_id)
            assistant_id = doc.get("assistantId") or number.get("assistantId")
            if not assistant_id:
                raise HTTPException(status_code=400,
                                    detail=f"assistantId is required: {number['number']} has no assistant attached")
            assistant = _get_or_404("assistants", str(assistant_id))
            if number_id not in checked:
                _check_outbound(overrides.apply(assistant, base), number)
                checked.add(number_id)
            if overrides.changes_assistant(customer.get("assistantOverrides")):
                _check_outbound(overrides.apply(assistant, overrides.combine(base, customer["assistantOverrides"]),
                                                customer), number)
        return doc

    async def dial_for_campaign(campaign: dict[str, Any], number_id: str, customer: dict[str, Any]) -> dict[str, Any]:
        try:
            number = _outbound_number(number_id)
            assistant = _get_or_404("assistants", str(campaign.get("assistantId") or number.get("assistantId") or ""))
            extra = overrides.combine(campaign.get("assistantOverrides"), customer.get("assistantOverrides"))
            applied = overrides.apply(assistant, extra, customer)
            _check_outbound(applied, number)
        except HTTPException as exc:
            raise campaigns.Stop(str(exc.detail)) from exc
        record = {"assistantId": assistant["id"], "campaignId": campaign["id"],
                  **({"assistantOverrides": extra} if extra else {})}
        try:
            return await dial(number, customer, record, applied)
        except Refused as exc:
            logger.warning("campaign {}: {} was not called: {}", campaign["id"], customer["number"], exc.detail)
            return exc.call

    dialer = campaigns.Dialer(store, dial_for_campaign)
    app.state.dialer = dialer

    @app.post("/campaign", status_code=201, dependencies=auth_deps)
    async def create_campaign(body: dict[str, Any]) -> dict[str, Any]:
        doc = _campaign_doc(body)
        campaign = store.create("campaigns", {**doc, "status": "scheduled", "calls": {},
                                              **campaigns.counters({**doc, "calls": {}})})
        dialer.start(campaign["id"])
        return campaign

    @app.get("/campaign", dependencies=auth_deps)
    def list_campaigns(limit: int = 100, status: str | None = None) -> dict[str, Any]:
        """Paginated like Vapi's: `{results, metadata}`."""
        found = [c for c in store.list("campaigns", limit=campaigns.MAX_CUSTOMERS)
                 if not status or c.get("status") == status]
        results = found[:max(limit, 0)]
        meta = {"itemsPerPage": len(results), "totalItems": len(found), "currentPage": 1}
        return {"results": results, "metadata": meta}

    @app.get("/campaign/{campaign_id}", dependencies=auth_deps)
    def get_campaign(campaign_id: str) -> dict[str, Any]:
        return _get_or_404("campaigns", campaign_id)

    @app.patch("/campaign/{campaign_id}", dependencies=auth_deps)
    async def update_campaign(campaign_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Rename it, set `status` to "cancelled" (or "ended", Vapi's older word) to stop it, or change it before
        it starts."""
        current = _get_or_404("campaigns", campaign_id)
        changes = {k: v for k, v in body.items() if k not in CAMPAIGN_OWNED}
        if set(changes) - {"name"}:
            if current.get("status") != "scheduled":
                raise HTTPException(status_code=400, detail="a campaign that started can only be renamed or cancelled")
            changes = _campaign_doc(changes, current)
        elif "name" in changes and not str(changes["name"] or "").strip():
            raise HTTPException(status_code=400, detail="name is required")
        if "status" not in body:
            return store.update("campaigns", campaign_id, changes)
        if body["status"] not in ("cancelled", "ended"):
            raise HTTPException(status_code=400, detail='status: set "cancelled" to stop the campaign')
        # The dialer may start the campaign meanwhile, so the stop is claimed from the status it has now.
        for _ in range(2):
            status = (store.get("campaigns", campaign_id) or {}).get("status")
            if status not in campaigns.LIVE:
                raise HTTPException(status_code=400, detail=f"the campaign is already {status}")
            reason = f"campaign.{status}.ended-by-user"
            stop = {**changes, "status": "cancelled", "endedReason": reason, "endedAt": now_iso()}
            if stopped := store.claim("campaigns", campaign_id, status, stop):
                return stopped
        raise HTTPException(status_code=409, detail="the campaign changed while stopping it; try again")

    @app.delete("/campaign/{campaign_id}", dependencies=auth_deps)
    def delete_campaign(campaign_id: str) -> dict[str, Any]:
        """Deletes the campaign; it calls nobody else, and calls already going finish."""
        _get_or_404("campaigns", campaign_id)
        return store.delete("campaigns", campaign_id)

    # Browser calls outlive the request that starts them; holding the tasks keeps them from being collected.
    web_calls: set[asyncio.Task] = set()

    @app.post("/call/web", status_code=201, dependencies=auth_deps)
    async def create_web_call(body: dict[str, Any]) -> dict[str, Any]:
        """Talk to an assistant from the browser: answers the page's WebRTC offer and starts the call."""
        from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

        from .bot import run_call, web_transport

        target = _get_or_404("squads", str(body["squadId"])) if body.get("squadId") else \
            _get_or_404("assistants", str(body.get("assistantId") or ""))
        if not body.get("sdp"):
            raise HTTPException(status_code=400, detail="sdp is required: the browser's WebRTC offer")
        # Variables have no caller to fill them here; they say nothing rather than "{{customer.name}}".
        try:
            if overrides.changes_assistant(body.get("assistantOverrides")):
                for member in squads.cast(target, store, body.get("assistantOverrides")):
                    _validate_assistant(member)
            plan = _call_plan(target, body.get("assistantOverrides"))
        except ProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        connection = SmallWebRTCConnection()
        try:
            await connection.initialize(sdp=body["sdp"], type=body.get("type") or "offer")
        except Exception as exc:  # aiortc raises several error types for a malformed offer
            raise HTTPException(status_code=400, detail=f"invalid WebRTC offer: {exc}") from exc
        call = store.create(
            "calls",
            {"type": "webCall", "status": "in-progress", "startedAt": now_iso(),
             "squadId" if squads.is_squad(target) else "assistantId": target["id"]},
        )
        task = asyncio.create_task(run_call(web_transport(connection), plan=plan, call_id=call["id"], store=store))
        web_calls.add(task)
        task.add_done_callback(web_calls.discard)
        answer = connection.get_answer()
        return {**call, "answer": {"sdp": answer["sdp"], "type": answer["type"]}}

    # ---- Twilio -------------------------------------------------------------------
    async def twilio_form(request: Request) -> dict[str, str]:
        """The webhook's form fields, once the request is proven to come from Twilio."""
        form = {k: str(v) for k, v in (await request.form()).items()}
        if not settings.validate_twilio_signature:
            return form
        # Without the token there is no way to tell Twilio from anyone else, so refuse.
        if not settings.twilio_auth_token:
            logger.error("refused a call: set TWILIO_AUTH_TOKEN so OpenRing can check it comes from Twilio")
            raise HTTPException(
                status_code=403, detail="TWILIO_AUTH_TOKEN is not set, so the Twilio signature cannot be checked"
            )
        # Twilio signs the exact URL it requested, query string included.
        base = settings.public_url or f"{request.url.scheme}://{request.url.netloc}"
        url = base + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if not is_valid_signature(settings.twilio_auth_token, url, form, request.headers.get("X-Twilio-Signature", "")):
            logger.warning("refused a request to {}: invalid Twilio signature", url)
            raise HTTPException(status_code=403, detail="invalid Twilio signature")
        return form

    async def answer_inbound(
        caller: str, called: str, provider_call_id: str | None, provider: str
    ) -> dict[str, Any] | str:
        """Record an incoming call and pick its assistant. Returns the call, or what to tell the caller."""
        number = store.find_phone_number(called)
        assistant_id = (number or {}).get("assistantId")
        squad_id = None if assistant_id else (number or {}).get("squadId")
        ask = server_of(number) if number and not assistant_id and not squad_id else None
        assistant_id = assistant_id or (None if ask or squad_id else settings.default_assistant_id)
        answers = store.get("squads", squad_id) if squad_id else store.get("assistants", assistant_id or "")
        if not ask and answers is None:
            return "No assistant is attached to this number."

        call = store.create(
            "calls",
            {
                "type": "inboundPhoneCall",
                "status": "queued",
                "squadId" if squad_id else "assistantId": squad_id or assistant_id,
                "phoneNumberId": (number or {}).get("id"),
                "customer": {"number": caller},
                "phoneCallProvider": provider,
                "phoneCallProviderId": provider_call_id,
            },
        )
        if ask:
            try:
                answer = await request_assistant(ask, call, number)
                if "assistantId" in answer and store.get("assistants", answer["assistantId"]) is None:
                    raise AssistantRequestError("assistant-request-returned-unknown-assistant",
                                                f"assistant {answer['assistantId']} not found")
                if "squadId" in answer and store.get("squads", answer["squadId"]) is None:
                    raise AssistantRequestError("assistant-request-returned-unknown-assistant",
                                                f"squad {answer['squadId']} not found")
                if "assistant" in answer:
                    _validate_assistant(answer["assistant"])
                if "squad" in answer:
                    _validate_squad(answer["squad"])
            except (AssistantRequestError, HTTPException) as exc:
                reason = getattr(exc, "ended_reason", "assistant-request-returned-invalid-assistant")
                logger.warning("call {}: assistant-request to {} failed: {}", call["id"],
                               netguard.safe_url_for_logs(ask["url"]), getattr(exc, "detail", exc))
                store.update("calls", call["id"], {"status": "ended", "endedReason": reason, "endedAt": now_iso()})
                return "Sorry, this line cannot take calls right now."
            call = store.update("calls", call["id"], answer)
        logger.info("{} call {} from {} to {}; streaming it to {}", provider, call["id"], caller, called,
                    call.get("assistantId") or call.get("squadId") or "a transient assistant")
        return call

    def left_on_voicemail(call: dict[str, Any], answered_by: str) -> str | None:
        """A machine answered an outbound call. The TwiML that ends the call: a recording, or just hanging up.
        None when the assistant has a sentence to leave: the call goes on to it, with `answeredBy` on the record."""
        try:
            assistant = squads.cast(_call_target(call) or {}, store, call.get("assistantOverrides"),
                                    call.get("customer"))[0]
        except ProviderError:  # nobody left to answer: hang up
            assistant = {}
        message = str(assistant.get("voicemailMessage") or "").strip()
        logger.info("call {}: answered by {}", call["id"], answered_by)
        if message and answered_by != "fax" and not twilio.is_recording(message):
            store.claim("calls", call["id"], "queued", {"answeredBy": answered_by})  # so a replay changes nothing
            return None
        patch = {"status": "ended", "endedReason": "voicemail", "endedAt": now_iso(), "answeredBy": answered_by}
        store.claim("calls", call["id"], "queued", patch)
        return twilio.play_twiml(message) if message and answered_by != "fax" else twilio.hangup_twiml()

    @app.post("/twilio/voice")
    async def twilio_voice(request: Request) -> Response:
        form = await twilio_form(request)
        if not settings.public_url:
            logger.error("PUBLIC_URL is not set; Twilio cannot reach the media websocket")
            return Response(say_twiml("This line is not configured yet."), media_type="application/xml")

        # An outbound call we placed (POST /call): the callee answered and Twilio asks what to play.
        if call_id := request.query_params.get("callId"):
            call = store.get("calls", call_id)
            if not call or call.get("phoneCallProviderId") not in (None, form.get("CallSid")):
                logger.warning("call {}: Twilio call {} is not this call; not connecting", call_id, form.get("CallSid"))
                return Response(say_twiml("Sorry, this call could not be connected."), media_type="application/xml")
            if form.get("AnsweredBy") in twilio.MACHINE and (twiml := left_on_voicemail(call, form["AnsweredBy"])):
                return Response(twiml, media_type="application/xml")
            logger.info("call {} answered; streaming it to its assistant", call_id)
            params = {"callId": call_id, **({"assistantId": call["assistantId"]} if call.get("assistantId") else {})}
            return Response(stream_twiml(settings.public_ws_url + "/ws/twilio", params), media_type="application/xml")

        # A call through a Twilio SIP domain or trunk says sip:+1555...@...; the number in it is what counts.
        answer = await answer_inbound(sip.user_of(form.get("From", "")), sip.user_of(form.get("To", "")),
                                      form.get("CallSid"), "twilio")
        if isinstance(answer, str):
            return Response(say_twiml(answer), media_type="application/xml")
        params = {"callId": answer["id"]}
        if answer.get("assistantId"):
            params["assistantId"] = answer["assistantId"]
        return Response(stream_twiml(settings.public_ws_url + "/ws/twilio", params), media_type="application/xml")

    async def telnyx_form(request: Request) -> dict[str, str]:
        """The webhook's fields, once its Ed25519 signature proves it comes from Telnyx."""
        body = await request.body()
        if settings.validate_telnyx_signature:
            if not settings.telnyx_public_key:
                logger.error("refused a call: set TELNYX_PUBLIC_KEY so OpenRing can check it comes from Telnyx")
                raise HTTPException(status_code=403,
                                    detail="TELNYX_PUBLIC_KEY is not set, so the signature cannot be checked")
            timestamp = request.headers.get("telnyx-timestamp", "")
            signature = request.headers.get("telnyx-signature-ed25519", "")
            if not telnyx.is_valid_signature(settings.telnyx_public_key, timestamp, body, signature):
                logger.warning("refused a request to {}: invalid Telnyx signature", request.url.path)
                raise HTTPException(status_code=403, detail="invalid Telnyx signature")
        if request.headers.get("content-type", "").startswith("application/json"):
            data = json.loads(body or b"{}")
            return {str(k): str(v) for k, v in (data if isinstance(data, dict) else {}).items()}
        return {k: str(v) for k, v in (await request.form()).items()}

    @app.post("/telnyx/voice")
    async def telnyx_voice(request: Request) -> Response:
        """Voice URL of a Telnyx TeXML Application: connects an incoming call to its assistant."""
        form = await telnyx_form(request)
        if not settings.public_url:
            logger.error("PUBLIC_URL is not set; Telnyx cannot reach the media websocket")
            return Response(say_twiml("This line is not configured yet."), media_type="application/xml")
        answer = await answer_inbound(sip.user_of(form.get("From", "")), sip.user_of(form.get("To", "")),
                                      form.get("CallSid") or form.get("CallControlId"), "telnyx")
        if isinstance(answer, str):
            return Response(say_twiml(answer), media_type="application/xml")
        # Telnyx's stream start carries no custom parameters, so the call id rides in the websocket URL.
        url = f"{settings.public_ws_url}/ws/telnyx?callId={answer['id']}"
        return Response(telnyx.stream_texml(url), media_type="application/xml")

    @app.post("/twilio/status", status_code=204)
    async def twilio_status(request: Request) -> Response:
        """Final status of an outbound call. Records the calls that never reached the assistant."""
        form = await twilio_form(request)
        logger.info("call {}: Twilio status {}", request.query_params.get("callId"), form.get("CallStatus"))
        if reason := UNREACHED.get(form.get("CallStatus", "")):
            # Only a call still queued: one whose stream connected is closed by the bot itself.
            patch = {"status": "ended", "endedReason": reason, "endedAt": now_iso()}
            store.claim("calls", request.query_params.get("callId", ""), "queued", patch)
        return Response(status_code=204)

    async def run_phone_call(
        call: dict[str, Any],
        refuse: Callable[[str], Awaitable[None]],
        build: Callable[[], Any],
        transfer: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Run a claimed phone call with its assistant. `refuse(why)` hangs up when the call can't start."""
        from .bot import PHONE_SAMPLE_RATE, run_call

        call_id = call["id"]
        logger.info("call {} media stream connected", call_id)
        # The assistant comes from the call record, never from the stream's parameters.
        target = _call_target(call)
        if target is None:
            store.update("calls", call_id, {"status": "ended", "endedReason": "assistant-not-found"})
            await refuse("the call's assistant was deleted")
            return
        # This call's overrides and variables: a campaign's `{{customer.name}}`, an assistant-request's changes.
        try:
            plan = _call_plan(target, call.get("assistantOverrides"), call.get("customer"))
        except ProviderError as exc:
            logger.error("call {} cannot start: {}", call_id, exc)
            store.update("calls", call_id, {"status": "ended", "endedReason": f"provider-error: {exc}"})
            await refuse(str(exc))
            return
        if call.get("answeredBy"):  # a voicemail answered (see left_on_voicemail): leave the message and hang up
            plan.first_message, plan.leave_message = plan.assistant.get("voicemailMessage"), True
        await run_call(build(), plan=plan, call_id=call_id, store=store, sample_rate=PHONE_SAMPLE_RATE,
                       transfer=transfer)

    async def media_stream(websocket: WebSocket, provider: str) -> None:
        from pipecat.runner.utils import parse_telephony_websocket

        from .bot import telnyx_transport, twilio_transport

        await websocket.accept()
        try:
            transport, call_data = await asyncio.wait_for(parse_telephony_websocket(websocket), HANDSHAKE_TIMEOUT)
        except TimeoutError:
            await websocket.close(code=1008)
            return
        except ValueError:  # closed before the handshake
            return
        if provider == "twilio":
            call_id = str((call_data.get("body") or {}).get("callId") or "")
        else:
            call_id = websocket.query_params.get("callId", "")
        # Claiming moves the call out of "queued" in one step, so each call gets exactly one
        # media stream: a replayed or second connection for the same call is refused.
        call = None
        if transport == provider and call_id:
            call = store.claim("calls", call_id, "queued", {"status": "in-progress", "startedAt": now_iso()})
        if call is None:
            logger.warning("refused media stream for call {}: unknown or already connected", call_id or "?")
            await websocket.close(code=1008)
            return
        transfer = None
        call_sid = call.get("phoneCallProviderId") or call_data.get("call_id")
        if provider == "twilio" and settings.twilio_account_sid and settings.twilio_auth_token and call_sid:

            async def transfer(number: str) -> None:
                twiml = twilio.dial_twiml(_e164(number))
                await twilio.redirect_call(settings.twilio_account_sid, settings.twilio_auth_token, call_sid, twiml)

        async def hang_up(_why: str) -> None:
            await websocket.close()

        build = twilio_transport if provider == "twilio" else telnyx_transport
        await run_phone_call(call, hang_up, lambda: build(websocket, dict(call_data), settings), transfer)

    async def run_sip_call(call_id: str, session: sip.Session) -> None:
        """A SIP call that's connected, either way: it runs like any phone call, and is hung up when it ends."""
        call = store.claim("calls", call_id, "queued", {"status": "in-progress", "startedAt": now_iso()})
        if call is None:
            logger.warning("refused SIP call {}: unknown or already connected", call_id)
            await session.hang_up()
            return

        async def refuse(_why: str) -> None:
            await session.hang_up()

        try:
            await run_phone_call(call, refuse, lambda: sip.transport_for(session), session.transfer)
        finally:
            await session.hang_up()

    def sip_call_failed(call_id: str, reason: str) -> None:
        """A SIP call that never reached the assistant."""
        store.claim("calls", call_id, "queued", {"status": "ended", "endedReason": reason, "endedAt": now_iso()})

    async def answer_sip(caller: str, called: str, sip_call_id: str) -> dict[str, Any] | str:
        return await answer_inbound(caller, called, sip_call_id, "sip")

    low, _, high = settings.sip_rtp_ports.partition("-")
    sip_server = sip.Server(
        host=settings.sip_host, port=settings.sip_port, public_ip=settings.sip_public_ip,
        rtp_ports=(int(low), int(high or low)), trunks=lambda: store.list("credentials", limit=1000),
        answer=answer_sip, run=run_sip_call, failed=sip_call_failed,
    )
    app.state.sip = sip_server

    @app.websocket("/ws/twilio")
    async def twilio_media(websocket: WebSocket) -> None:
        await media_stream(websocket, "twilio")

    @app.websocket("/ws/telnyx")
    async def telnyx_media(websocket: WebSocket) -> None:
        await media_stream(websocket, "telnyx")

    # ---- Genesys Cloud (Audio Connector, AudioHook protocol) ------------------------
    def genesys_refusal(websocket: WebSocket) -> str | None:
        """Why this connection is not Genesys with our Audio Connector credentials, or None when it is."""
        if not (settings.genesys_api_key and settings.genesys_client_secret):
            return "set GENESYS_API_KEY and GENESYS_CLIENT_SECRET (Providers > Genesys Cloud)"
        given = websocket.headers.get("x-api-key", "")
        if not hmac.compare_digest(given.encode(), settings.genesys_api_key.encode()):
            return "wrong X-API-KEY"
        target = websocket.url.path + (f"?{websocket.url.query}" if websocket.url.query else "")
        authority = urlsplit(settings.public_url).netloc or websocket.headers.get("host", "")
        return genesys.check_signature(dict(websocket.headers.items()), target, authority,
                                       api_key=settings.genesys_api_key, client_secret=settings.genesys_client_secret)

    @app.websocket("/ws/genesys")
    async def genesys_media(websocket: WebSocket) -> None:
        """A Genesys Cloud call. The Architect flow picks the assistant with an `assistantId` input variable, or
        the called number does; other input variables fill the assistant's {{variables}}. Back in Architect,
        output variables say what happened: `action` is "finished" or "transfer" (with `transferTo`)."""
        from pipecat.serializers.genesys import GenesysAudioHookSerializer

        from .bot import genesys_transport

        if problem := genesys_refusal(websocket):
            logger.warning("refused a Genesys connection: {}", problem)
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            first = await asyncio.wait_for(websocket.receive_text(), HANDSHAKE_TIMEOUT)
            opening = json.loads(first)
        except WebSocketDisconnect:
            return
        except (TimeoutError, ValueError, KeyError):  # silence, or not JSON text
            await websocket.close(code=1008)
            return
        if not isinstance(opening, dict) or opening.get("type") != "open":
            await websocket.close(code=1008)
            return
        serializer = GenesysAudioHookSerializer()
        opened = await serializer.deserialize(first)  # also keeps the session's caller and variables
        await websocket.send_text(json.dumps(opened.message))

        async def refuse(why: str) -> None:
            """Ends the session with an error, so the Architect flow takes its failure path."""
            logger.warning("Genesys call {} refused: {}", serializer.conversation_id, why)
            message = serializer.create_disconnect_message(reason="error", action="finished", info=why)
            await websocket.send_text(json.dumps(message))
            await websocket.close()

        participant = serializer.participant or {}
        inputs = dict(serializer.input_variables or {})
        caller, called = genesys.number(participant.get("ani")), genesys.number(participant.get("dnis"))
        if wanted := str(inputs.pop("assistantId", "") or ""):
            if store.get("assistants", wanted) is None:
                await refuse(f"assistant {wanted} not found")
                return
            number = store.find_phone_number(called) if called else None
            call: dict[str, Any] | str = store.create("calls", {
                "type": "inboundPhoneCall", "status": "queued", "assistantId": wanted,
                "phoneNumberId": (number or {}).get("id"), "customer": {"number": caller},
                "phoneCallProvider": "genesys", "phoneCallProviderId": serializer.conversation_id,
            })
        else:
            call = await answer_inbound(caller, called, serializer.conversation_id, "genesys")
        if isinstance(call, str):
            await refuse(call)
            return
        customer = dict(call.get("customer") or {})
        if participant.get("aniName"):
            customer["name"] = participant["aniName"]
        patch: dict[str, Any] = {"customer": customer, "status": "in-progress", "startedAt": now_iso()}
        if inputs:
            patch["assistantOverrides"] = overrides.combine({"variableValues": inputs}, call.get("assistantOverrides"))
        claimed = store.claim("calls", call["id"], "queued", patch)
        if claimed is None:
            await refuse("the call is already connected")
            return
        serializer.set_output_variables({"action": "finished", "callId": claimed["id"]})

        async def transfer(number: str) -> None:
            """Genesys moves the call: the Architect flow reads `action` and `transferTo` when the connector ends."""
            serializer.set_output_variables({"action": "transfer", "transferTo": number, "callId": claimed["id"]})
            message = serializer.create_disconnect_message(reason="completed",
                                                           output_variables=serializer.output_variables)
            await websocket.send_text(json.dumps(message))

        await run_phone_call(claimed, refuse, lambda: genesys_transport(websocket, serializer), transfer)

    return app
