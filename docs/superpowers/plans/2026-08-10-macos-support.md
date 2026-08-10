# macOS Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the single seam macOS support needs, and prove Linux still behaves exactly as it does today.

**Architecture:** macOS is additive. Every macOS-specific module is a new file Linux never imports. The one unavoidable shared change is a factory that chooses the capture backend — four lines across `cli.py` and `server.py`, returning the same `PipeWireRecorder` with the same arguments on Linux.

**Tech Stack:** Python 3.10+, pytest, pydantic v2. No new dependencies.

---

## The governing constraint

**The Linux application must work exactly as it does today.** This outranks
every other goal here.

Concretely, at the end of this plan:

- `src/beyondmeetings/audio/pipewire.py` is **unmodified**;
- `src/beyondmeetings/desktop.py` is **unmodified**;
- all 569 tests that passed at `913def6` still pass, **unmodified**;
- the only Linux-path changes are 4 lines (imports and constructions in
  `cli.py` and `server.py`) plus one function relocation with a re-export.

Task 3 exists purely to make those claims testable rather than asserted.

---

## Scope

Covers **Phase 2** of `docs/superpowers/specs/2026-08-10-macos-support-design.md`.

**Already landed** (commit `80708b2`, on this branch): the `Recorder` ABC now
declares `roll_segment`, `reset` and `state_error`, and `session.py`'s
defensive `getattr` is gone. Zero behaviour change; 577 tests green. That
commit also fixed `test_session.py`'s `FakeRecorder`, which had silently
drifted from the interface — the bug the ABC change was meant to surface.

**This plan: Tasks 1–3.** No Mac required. Linux behaviour unchanged.

**Explicitly dropped from the earlier revision:**

- *Splitting `desktop.py` into a package* — restructured working Linux code,
  freshly fixed in PR #2, for a macOS launcher that does not exist. macOS gets
  a separate `desktop_macos.py` instead.
- *Moving `SubprocessRunner` to `base.py`* — `audio/macos.py` will define its
  own eight-line runner rather than have a macOS backend import from the Linux
  one. Only `build_filename_base` moves, because the filename convention is
  shared core that the whole pipeline depends on.

**Not planned here:** spec phases 3–6 (the Swift helper, `.app` bundle,
installer branch, doctor checks, docs) and phase 4 (`MacRecorder`). All are
new-file-only work, and all are gated on the phase 1 spike, whose outcome can
still change the helper's interface. They get their own plan once it returns.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/beyondmeetings/audio/base.py` | `Recorder` ABC, `RecordingState`, state I/O, shared naming | Modify — receives `build_filename_base` |
| `src/beyondmeetings/audio/pipewire.py` | Linux capture | Modify — `build_filename_base` moves out, re-export left behind |
| `src/beyondmeetings/audio/factory.py` | Chooses a backend per platform | **Create** |
| `src/beyondmeetings/cli.py` | CLI entry point | Modify — 2 lines |
| `src/beyondmeetings/server.py` | HTTP app | Modify — 2 lines |
| `src/beyondmeetings/desktop.py` | Linux launcher | **Unmodified** |
| `tests/test_audio_factory.py` | Dispatch behaviour | **Create** |
| `tests/test_linux_unaffected.py` | Regression guards for the constraint | **Create** |

---

## Task 1: Move `build_filename_base` to the shared core

`audio/macos.py` will need the `YYYY-MM-DD_HH-MM_slug` convention. It is shared
core — the stop script, transcript paths, processed-audio paths and Obsidian
note names all depend on it — not Linux capture logic.

A re-export stays in `pipewire.py` so every existing import keeps resolving.
`SubprocessRunner` does **not** move.

**Files:**
- Modify: `src/beyondmeetings/audio/base.py`
- Modify: `src/beyondmeetings/audio/pipewire.py:11-33`
- Test: `tests/test_audio_interface.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_audio_interface.py`:

```python
# --- the filename convention is shared core, not Linux capture logic ---

def test_filename_base_is_importable_from_base():
    from beyondmeetings.audio.base import build_filename_base

    assert build_filename_base("Client Kickoff!", "2026-07-30", "14-30") == (
        "2026-07-30_14-30_client-kickoff"
    )


def test_filename_base_is_still_importable_from_pipewire():
    """Moving it must not break an existing import path."""
    from beyondmeetings.audio.base import build_filename_base as from_base
    from beyondmeetings.audio.pipewire import build_filename_base as from_pipewire

    assert from_pipewire is from_base
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_audio_interface.py -k filename_base -v`

Expected: FAIL — `ImportError: cannot import name 'build_filename_base' from 'beyondmeetings.audio.base'`

- [ ] **Step 3: Add the function to `base.py`**

In `src/beyondmeetings/audio/base.py`, add `import re` to the imports, then add
this immediately above `class Recorder(ABC):`:

```python
def build_filename_base(name: str, day: str, clock: str) -> str:
    """The `YYYY-MM-DD_HH-MM_slug` convention every backend and path derives from."""
    slug = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-")).strip("-")
    return f"{day}_{clock}_{slug or 'meeting'}"
```

- [ ] **Step 4: Replace the definition in `pipewire.py` with a re-export**

In `src/beyondmeetings/audio/pipewire.py`, delete this function:

```python
def build_filename_base(name: str, day: str, clock: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-")).strip("-")
    return f"{day}_{clock}_{slug or 'meeting'}"
```

and change the existing `from .base import ...` line to include it, so the name
still resolves for anything importing it from here:

```python
from .base import (
    Recorder,
    RecordingState,
    build_filename_base,
    clear_state,
    load_state,
    save_state,
)
```

Keep `import re` — `PipeWireRecorder.start` still uses it for the
`Default Source:` regex. Keep `SubprocessRunner` exactly where it is.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 579 passed.

- [ ] **Step 6: Confirm the Linux backend is otherwise untouched**

Run: `git diff --stat src/beyondmeetings/audio/pipewire.py`
Expected: a small diff touching only the import block and the deleted function — roughly `1 file changed, 8 insertions(+), 6 deletions(-)`. If any line inside `PipeWireRecorder` changed, revert and redo the step.

- [ ] **Step 7: Commit**

```bash
git add src/beyondmeetings/audio/base.py src/beyondmeetings/audio/pipewire.py tests/test_audio_interface.py
git commit -m "refactor: build_filename_base belongs to the shared core

The YYYY-MM-DD_HH-MM_slug convention is depended on by transcript paths,
processed audio, and Obsidian note names — it is not Linux capture logic. A
re-export stays in pipewire so no existing import path breaks."
```

---

## Task 2: Add the platform dispatch point

The only shared change macOS support requires. On Linux it returns the same
`PipeWireRecorder`, constructed with the same arguments, as the direct call it
replaces.

`platform` is an explicit argument defaulting to `sys.platform`, so tests
select a branch by passing a value instead of monkeypatching a global.

Backends are imported *inside* their branch — this is what guarantees a Linux
machine never loads macOS code.

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
    assert isinstance(build_recorder(tmp_path, platform="linux"), PipeWireRecorder)


def test_the_linux_backend_is_built_exactly_as_before(tmp_path):
    """The constraint: going through the factory must change nothing on Linux."""
    built = build_recorder(tmp_path, segment_minutes=7, platform="linux")
    direct = PipeWireRecorder(tmp_path, segment_minutes=7)

    assert built.data_dir == direct.data_dir
    assert built.segment_minutes == direct.segment_minutes
    assert built.state_path == direct.state_path


def test_macos_is_rejected_until_its_backend_lands(tmp_path):
    """Clearer than the status quo, which is `pactl` not found mid-start."""
    with pytest.raises(UnsupportedPlatformError, match="macOS"):
        build_recorder(tmp_path, platform="darwin")


def test_an_unknown_platform_is_rejected_rather_than_guessed(tmp_path):
    with pytest.raises(UnsupportedPlatformError, match="win32"):
        build_recorder(tmp_path, platform="win32")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_audio_factory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'beyondmeetings.audio.factory'`

- [ ] **Step 3: Write the factory**

Create `src/beyondmeetings/audio/factory.py`:

```python
"""Choose the capture backend for the running platform.

Backends are imported inside their branch, never at module scope: a Linux
machine must not load macOS code, and vice versa.
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
Expected: PASS — 4 tests.

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

In `src/beyondmeetings/server.py`, replace:

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
Expected: PASS — 583 passed.

- [ ] **Step 9: Confirm the Linux footprint is four lines**

Run: `git diff --stat src/beyondmeetings/cli.py src/beyondmeetings/server.py`
Expected: `2 files changed, 4 insertions(+), 4 deletions(-)`

- [ ] **Step 10: Commit**

```bash
git add src/beyondmeetings/audio/factory.py src/beyondmeetings/cli.py src/beyondmeetings/server.py tests/test_audio_factory.py
git commit -m "feat: select the capture backend through a factory

The one shared change macOS support needs. On Linux it returns the same
PipeWireRecorder with the same arguments as the direct construction it
replaces — four lines, no behaviour change.

Backends are imported inside their branch, so a Linux machine never loads
macOS code."
```

---

## Task 3: Pin the constraint with regression guards

Tasks 1 and 2 *claim* Linux is unaffected. These tests make the claim fail
loudly if it ever stops being true — including from work nobody has written yet.

**Files:**
- Test: `tests/test_linux_unaffected.py` (create)

- [ ] **Step 1: Write the guards**

Create `tests/test_linux_unaffected.py`:

```python
"""macOS support must not change how the Linux application behaves.

These tests exist solely to enforce that. If one fails, a macOS change has
leaked into a Linux path — fix the leak rather than the test.
"""
import subprocess
import sys
from pathlib import Path

from beyondmeetings.audio.pipewire import PipeWireRecorder

# Repo root, matching the convention in tests/test_packaging.py. A relative
# path would resolve against the caller's cwd, not the repo.
ROOT = Path(__file__).resolve().parents[1]


def _modules_after(statement: str) -> set[str]:
    """Module names loaded by `statement` in a fresh interpreter."""
    code = f"{statement}; import sys; print('\\n'.join(sorted(sys.modules)))"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return set(out.stdout.split())


def test_the_cli_loads_no_macos_module():
    loaded = _modules_after("import beyondmeetings.cli")
    mac = {m for m in loaded if "macos" in m or "darwin" in m}
    assert not mac, f"Linux CLI imported macOS modules: {sorted(mac)}"


def test_the_factory_imports_no_backend_at_module_scope():
    """Backends belong inside their branch, so no platform pays for another."""
    loaded = _modules_after("import beyondmeetings.audio.factory")
    assert "beyondmeetings.audio.pipewire" not in loaded


def test_the_linux_capture_backend_is_reachable_by_its_original_path():
    """Anything importing PipeWireRecorder directly still works."""
    assert PipeWireRecorder is not None


def test_the_filename_convention_survives_the_move(tmp_path):
    from beyondmeetings.audio.base import build_filename_base

    assert build_filename_base("Client Kickoff!", "2026-07-30", "14-30") == (
        "2026-07-30_14-30_client-kickoff"
    )
    assert build_filename_base("!!!", "2026-07-30", "14-30") == (
        "2026-07-30_14-30_meeting"
    )


def test_the_linux_launcher_module_is_untouched():
    """desktop.py was deliberately not restructured for macOS.

    macOS packaging goes in a separate desktop_macos.py. If this fails, that
    decision was reversed without updating the spec.
    """
    source = ROOT / "src" / "beyondmeetings" / "desktop.py"
    assert source.is_file(), "desktop.py must remain a module, not become a package"
    assert not (ROOT / "src" / "beyondmeetings" / "desktop").exists()
```

- [ ] **Step 2: Run the guards**

Run: `.venv/bin/python -m pytest tests/test_linux_unaffected.py -v`
Expected: PASS — 5 tests.

If `test_the_factory_imports_no_backend_at_module_scope` fails, the factory has
a top-level backend import; move it inside its branch.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 588 passed.

- [ ] **Step 4: Prove the pre-existing suite is untouched**

The strongest evidence for the constraint: every test that passed before this
work still passes, unmodified.

```bash
git diff main --stat -- tests/ | tail -3
```

Expected: only `tests/test_audio_interface.py`, `tests/test_audio_factory.py`,
`tests/test_linux_unaffected.py` (new) and `tests/test_session.py` (the
`FakeRecorder` interface fix from `80708b2`). **No other pre-existing test file
may appear.** If one does, a Linux behaviour changed and the test was bent to
match — stop and investigate.

- [ ] **Step 5: Commit**

```bash
git add tests/test_linux_unaffected.py
git commit -m "test: guard that macOS work leaves Linux behaviour alone

Tasks 1 and 2 claim Linux is unaffected; these make the claim fail loudly if
it stops being true, including from work not yet written."
```

---

## Self-review

**Spec coverage.** Governing constraint → Task 3's guards. Phase 2 "the seam" →
Tasks 1 and 2. ABC tightening → already landed in `80708b2`. The spec's
rejected `desktop.py` split → guarded by
`test_the_linux_launcher_module_is_untouched`.

**Placeholders.** None. Every code step carries its code; every command its
expected output.

**Type consistency.** `build_recorder(data_dir, segment_minutes, platform)` and
`UnsupportedPlatformError` are used identically in Tasks 2 and 3.
`build_filename_base` keeps its signature across the Task 1 move.

**Test counts** (577 → 579 → 583 → 588) assume the suite is green at `80708b2`
and that nothing else lands in between. A mismatch means something else
changed — check before assuming a task is wrong.
