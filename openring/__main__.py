"""`openring` command: first run creates ~/.openring with a generated API key, then starts the server."""

import argparse
import os
import secrets
import socket
import sys
from pathlib import Path

from . import __version__
from .config import is_placeholder_key

HOME = Path(os.environ.get("OPENRING_HOME", Path.home() / ".openring"))
ENV_FILE = HOME / ".env"

ENV_TEMPLATE = """# OpenRing settings. Edit and restart `openring` to apply.
OPENRING_API_KEY={key}
# Dashboard password. Leave empty to choose it in the browser on first visit (from this computer).
# Set it for servers you reach over the network (Docker, VPS). Forgot it? Run: openring --reset-password
OPENRING_PASSWORD=
# Public https URL Twilio can reach (e.g. from `ngrok http 8000`)
PUBLIC_URL=
TWILIO_ACCOUNT_SID=
# Required to answer calls: it proves each webhook really comes from Twilio
TWILIO_AUTH_TOKEN=
# Speech-to-text: Deepgram gives US$200 free credit to new accounts
DEEPGRAM_API_KEY=
# LLM: any of these (or run OmniRoute locally for free models)
OPENAI_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=
OMNIROUTE_BASE_URL=http://localhost:20128/v1
# Text-to-speech: leave empty to use Kokoro (free, local)
ELEVENLABS_API_KEY=
"""


def load_env_file(path: Path) -> bool:
    """Load KEY=VALUE lines; real environment variables win, unless they are empty.

    An example API key ("change-me") is skipped. Returns whether one was, so a real key can replace it.
    """
    if not path.exists():
        return False
    skipped = False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key == "OPENRING_API_KEY" and is_placeholder_key(value):
            skipped = True
            continue
        if value and not os.environ.get(key, "").strip():
            os.environ[key] = value
    return skipped


def drop_example_api_key() -> bool:
    """Forget an OPENRING_API_KEY environment variable that holds an example value. Returns whether it did."""
    if is_placeholder_key(os.environ.get("OPENRING_API_KEY", "")):
        del os.environ["OPENRING_API_KEY"]
        return True
    return False


def save_new_api_key() -> str:
    """Generate an API key, keep it in the settings file and use it now."""
    key = secrets.token_urlsafe(24)
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("OPENRING_API_KEY="):
            lines[i] = f"OPENRING_API_KEY={key}"
            break
    else:
        lines.append(f"OPENRING_API_KEY={key}")
    HOME.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["OPENRING_API_KEY"] = key
    return key


def first_run_setup() -> bool:
    if ENV_FILE.exists():
        return False
    HOME.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(ENV_TEMPLATE.format(key=secrets.token_urlsafe(24)), encoding="utf-8")
    return True


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        if os.name != "nt":  # like uvicorn, ignore connections still closing from a previous run
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def ensure_port_free(host: str, port: int) -> None:
    if port_in_use(host, port):
        print(f"\n  Port {port} is already in use, maybe by another OpenRing.")
        print(f"  Stop that program, or start on another port: openring --port {port + 1}\n")
        raise SystemExit(1)


def reset_password(db_path: str) -> None:
    from .auth import Sessions
    from .store import Store

    Sessions(Store(db_path)).reset_password()
    print("\n  Dashboard password removed. Start openring and open http://localhost to choose a new one.")
    if os.environ.get("OPENRING_PASSWORD"):
        print("  OPENRING_PASSWORD is set, so it becomes the password again on the next start.")
    print()


def load_settings() -> tuple[bool, bool]:
    """Create the settings file on the first run, then read the settings into the environment.

    An example API key ("change-me" from .env.example) is ignored: the key saved in the settings file applies,
    or a generated one, saved so the next start uses the same key. Returns (settings file created now,
    example key ignored), so the command can say which key is in use.
    """
    example_key = drop_example_api_key()
    created = first_run_setup()
    example_key |= load_env_file(Path.cwd() / ".env")  # a project-local .env takes precedence
    example_key |= load_env_file(ENV_FILE)
    if example_key and not os.environ.get("OPENRING_API_KEY"):
        save_new_api_key()
    return created, example_key


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="openring", description="Self-hosted voice AI agents, Vapi-compatible.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--port", type=int, help="port to serve on (default: $PORT, or 8000)")
    parser.add_argument("--host", help="host to bind (default: $HOST, or 127.0.0.1); anything other than "
                        "localhost exposes the dashboard, so set OPENRING_PASSWORD")
    parser.add_argument("--reset-password", action="store_true",
                        help="forget the dashboard password; choose a new one at http://localhost on the next visit")
    args = parser.parse_args(argv)
    # Docker, systemd and `> log` give stdout to a pipe, where Python holds prints until a buffer fills:
    # the startup lines (and the notice about a replaced API key) would never show. One line at a time.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    created, example_key = load_settings()
    if example_key:
        # Clients still sending the example key get 401; say which key the server uses instead.
        print("\n  OPENRING_API_KEY was an example value anyone can guess, so it is ignored.")
        print(f"  The API key is the one in {ENV_FILE} (also under Settings in the dashboard).")
    os.environ.setdefault("OPENRING_DB_PATH", str(HOME / "openring.db"))
    if args.reset_password:
        reset_password(os.environ["OPENRING_DB_PATH"])
        return
    host = args.host if args.host is not None else os.environ.get("HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("PORT", "8000"))
    ensure_port_free(host, port)

    import uvicorn

    from .api import create_app
    from .providers import kokoro_installed, kokoro_ready, warm_up

    os.environ["OPENRING_ENV_FILE"] = str(ENV_FILE)  # error messages point people to it
    print(f"\n  OpenRing is running: http://localhost:{port}")
    print(f"  Settings: {ENV_FILE}" + ("  (created now)" if created else ""))
    if kokoro_installed() and not kokoro_ready():
        print("  Downloading the free Kokoro voice in the background (one time, about 120 MB).")
    warm_up()
    print("  Press Ctrl+C to stop.\n")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
