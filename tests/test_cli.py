import re

from beyondmeetings.cli import build_parser, placeholder_name


def test_start_accepts_a_name():
    args = build_parser().parse_args(["start", "Client Kickoff"])
    assert args.command == "start"
    assert args.name == "Client Kickoff"


def test_start_name_is_optional():
    args = build_parser().parse_args(["start"])
    assert args.name is None


def test_placeholder_name_uses_the_clock():
    assert re.fullmatch(r"recording-\d{2}-\d{2}", placeholder_name())


def test_stop_takes_no_arguments():
    assert build_parser().parse_args(["stop"]).command == "stop"


def test_notes_accepts_a_transcript_path():
    args = build_parser().parse_args(["notes", "/tmp/t.txt"])
    assert args.command == "notes"
    assert args.transcript == "/tmp/t.txt"


def test_stop_without_a_recording_exits_cleanly(tmp_path, monkeypatch, capsys):
    """No traceback — this is the most likely first-run mistake."""
    import pytest

    from beyondmeetings import cli
    from beyondmeetings.config import Config

    monkeypatch.setattr(cli, "load_config", lambda: Config(data_dir=str(tmp_path)))
    with pytest.raises(SystemExit) as exc:
        cli.main(["stop"])
    assert "Nothing to stop" in str(exc.value)


# --- Review finding #1: cli stop and the app were two divergent pipelines ---

def test_cli_stop_delegates_to_the_shared_session(monkeypatch, tmp_path, capsys):
    """There must be exactly one stop implementation."""
    import pytest

    from beyondmeetings import cli
    from beyondmeetings.config import Config

    calls = []

    class FakeSession:
        def run_stop(self):
            calls.append("run_stop")
            return {"phase": "done", "note_path": "/v/Meetings/2026-07-30/N.md",
                    "transcript_path": "/d/t.txt", "error": None}

    monkeypatch.setattr(cli, "load_config", lambda: Config(data_dir=str(tmp_path)))
    monkeypatch.setattr(cli, "_session", lambda c, d: FakeSession())
    assert cli.main(["stop"]) == 0
    assert calls == ["run_stop"]
    assert "Note written" in capsys.readouterr().out


def test_cli_stop_reports_a_failed_stop_with_the_transcript_path(monkeypatch,
                                                                 tmp_path, capsys):
    import pytest

    from beyondmeetings import cli
    from beyondmeetings.config import Config

    class FakeSession:
        def run_stop(self):
            return {"phase": "failed", "note_path": None,
                    "transcript_path": "/d/t.txt", "error": "api down"}

    monkeypatch.setattr(cli, "load_config", lambda: Config(data_dir=str(tmp_path)))
    monkeypatch.setattr(cli, "_session", lambda c, d: FakeSession())
    with pytest.raises(SystemExit, match="api down"):
        cli.main(["stop"])
    assert "/d/t.txt" in capsys.readouterr().out


def test_placeholder_name_is_defined_once():
    """cli re-exports session's, rather than repeating the rule."""
    from beyondmeetings import cli, session
    assert cli.placeholder_name is session.placeholder_name


# --- Interrupted stop left audio unreachable; the CLI was also silent ---

def test_notes_accepts_a_recording_not_just_a_transcript(monkeypatch, tmp_path,
                                                        capsys):
    """Recovery path: a killed stop leaves a .wav and no transcript."""
    from beyondmeetings import cli
    from beyondmeetings.config import Config
    from beyondmeetings.segments import transcript_path

    wav = tmp_path / "seg000.wav"
    wav.write_bytes(b"RIFF")
    transcript_path(wav).write_text("cached from the interrupted run")

    seen = {}
    monkeypatch.setattr(cli, "load_config", lambda: Config(data_dir=str(tmp_path)))
    monkeypatch.setattr(cli, "build_transcriber", lambda c: object())
    monkeypatch.setattr(
        cli, "generate_notes",
        lambda text, cfg, prov, *a: seen.setdefault("text", text) or tmp_path / "n.md",
    )
    monkeypatch.setattr(cli, "_provider", lambda c: object())

    assert cli.main(["notes", str(wav)]) == 0
    assert seen["text"] == "cached from the interrupted run"


def test_notes_rejects_a_missing_file(monkeypatch, tmp_path):
    import pytest

    from beyondmeetings import cli
    from beyondmeetings.config import Config

    monkeypatch.setattr(cli, "load_config", lambda: Config(data_dir=str(tmp_path)))
    with pytest.raises(SystemExit, match="No such file"):
        cli.main(["notes", str(tmp_path / "absent.txt")])


def test_stop_reports_progress_instead_of_sitting_silent(monkeypatch, tmp_path,
                                                         capsys):
    """32 minutes of silent work made a user assume a hang and Ctrl+C out."""
    from beyondmeetings import cli
    from beyondmeetings.config import Config

    class FakeSession:
        on_phase_change = None

        def status(self):
            return {"detail": "Transcribing segment 1 of 1"}

        def run_stop(self):
            if self.on_phase_change:
                self.on_phase_change("transcribing")
            return {"phase": "done", "note_path": "/v/n.md",
                    "transcript_path": "/d/t.txt", "error": None}

    monkeypatch.setattr(cli, "load_config", lambda: Config(data_dir=str(tmp_path)))
    monkeypatch.setattr(cli, "_session", lambda c, d: FakeSession())
    cli.main(["stop"])
    assert "Transcribing segment 1 of 1" in capsys.readouterr().out


# --- `setup`/`serve` handed the URL over before the server was listening ---

def _browser_race_probe(monkeypatch, tmp_path):
    """Records whether the browser was opened while the port was still closed."""
    import threading

    from beyondmeetings import cli, desktop, server
    from beyondmeetings.config import Config

    listening = threading.Event()
    opened, opened_too_early = [], []

    def fake_open(url):
        if not listening.is_set():
            opened_too_early.append(url)
        opened.append(url)
        return True

    monkeypatch.setattr(cli, "load_config", lambda: Config(data_dir=str(tmp_path)))
    monkeypatch.setattr(server, "create_app", lambda **kw: object())
    monkeypatch.setattr(desktop, "open_browser", fake_open)
    monkeypatch.setattr(desktop, "wait_for_server",
                        lambda port, timeout=0: listening.wait(10))
    return listening, opened, opened_too_early


def _settle(opened, listening):
    """Stand in for a server that stays up: bound, then serving."""
    import time

    listening.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not opened:
        time.sleep(0.01)


def test_setup_opens_the_browser_only_once_the_port_answers(monkeypatch, tmp_path):
    """An already-running browser navigates in milliseconds and beat uvicorn's bind."""
    import uvicorn

    from beyondmeetings import cli

    listening, opened, too_early = _browser_race_probe(monkeypatch, tmp_path)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: _settle(opened, listening))

    assert cli.main(["setup", "--port", "7788"]) == 0
    assert opened == ["http://127.0.0.1:7788/setup"]
    assert too_early == [], "browser was sent to a port nothing was listening on"


def test_setup_honours_no_browser(monkeypatch, tmp_path):
    import uvicorn

    from beyondmeetings import cli

    listening, opened, _ = _browser_race_probe(monkeypatch, tmp_path)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: listening.set())
    assert cli.main(["setup", "--port", "7788", "--no-browser"]) == 0
    assert opened == []


def test_serve_opens_the_browser_only_once_the_port_answers(monkeypatch, tmp_path):
    import uvicorn

    from beyondmeetings import cli

    listening, opened, too_early = _browser_race_probe(monkeypatch, tmp_path)

    class FakeServer:
        def __init__(self, config):
            pass

        def run(self):
            _settle(opened, listening)

    monkeypatch.setattr(uvicorn, "Config", lambda app, **kw: object())
    monkeypatch.setattr(uvicorn, "Server", FakeServer)

    assert cli.main(["serve", "--port", "7788", "--no-tray"]) == 0
    assert opened == ["http://127.0.0.1:7788/"]
    assert too_early == [], "browser was sent to a port nothing was listening on"
