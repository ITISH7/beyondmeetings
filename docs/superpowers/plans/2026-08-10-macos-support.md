# macOS Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a platform-dispatched capture layer so beyondMeetings can grow a macOS backend, and close the `Recorder` interface gaps that a second backend would otherwise expose at runtime.

**Architecture:** Today `PipeWireRecorder` is constructed directly in two places and the `Recorder` ABC declares only three of the six members the application actually calls. This plan adds an `audio/factory.py` dispatch point (mirroring the existing `transcribe/factory.py` and `llm/factory.py`), promotes the three undeclared members onto the ABC, moves platform-neutral helpers out of `pipewire.py`, and splits `desktop.py` into a package so a macOS launcher can live beside the Linux one.

**Tech Stack:** Python 3.10+, pytest, pydantic v2. No new dependencies. Every task in Tasks 1–4 is written and verified on Linux.

---

## Scope

This plan covers **Phase 2** of `docs/superpowers/specs/2026-08-10-macos-support-design.md` — the platform-neutral refactor — plus **Phase 4** (`MacRecorder`) as a gated task.

**Tasks 1–4 are executable now.** They need no Mac, change no behaviour on Linux, and are worth landing regardless of whether macOS ever ships: they close a real interface gap that exists today.

**Task 5 is gated** on the Phase 1 spike (below) and must not be started before it.

**Phases 3, 5 and 6 of the spec are deliberately not planned here.** They are the Swift helper, the `.app` bundle, the doctor changes and the docs. Each depends on the spike's outcome, and none can be written as verifiable TDD steps from a Linux machine — specifying "run this, expect PASS" for Swift that cannot be compiled or TCC behaviour that cannot be observed would be fabricated precision. They get their own plan once the spike returns.

---

## Precondition: the Phase 1 spike

**This gates Task 5 only. Tasks 1–4 may proceed immediately.**

The spike is exploratory work on a real Mac, not a TDD task. It exists to answer four questions before any macOS code is designed against them:

1. Does an audio-only `SCStream` (`capturesAudio = true`, minimal video config) actually deliver system audio on **macOS 13**? The API is documented as 13.0+, but audio-only capture is an unusual configuration.
2. **Who does TCC attribute the Screen Recording grant to** when a helper binary inside `beyondMeetings.app/Contents/MacOS/` is spawned by a Python process that was itself launched by the bundle's launcher? The bundle, the Python process, or something else?
3. Does that attribution differ when the same helper is spawned from `beyondmeetings start` typed into Terminal?
4. Does an `Info.plist` embedded into the helper via `-sectcreate __TEXT __info_plist` change the answer to either?

**If question 3 shows the terminal path prompts separately for Terminal**, the fallback in the spec applies: on macOS the CLI delegates `start`/`stop` to the bundle-launched server rather than spawning the helper itself. That decision changes Task 5's shape, which is why Task 5 is gated.

Record the answers in the spec's "Open risk" section before starting Task 5.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/beyondmeetings/audio/base.py` | `Recorder` ABC, `RecordingState`, state I/O, platform-neutral helpers | Modify — add 3 abstract members, receive 2 moved helpers |
| `src/beyondmeetings/audio/pipewire.py` | Linux capture only | Modify — 2 helpers move out |
| `src/beyondmeetings/audio/factory.py` | Chooses a backend for the running platform | **Create** |
| `src/beyondmeetings/audio/macos.py` | macOS capture via the `bmcapture` helper | **Create** (Task 5, gated) |
| `src/beyondmeetings/session.py` | Orchestration | Modify — one defensive `getattr` becomes direct access |
| `src/beyondmeetings/cli.py` | CLI entry point | Modify — construct via factory |
| `src/beyondmeetings/server.py` | HTTP app | Modify — construct via factory |
| `src/beyondmeetings/desktop/base.py` | Server lifecycle, browser opening — platform-neutral | **Create** (moved from `desktop.py`) |
| `src/beyondmeetings/desktop/linux.py` | freedesktop `.desktop` entry and icon | **Create** (moved from `desktop.py`) |
| `src/beyondmeetings/desktop/__init__.py` | Re-exports so existing imports keep working | **Create** |
| `src/beyondmeetings/desktop.py` | — | **Delete** (contents split above) |

Tests created: `tests/test_audio_interface.py`, `tests/test_audio_factory.py`, `tests/test_audio_macos.py` (Task 5).
Tests modified: `tests/test_audio_pipewire.py`.

---

## Task 1: Make the `Recorder` ABC declare what the app actually calls

The ABC declares `start`, `stop` and `status`. The application also calls
`roll_segment` (`rollover.py:39`), `reset` (`session.py:144`), and reads
`state_error` (`session.py:103`). With one backend that is invisible; with two,
an omitted member becomes a failure mid-meeting rather than at construction.

`session.py:103` currently reads `getattr(self.recorder, "state_error", None)` —
that defensive `getattr` exists *because* the attribute is not on the interface,
and it is removed here.

**Files:**
- Modify: `src/beyondmeetings/audio/base.py:47-58`
- Modify: `src/beyondmeetings/session.py:103`
- Test: `tests/test_audio_interface.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_audio_interface.py`:

```python
"""The Recorder ABC must declare every member the application calls.

RolloverWorker calls roll_segment() mid-meeting and SessionManager calls
reset() and reads state_error. While there was one backend these went
undeclared and nothing noticed; a second backend that omitted one would fail
during a recording instead of at construction.
"""
import pytest

from beyondmeetings.audio.base import Recorder

MEMBERS = {
    "start": lambda self, name: None,
    "stop": lambda self: None,
    "status": lambda self: None,
    "roll_segment": lambda self: "",
    "reset": lambda self: None,
    "state_error": property(lambda self: None),
}


@pytest.mark.parametrize("missing", sorted(MEMBERS))
def test_a_backend_missing_any_member_cannot_be_constructed(missing):
    members = dict(MEMBERS)
    del members[missing]
    partial = type("Partial", (Recorder,), members)

    with pytest.raises(TypeError, match=missing):
        partial()


def test_a_backend_implementing_everything_can_be_constructed():
    assert type("Complete", (Recorder,), dict(MEMBERS))() is not None


def test_the_linux_backend_satisfies_the_full_interface(tmp_path):
    from beyondmeetings.audio.pipewire import PipeWireRecorder

    assert isinstance(PipeWireRecorder(tmp_path), Recorder)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_audio_interface.py -v`

Expected: the three `roll_segment`/`reset`/`state_error` parametrisations FAIL — `Partial()` constructs successfully because the ABC does not require them, so `pytest.raises(TypeError)` is not satisfied. The `start`/`stop`/`status` cases and the last two tests PASS already.

- [ ] **Step 3: Add the three members to the ABC**

In `src/beyondmeetings/audio/base.py`, replace the `class Recorder(ABC):` block (lines 47-58) with:

```python
class Recorder(ABC):
    """A capture backend. One implementation per platform.

    Every member here is called by the application: RolloverWorker calls
    roll_segment() on a timer, SessionManager calls reset() to clear a wedged
    recording and reads state_error to explain one. Declaring them means a new
    backend that forgets one fails at construction, not mid-meeting.
    """

    @abstractmethod
    def start(self, name: str) -> RecordingState:
        ...

    @abstractmethod
    def stop(self) -> RecordingState:
        ...

    @abstractmethod
    def status(self) -> RecordingState | None:
        ...

    @abstractmethod
    def roll_segment(self) -> str:
        """End the current segment, start the next. Returns the finished path."""

    @abstractmethod
    def reset(self) -> None:
        """Forget a wedged recording. The UI's escape hatch."""

    @property
    @abstractmethod
    def state_error(self) -> str | None:
        """Why the state file was unreadable, or None."""
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_audio_interface.py -v`
Expected: PASS — 8 tests.

- [ ] **Step 5: Remove the defensive getattr the interface made unnecessary**

In `src/beyondmeetings/session.py:103`, replace:

```python
                "state_error": getattr(self.recorder, "state_error", None),
```

with:

```python
                "state_error": self.recorder.state_error,
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 569 passed plus the 8 new tests = 577 passed.

- [ ] **Step 7: Commit**

```bash
git add src/beyondmeetings/audio/base.py src/beyondmeetings/session.py tests/test_audio_interface.py
git commit -m "refactor: Recorder ABC now declares every member the app calls

RolloverWorker calls roll_segment and SessionManager calls reset and reads
state_error, none of which the ABC declared. With one backend that was
invisible; a second one omitting a member would have failed mid-meeting.

The defensive getattr in session.py existed only because state_error was not
on the interface, so it goes too."
```

---

## Task 2: Move platform-neutral helpers out of the Linux backend

`SubprocessRunner` and `build_filename_base` live in `pipewire.py` but neither
is Linux-specific — `build_filename_base` produces the shared
`YYYY-MM-DD_HH-MM_slug` naming that the whole pipeline depends on, and
`SubprocessRunner` is a generic `subprocess` wrapper. A macOS backend needs
both, and importing them from `pipewire` would be absurd.

**Files:**
- Modify: `src/beyondmeetings/audio/base.py`
- Modify: `src/beyondmeetings/audio/pipewire.py:11-33`
- Modify: `tests/test_audio_pipewire.py:3`
- Test: `tests/test_audio_interface.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_audio_interface.py`:

```python
# --- helpers shared by every backend live in base, not in the Linux one ---

def test_filename_base_is_importable_from_base():
    from beyondmeetings.audio.base import build_filename_base

    assert build_filename_base("Client Kickoff!", "2026-07-30", "14-30") == (
        "2026-07-30_14-30_client-kickoff"
    )


def test_subprocess_runner_is_importable_from_base():
    from beyondmeetings.audio.base import SubprocessRunner

    assert hasattr(SubprocessRunner(), "run")
    assert hasattr(SubprocessRunner(), "spawn")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_audio_interface.py -k importable -v`
Expected: FAIL — `ImportError: cannot import name 'build_filename_base' from 'beyondmeetings.audio.base'`

- [ ] **Step 3: Move both helpers into `base.py`**

In `src/beyondmeetings/audio/base.py`, add to the imports at the top:

```python
import re
import subprocess
```

Then add above `class Recorder(ABC):`:

```python
class SubprocessRunner:
    """The default way backends shell out. Injected so tests can record calls."""

    def run(self, args: list[str]) -> str:
        return subprocess.run(
            args, capture_output=True, text=True, check=False
        ).stdout.strip()

    def spawn(self, args: list[str]) -> int:
        return subprocess.Popen(args).pid


def build_filename_base(name: str, day: str, clock: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-")).strip("-")
    return f"{day}_{clock}_{slug or 'meeting'}"
```

- [ ] **Step 4: Delete both from `pipewire.py` and import them instead**

In `src/beyondmeetings/audio/pipewire.py`, delete the `SubprocessRunner` class and the `build_filename_base` function (lines 22-33), and delete the now-unused `import subprocess` line. Change the `from .base import ...` line to:

```python
from .base import (
    Recorder,
    RecordingState,
    SubprocessRunner,
    build_filename_base,
    clear_state,
    load_state,
    save_state,
)
```

Keep `import re` — it is still used by `PipeWireRecorder.start` for the default-source regex.

- [ ] **Step 5: Update the test that imported from `pipewire`**

In `tests/test_audio_pipewire.py`, change line 3 from:

```python
from beyondmeetings.audio.pipewire import PipeWireRecorder, build_filename_base
```

to:

```python
from beyondmeetings.audio.base import build_filename_base
from beyondmeetings.audio.pipewire import PipeWireRecorder
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 579 passed.

- [ ] **Step 7: Commit**

```bash
git add src/beyondmeetings/audio/ tests/
git commit -m "refactor: move platform-neutral audio helpers into base

Neither SubprocessRunner nor build_filename_base is Linux-specific, and a
second backend would otherwise have to import them from pipewire."
```

---

## Task 3: Add the platform dispatch point

`PipeWireRecorder` is constructed in exactly two places. Both become calls to a
factory, mirroring `transcribe/factory.py` and `llm/factory.py`.

The factory takes an explicit `platform` argument defaulting to `sys.platform`,
so tests select a branch by passing a value rather than monkeypatching a global.

On macOS today this raises a clear, actionable error instead of the current
behaviour — `pactl` not found, surfacing as a confusing mid-start failure.

**Files:**
- Create: `src/beyondmeetings/audio/factory.py`
- Modify: `src/beyondmeetings/cli.py:13`, `src/beyondmeetings/cli.py:96`
- Modify: `src/beyondmeetings/server.py:84-91`
- Test: `tests/test_audio_factory.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_audio_factory.py`:

```python
"""Backend selection per platform.

`platform` is a parameter rather than a monkeypatched sys.platform so each
branch is selected explicitly, and so a test cannot leak a patched global into
the next one.
"""
import pytest

from beyondmeetings.audio.factory import UnsupportedPlatformError, build_recorder
from beyondmeetings.audio.pipewire import PipeWireRecorder


def test_linux_gets_the_pipewire_backend(tmp_path):
    built = build_recorder(tmp_path, platform="linux")
    assert isinstance(built, PipeWireRecorder)


def test_segment_minutes_reaches_the_backend(tmp_path):
    built = build_recorder(tmp_path, segment_minutes=7, platform="linux")
    assert built.segment_minutes == 7


def test_macos_is_rejected_with_an_actionable_message(tmp_path):
    """Better than the status quo, which is `pactl` not found mid-start."""
    with pytest.raises(UnsupportedPlatformError, match="macOS"):
        build_recorder(tmp_path, platform="darwin")


def test_an_unknown_platform_is_rejected_rather_than_guessed(tmp_path):
    with pytest.raises(UnsupportedPlatformError, match="win32"):
        build_recorder(tmp_path, platform="win32")


def test_importing_the_factory_does_not_import_a_backend():
    """Backends are imported inside their branch.

    A macOS backend must never be imported on Linux, and vice versa — the
    import itself must stay free of platform-specific cost.
    """
    import subprocess
    import sys

    code = (
        "import sys; import beyondmeetings.audio.factory; "
        "print('beyondmeetings.audio.pipewire' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_audio_factory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'beyondmeetings.audio.factory'`

- [ ] **Step 3: Write the factory**

Create `src/beyondmeetings/audio/factory.py`:

```python
"""Choose the capture backend for the running platform.

Backends are imported inside their branch: importing the factory must not drag
in a platform's dependencies on a machine that cannot use them.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .base import Recorder

SPEC = "docs/superpowers/specs/2026-08-10-macos-support-design.md"


class UnsupportedPlatformError(RuntimeError):
    """No capture backend exists for this operating system."""


def build_recorder(
    data_dir: Path,
    segment_minutes: int = 50,
    platform: str | None = None,
) -> Recorder:
    platform = platform if platform is not None else sys.platform

    if platform.startswith("linux"):
        from .pipewire import PipeWireRecorder

        return PipeWireRecorder(data_dir, segment_minutes=segment_minutes)

    if platform == "darwin":
        raise UnsupportedPlatformError(
            "beyondMeetings cannot record on macOS yet — recording needs "
            "PipeWire, which is Linux-only. macOS support is being built; "
            f"see {SPEC}."
        )

    raise UnsupportedPlatformError(
        f"beyondMeetings has no capture backend for {platform}. Recording "
        "currently requires Linux with PipeWire."
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_audio_factory.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 5: Wire the CLI to the factory**

In `src/beyondmeetings/cli.py`, replace line 13:

```python
from .audio.pipewire import PipeWireRecorder
```

with:

```python
from .audio.factory import build_recorder
```

and in `_session` (line 96), replace:

```python
        recorder=PipeWireRecorder(data_dir, segment_minutes=config.segment_minutes),
```

with:

```python
        recorder=build_recorder(data_dir, segment_minutes=config.segment_minutes),
```

- [ ] **Step 6: Wire the server to the factory**

In `src/beyondmeetings/server.py`, replace lines 84-91:

```python
            from .audio.pipewire import PipeWireRecorder
            from .transcribe.factory import build_transcriber

            cfg = state["config"]
            state["session"] = SessionManager(
                config=cfg,
                recorder=PipeWireRecorder(
                    Path(cfg.data_dir), segment_minutes=cfg.segment_minutes
                ),
```

with:

```python
            from .audio.factory import build_recorder
            from .transcribe.factory import build_transcriber

            cfg = state["config"]
            state["session"] = SessionManager(
                config=cfg,
                recorder=build_recorder(
                    Path(cfg.data_dir), segment_minutes=cfg.segment_minutes
                ),
```

- [ ] **Step 7: Verify no direct construction remains**

Run: `grep -rn "PipeWireRecorder(" --include=*.py src/`
Expected: exactly one line — `src/beyondmeetings/audio/factory.py`.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 584 passed.

- [ ] **Step 9: Commit**

```bash
git add src/beyondmeetings/audio/factory.py src/beyondmeetings/cli.py src/beyondmeetings/server.py tests/test_audio_factory.py
git commit -m "feat: select the capture backend through a factory

Mirrors transcribe/factory.py and llm/factory.py. macOS now fails with a
message naming the platform instead of a confusing 'pactl not found'."
```

---

## Task 4: Split `desktop.py` into a package

`desktop.py` mixes platform-neutral logic (server lifecycle, browser opening)
with the freedesktop `.desktop` template. A macOS launcher writes an `.app`
bundle instead — different enough that branching inside shared functions would
be worse than separate modules.

This is a pure move: no behaviour changes, and `__init__.py` re-exports every
public name so the four importing modules are untouched.

**The trap in this task:** `ASSETS = Path(__file__).parent / "assets"` resolves
relative to the module file. Once the module becomes a package, `__file__` is
one directory deeper and the path silently points at a directory that does not
exist. It must become `.parent.parent`. The test in Step 1 exists to catch this.

**Files:**
- Create: `src/beyondmeetings/desktop/__init__.py`, `src/beyondmeetings/desktop/base.py`, `src/beyondmeetings/desktop/linux.py`
- Delete: `src/beyondmeetings/desktop.py`
- Test: `tests/test_desktop.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_desktop.py`:

```python
# --- the module became a package so a macOS launcher can sit beside Linux ---

def test_the_icon_asset_still_resolves_after_the_package_split():
    """`Path(__file__).parent / "assets"` is one level too shallow in a package.

    It fails silently: install_desktop_entry copies from a path that does not
    exist and raises only at install time, which no unit test reaches.
    """
    from beyondmeetings.desktop.linux import ASSETS

    assert (ASSETS / "icon.svg").is_file(), f"icon.svg not found under {ASSETS}"


def test_public_names_are_still_importable_from_the_package():
    """cli.py, tray.py and doctor/desktop.py import these paths today."""
    from beyondmeetings.desktop import (  # noqa: F401
        DEFAULT_PORT,
        desktop_entry_path,
        icon_install_path,
        install_desktop_entry,
        open_app,
        open_browser,
        open_browser_when_ready,
        remove_desktop_entry,
        wait_until,
    )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_desktop.py -k "package_split or still_importable" -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'beyondmeetings.desktop.linux'; 'beyondmeetings.desktop' is not a package`

- [ ] **Step 3: Create the package directory and move the file**

```bash
mkdir -p src/beyondmeetings/desktop
git mv src/beyondmeetings/desktop.py src/beyondmeetings/desktop/base.py
```

- [ ] **Step 4: Move the Linux-specific half into `linux.py`**

Create `src/beyondmeetings/desktop/linux.py`:

```python
"""The freedesktop application entry — Linux desktops only."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .base import APP_ID, resolve_executable

# .parent.parent, not .parent: this module sits one level inside the package,
# and the assets directory belongs to beyondmeetings/, not beyondmeetings/desktop/.
ASSETS = Path(__file__).parent.parent / "assets"

# Categories deliberately lists ONE main category: several makes the app
# appear multiple times in the applications menu.
DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=beyondMeetings
GenericName=Meeting Recorder
Comment=Record a meeting and get structured notes in Obsidian
Exec={exec_path} open
Icon={app_id}
Terminal=false
Categories=Office;
Keywords=meeting;recording;transcription;notes;obsidian;
StartupNotify=true
StartupWMClass=beyondmeetings
"""


def desktop_entry_path(home: Path | None = None) -> Path:
    home = Path(home or Path.home())
    return home / ".local" / "share" / "applications" / f"{APP_ID}.desktop"


def icon_install_path(home: Path | None = None) -> Path:
    home = Path(home or Path.home())
    return (
        home / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps"
        / f"{APP_ID}.svg"
    )


def install_desktop_entry(home: Path | None = None) -> Path:
    """Put the icon in the Ubuntu app grid."""
    icon_target = icon_install_path(home)
    icon_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ASSETS / "icon.svg", icon_target)

    entry = desktop_entry_path(home)
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        DESKTOP_ENTRY.format(exec_path=resolve_executable(), app_id=APP_ID),
        encoding="utf-8",
    )
    os.chmod(entry, 0o755)

    # Without this the launcher can take minutes to show up in the app grid.
    for command, args in (
        ("update-desktop-database", [str(entry.parent)]),
        ("gtk-update-icon-cache", ["-f", "-t", str(icon_target.parents[2])]),
    ):
        binary = shutil.which(command)
        if binary:
            subprocess.run([binary, *args], capture_output=True, check=False)

    return entry


def remove_desktop_entry(home: Path | None = None) -> None:
    desktop_entry_path(home).unlink(missing_ok=True)
    icon_install_path(home).unlink(missing_ok=True)
```

- [ ] **Step 5: Delete the moved half from `base.py`**

From `src/beyondmeetings/desktop/base.py`, delete: the `ASSETS` assignment, the `DESKTOP_ENTRY` template, and the functions `desktop_entry_path`, `icon_install_path`, `install_desktop_entry`, `remove_desktop_entry`.

Then delete the imports that only those used — `os` and `shutil`. Keep `socket`, `subprocess`, `sys`, `threading`, `time`, `webbrowser` and `Path`, which the remaining functions still need.

- [ ] **Step 6: Write the re-export shim**

Create `src/beyondmeetings/desktop/__init__.py`:

```python
"""Desktop integration.

Split by platform: base.py is platform-neutral, linux.py writes the
freedesktop entry. Every public name is re-exported here so importers do not
need to know which module a name lives in.
"""
from .base import (
    APP_ID,
    DEFAULT_PORT,
    POLL_INTERVAL,
    STARTUP_TIMEOUT,
    launch_server,
    open_app,
    open_browser,
    open_browser_when_ready,
    resolve_executable,
    server_is_running,
    wait_for_server,
    wait_until,
)
from .linux import (
    DESKTOP_ENTRY,
    desktop_entry_path,
    icon_install_path,
    install_desktop_entry,
    remove_desktop_entry,
)

__all__ = [
    "APP_ID",
    "DEFAULT_PORT",
    "DESKTOP_ENTRY",
    "POLL_INTERVAL",
    "STARTUP_TIMEOUT",
    "desktop_entry_path",
    "icon_install_path",
    "install_desktop_entry",
    "launch_server",
    "open_app",
    "open_browser",
    "open_browser_when_ready",
    "remove_desktop_entry",
    "resolve_executable",
    "server_is_running",
    "wait_for_server",
    "wait_until",
]
```

- [ ] **Step 7: Repoint the five monkeypatches that target the old module path**

`tests/test_desktop.py` patches `server_is_running` by module path on lines
**15, 28, 42, 52 and 60**. After the split that string resolves to the
re-export in `__init__.py`, while `open_app` resolves `server_is_running` from
its own module — so the patch would apply to a name nobody calls and the tests
would hit the real network path.

Patch the defining module instead. Replace all five occurrences of:

```python
monkeypatch.setattr("beyondmeetings.desktop.server_is_running", ...)
```

with:

```python
monkeypatch.setattr("beyondmeetings.desktop.base.server_is_running", ...)
```

keeping each lambda exactly as it is (`lambda p=0: True` on line 15, `lambda p=0: False` on the other four). Mechanically:

```bash
sed -i 's/beyondmeetings\.desktop\.server_is_running/beyondmeetings.desktop.base.server_is_running/g' tests/test_desktop.py
grep -c "beyondmeetings.desktop.base.server_is_running" tests/test_desktop.py   # expect 5
```

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 586 passed.

Any remaining failure naming a `beyondmeetings.desktop.<name>` attribute is the
same class of problem: patch the module that *defines* the name, not the
package that re-exports it.

- [ ] **Step 9: Verify the installer's icon path still works end to end**

The unit tests never execute `install_desktop_entry`, so run it for real
against a scratch home:

```bash
.venv/bin/python -c "
import tempfile, pathlib
from beyondmeetings.desktop import install_desktop_entry, icon_install_path
home = pathlib.Path(tempfile.mkdtemp())
entry = install_desktop_entry(home)
assert entry.is_file(), 'no .desktop entry written'
assert icon_install_path(home).is_file(), 'icon.svg was not copied'
print('installed OK:', entry)
"
```

Expected: `installed OK: /tmp/.../beyondmeetings.desktop`

- [ ] **Step 10: Commit**

```bash
git add -A src/beyondmeetings/desktop src/beyondmeetings/desktop.py tests/test_desktop.py
git commit -m "refactor: split desktop.py into a package

Platform-neutral server/browser logic in base.py, the freedesktop entry in
linux.py, so a macOS .app launcher can sit beside it rather than branching
inside shared functions. __init__ re-exports every public name, so importers
are untouched.

ASSETS needed .parent.parent once the module became a package — it would have
failed only at install time, which no unit test reaches."
```

---

## Task 5 (GATED): the macOS recorder

> **Do not start this task until the Phase 1 spike has been run and its answers
> recorded in the spec.** The spike can invalidate the helper's command-line
> interface, and every assertion below encodes that interface.

Once unblocked, this task is fully executable on Linux: `MacRecorder` shells out
to `bmcapture` and `ffmpeg`, both through the injected runner, so the existing
`FakeRunner` pattern from `tests/test_audio_pipewire.py` drives all of it.

**Files:**
- Create: `src/beyondmeetings/audio/macos.py`
- Modify: `src/beyondmeetings/audio/factory.py` — replace the `darwin` raise with a `MacRecorder` construction
- Test: `tests/test_audio_macos.py` (create)

**What the tests must pin down**, each a distinct failure the spec calls out:

1. `start()` spawns `bmcapture record --system <base>_seg000.system.wav --mic <base>_seg000.mic.wav`.
2. `roll_segment()` kills the helper, **spawns the next segment before invoking ffmpeg**, then mixes — asserted by the *order* of recorded commands, because mixing first would extend the audio gap by however long ffmpeg takes.
3. The mix is `ffmpeg -i <system> -i <mic> -filter_complex amix=inputs=2 <final>`, and `RecordingState.segments` receives the single mixed path, never the two intermediates.
4. Both intermediates are deleted after a successful mix, and **kept** if ffmpeg fails — losing the only copy of a meeting to a mix failure is unacceptable.
5. `stop()` kills the helper and mixes the final segment.
6. `status()` returns `None` on a corrupt state file and populates `state_error`, matching `PipeWireRecorder`'s contract.
7. `MacRecorder` satisfies the `Recorder` ABC — add `"darwin"` to a parametrisation in `tests/test_audio_interface.py`.

The factory change and its test:

```python
def test_macos_gets_the_mac_backend(tmp_path):
    from beyondmeetings.audio.macos import MacRecorder

    assert isinstance(build_recorder(tmp_path, platform="darwin"), MacRecorder)
```

`tests/test_audio_factory.py::test_macos_is_rejected_with_an_actionable_message`
is deleted in the same commit — the behaviour it pins is intentionally replaced.

---

## Deferred to a second plan

Spec phases 3, 5 and 6 — the `bmcapture` Swift helper, the `.app` bundle,
`install.sh`'s Darwin branch, the doctor's permission checks, and the docs.

They are not planned here because each depends on the spike's answers, and none
can be given verifiable steps from Linux: the Swift cannot be compiled, and TCC
attribution cannot be observed. Writing "expected: PASS" against either would be
inventing confidence. Once the spike returns, that plan can be written properly.

---

## Self-review

**Spec coverage.** Phase 2 → Tasks 1–4. Phase 4 → Task 5 (gated). Phase 1 →
documented as a precondition with explicit questions, correctly not TDD.
Phases 3/5/6 → explicitly deferred, with the reason. The spec's "import guard"
requirement is Task 3 Step 1's last test; the "spawn-before-mix ordering"
requirement is Task 5's assertion 2; the ABC gap is Task 1.

**Placeholders.** None. Every code step carries the code, every command its
expected output. Task 5 states the assertions rather than final code because
its interface is gated — flagged as such rather than left vague.

**Type consistency.** `build_recorder(data_dir, segment_minutes, platform)` and
`UnsupportedPlatformError` are used identically in Tasks 3 and 5.
`build_filename_base` and `SubprocessRunner` keep their signatures across the
Task 2 move. `ASSETS`, `APP_ID` and `resolve_executable` keep their names across
the Task 4 split.

**Test counts** (569 → 577 → 579 → 584 → 586) assume the suite is green at
`913def6` and that no other work lands in between. If a count is off, check
what else changed before assuming the task is wrong.
