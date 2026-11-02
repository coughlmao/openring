import socket

import pytest

from openring import __version__
from openring.__main__ import ensure_port_free, main, port_in_use, reset_password
from openring.auth import Sessions
from openring.store import Store


def test_version_prints_version_and_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"openring {__version__}"


def test_busy_port_is_reported_with_a_way_out(capsys):
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]
        assert port_in_use("127.0.0.1", port)
        with pytest.raises(SystemExit):
            ensure_port_free("127.0.0.1", port)
    out = capsys.readouterr().out
    assert f"Port {port} is already in use" in out and f"openring --port {port + 1}" in out
    assert not port_in_use("127.0.0.1", port)


def test_reset_password_lets_you_choose_a_new_one(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("OPENRING_PASSWORD", raising=False)
    db = str(tmp_path / "openring.db")
    sessions = Sessions(Store(db))
    sessions.set_password("forgotten password")
    token = sessions.start()
    reset_password(db)
    after = Sessions(Store(db))
    assert not after.password_set and not after.valid(token)
    assert "choose a new one" in capsys.readouterr().out


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty OpenRing home and no API key in the environment."""
    import openring.__main__ as cli

    monkeypatch.setattr(cli, "HOME", tmp_path)
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("OPENRING_API_KEY", raising=False)
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")  # no project-local .env
    return cli


def _start(cli):
    """What `openring` does with the API key at startup, without starting the server."""
    import os

    cli.load_settings()
    return os.environ.get("OPENRING_API_KEY", "")


def test_an_example_key_in_the_environment_is_replaced_and_kept(home, monkeypatch):
    # Docker: env_file .env (copied from .env.example) sets OPENRING_API_KEY=change-me.
    monkeypatch.setenv("OPENRING_API_KEY", "change-me")
    first = _start(home)
    assert first and first != "change-me" and len(first) >= 30
    monkeypatch.setenv("OPENRING_API_KEY", "change-me")  # the container restarts with the same env_file
    assert _start(home) == first  # the saved key, not a new one each time


def test_an_example_key_in_the_settings_file_is_replaced(home, monkeypatch):
    home.ENV_FILE.write_text("OPENRING_API_KEY=change-me\nPUBLIC_URL=\n", encoding="utf-8")
    key = _start(home)
    assert key and key != "change-me"
    assert f"OPENRING_API_KEY={key}" in home.ENV_FILE.read_text(encoding="utf-8")


def test_an_empty_environment_variable_does_not_hide_the_saved_key(home, monkeypatch):
    home.ENV_FILE.write_text("OPENRING_API_KEY=saved-key-1234567890\n", encoding="utf-8")
    monkeypatch.setenv("OPENRING_API_KEY", "")  # Docker's env_file with OPENRING_API_KEY=
    assert _start(home) == "saved-key-1234567890"


def test_a_deliberately_empty_key_stays_empty(home, monkeypatch):
    home.ENV_FILE.write_text("OPENRING_API_KEY=\n", encoding="utf-8")
    assert _start(home) == ""  # the API then answers only this computer


def test_startup_lines_reach_a_pipe_while_the_server_runs(tmp_path):
    """Docker logs, systemd and `> log` read stdout through a pipe; the startup lines must not wait in a buffer."""
    import os
    import subprocess
    import sys
    import threading
    import time
    from pathlib import Path

    with socket.socket() as s:  # a free port: the server is never really started, but the port is checked
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script = "\n".join([
        "import time, uvicorn, openring.providers as p, openring.__main__ as m",
        "p.warm_up = lambda: None",  # no Kokoro download in a test
        "uvicorn.run = lambda *a, **k: time.sleep(60)",  # the server keeps running
        f"m.main(['--port', '{port}'])",
    ])
    env = {**os.environ, "OPENRING_HOME": str(tmp_path), "OPENRING_API_KEY": "change-me",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}  # this checkout's openring
    env.pop("PYTHONUNBUFFERED", None)
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=env, text=True, cwd=tmp_path)
    lines: list[str] = []
    threading.Thread(target=lambda: lines.extend(proc.stdout), daemon=True).start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not any("OpenRing is running" in line for line in lines):
            time.sleep(0.2)
    finally:
        proc.kill()
    assert any("OpenRing is running" in line for line in lines), lines
    assert any("example value anyone can guess" in line for line in lines)


@pytest.mark.parametrize(("arguments", "environment", "expected"), [
    ([], None, "127.0.0.1"),
    ([], "192.0.2.10", "192.0.2.10"),
    (["--host", "192.0.2.20"], None, "192.0.2.20"),
    (["--host", "localhost"], "192.0.2.10", "localhost"),
])
def test_host_selection(home, monkeypatch, arguments, environment, expected):
    if environment is None:
        monkeypatch.delenv("HOST", raising=False)
    else:
        monkeypatch.setenv("HOST", environment)
    monkeypatch.setattr(home, "load_settings", lambda: (False, False))
    checked = []

    def check_port(host, port):
        checked.append((host, port))
        raise SystemExit(0)

    monkeypatch.setattr(home, "ensure_port_free", check_port)
    with pytest.raises(SystemExit) as stopped:
        home.main(arguments + ["--port", "8123"])
    assert stopped.value.code == 0
    assert checked == [(expected, 8123)]


def test_host_help_mentions_dashboard_exposure_and_password(home, capsys):
    with pytest.raises(SystemExit) as stopped:
        home.main(["--help"])
    assert stopped.value.code == 0
    help_text = capsys.readouterr().out
    assert "--host" in help_text
    assert "exposes the dashboard" in help_text
    assert "OPENRING_PASSWORD" in help_text
