# macOS support — design

**Date:** 2026-08-10
**Status:** approved, not yet implemented
**Scope:** bring beyondMeetings to macOS 13+ with system-audio capture, without asking the user to reroute their audio.

---

## Why this is not a small port

Everything above the audio layer is already platform-neutral: transcription, LLM
providers, note generation, the task board, vault writing, the FastAPI server
and web UI all use `pathlib` and shell out to nothing Linux-specific.

The port is hard for exactly two reasons:

1. **macOS has no `pactl`.** The Linux recorder builds a PipeWire null sink,
   loops every monitor source plus the default mic into it, and records the
   mix. That primitive does not exist on macOS in any form.
2. **macOS gates audio behind TCC**, and TCC grants are keyed per *bundle
   identifier*. A pip-installed CLI has no bundle identity, so grants attribute
   to the launching terminal instead of to beyondMeetings — and a grant made
   from Terminal does not apply when the same code is launched from an app
   icon.

The second is the reason this design includes an `.app` bundle. It is not
cosmetic packaging; it is what makes the permission attach to us and persist.

---

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Minimum macOS | **13.0 (Ventura)** | `SCStreamConfiguration.capturesAudio` lands in 13.0. CoreAudio process taps would be cleaner but are 14.4+. |
| Capture strategy | **ScreenCaptureKit + AVFoundation, native helper** | The user never touches Audio MIDI Setup or changes output device. Cost accepted: native code and a Screen Recording prompt. |
| Helper distribution | **Compile at install time via Xcode CLT** | No Apple Developer account, no notarization, no Gatekeeper quarantine. Cost: one-time `xcode-select --install`. |
| Mixing | **Two WAVs per segment, mixed by ffmpeg** | ffmpeg is already a hard dependency; keeps the Swift helper small. |
| Data paths | **Unchanged — `~/.local/share/beyondmeetings`** | Per-platform paths would mean a migration story across secrets/vault/MCP for no user-visible gain. |

### Rejected alternatives

- **pyobjc-framework-ScreenCaptureKit** (pure pip, no compiler). Rejected:
  [pyobjc#647](https://github.com/ronaldoussoren/pyobjc/issues/647) reports
  consistent failure — no callbacks — for precisely our case, capturing system
  audio without external tools. The core capture path cannot rest on that.
- **BlackHole virtual device.** Works on any macOS version with zero native
  code, but requires the user to install a driver, build a Multi-Output Device
  in Audio MIDI Setup, and switch their system output to it during calls.
  Ruled out by the no-manual-routing requirement.
- **Prebuilt signed + notarized binary.** Best end-user experience, but needs a
  paid Apple Developer account and a notarization step in the release process.
  Deferred, not rejected — see Future work.
- **Mic-only first release.** Misses every remote participant on headphones,
  which is the product's whole point.

---

## Capture design

A single Swift helper, `bmcapture` (~250 lines), owns both audio sources —
because at our floor they cannot come from one API:

| Source | API | Availability |
|---|---|---|
| System audio (remote participants) | `SCStream` with `capturesAudio = true` | macOS 13.0+ |
| Microphone (local voice) | `AVCaptureSession` audio input | `SCStreamConfiguration.captureMicrophone` is 15.0+, unusable here |

System audio arrives under the Screen Recording grant alone; no separate audio
permission exists for it. The microphone is a distinct TCC grant.

### Helper interface

Shaped to match what the Linux recorder already does, so the Python side barely
changes:

```
bmcapture record --system <path.wav> --mic <path.wav>   # runs until SIGTERM
bmcapture check-permissions --json                       # {"screen_recording":bool,"microphone":bool}
bmcapture list-devices --json
```

`stop()` and `roll_segment()` on Linux work by killing the capture PID.
`bmcapture` traps SIGTERM and finalizes WAV headers cleanly, so that logic
carries over unchanged.

### Mixing, and the drift trade-off

The helper writes two files per segment; ffmpeg `amix` combines them when the
segment closes. The mic and system audio run off different hardware clocks, so
they can drift apart within a segment.

Accepted deliberately:

- each segment (≤50 min) mixes independently, so drift cannot accumulate across
  a multi-hour meeting;
- the output feeds Whisper, not a listener — a few hundred ms of relative drift
  leaves every word intelligible;
- the helper stays small enough to audit.

If drift ever proves audible, moving to in-process mixing changes only the
helper, not the Python side.

---

## Python integration

### Factory dispatch

`transcribe/` and `llm/` already dispatch through a `factory.py`; audio does not,
because there has only ever been one implementation. New `audio/factory.py`:

```python
def build_recorder(data_dir: Path, segment_minutes: int) -> Recorder:
    if sys.platform == "darwin":
        return MacRecorder(data_dir, segment_minutes=segment_minutes)
    return PipeWireRecorder(data_dir, segment_minutes=segment_minutes)
```

This replaces the only two places that name a recorder today: `cli.py:87` and
`server.py:88`.

`audio/factory.py` must not import macOS-only modules at module scope — a
Linux import of the factory has to stay clean.

### Tightening the `Recorder` ABC

A real gap this port exposes, not a refactor for its own sake: `RolloverWorker`
calls `recorder.roll_segment()`, and the server calls `recorder.reset()` and
reads `recorder.state_error` — none of which is declared on the ABC. With one
implementation that is invisible; with two, an omitted method becomes a runtime
failure mid-meeting. All three move onto the interface.

### `MacRecorder`

Takes the same injected `runner` as `PipeWireRecorder`, so it is driven by the
existing `FakeRunner` pattern and the entire Python side is testable on Linux.

Segment rollover, with one ordering detail that matters:

```
roll_segment():  kill helper → spawn next segment immediately → then ffmpeg-mix the finished one
```

Mixing *after* respawning keeps the audio gap to the process-restart window
rather than extending it by however long ffmpeg takes.

Intermediates (`…_seg000.system.wav`, `…_seg000.mic.wav`) are derived by naming
convention and deleted after mixing, so `RecordingState.segments` still holds
exactly one final path per segment. The state model and everything downstream
are untouched.

`RecordingState.module_ids` is PipeWire-specific and stays empty on macOS.
Generalizing it would rewrite the on-disk state format for no functional gain;
it stays, documented as Linux-only.

---

## Packaging and permissions

```
~/Applications/beyondMeetings.app/Contents/
  Info.plist             CFBundleIdentifier, NSMicrophoneUsageDescription,
                         LSMinimumSystemVersion 13.0
  MacOS/beyondMeetings   launcher → execs the venv's `beyondmeetings open`
  MacOS/bmcapture        the Swift helper
  Resources/beyondmeetings.icns
```

`NSMicrophoneUsageDescription` is mandatory: without it the process is killed
outright on first mic access, not merely denied.

### `desktop.py` becomes a package

It currently mixes platform-neutral logic (`open_app`, `server_is_running`,
`resolve_executable`) with the freedesktop `.desktop` template. Split into
`desktop/{base,linux,macos}.py`, with `__init__.py` re-exporting the public
names so existing imports such as `from .desktop import DEFAULT_PORT, open_app`
keep working untouched.

> **Sequencing hazard.** As of 2026-08-10 there is uncommitted work in progress
> on `desktop.py` — `tests/test_desktop.py` contains a red-phase TDD suite for a
> refactor (`wait_until`, `ready`/`reporter`/`cancelled` parameters,
> `browser_check`) whose implementation is not yet written; 10 tests fail. That
> refactor must land before this split begins, or the two will conflict badly.

### Build

`bmcapture.swift` ships in the wheel under `src/beyondmeetings/native/`.
Hatchling already includes non-Python files this way for `web/`, so no
packaging change is needed.

`install.sh` gains a `uname -s` branch. On Darwin it verifies `xcode-select -p`,
builds with `swiftc -O … -framework ScreenCaptureKit -framework AVFoundation`,
and assembles the bundle. The uv fallback matters more here than on Linux —
macOS ships no system Python beyond what Xcode CLT provides.

### Doctor changes

- `PipeWireCheck` becomes Linux-only. macOS gets a check that shells
  `bmcapture check-permissions --json` and reports Screen Recording and
  Microphone separately, with a fix action that deep-links System Settings
  (`x-apple.systempreferences:…Privacy_ScreenCapture`). It must state that a
  Screen Recording grant requires an app restart to take effect — otherwise
  users grant it, see no change, and conclude it is broken.
- `install_hint`'s manager table gains `brew`.
- `ObsidianCheck` swaps flatpak for `/Applications/Obsidian.app` and
  `brew install --cask obsidian`.
- `AutostartCheck` swaps the freedesktop autostart entry for a
  `~/Library/LaunchAgents` plist.

---

## Open risk: TCC attribution

Two launch paths exist — Finder (inside the bundle) and `beyondmeetings start`
from a terminal. If TCC attributes the grant to the responsible *ancestor*
process, the terminal path will prompt for Terminal separately and the bundle's
grant will not apply to it.

**This cannot be determined without a Mac and is not settled by this document.**
Phase 1 is a spike that answers it. If it confirms the problem, the fallback is
to have the macOS CLI delegate `start`/`stop` to the bundle-launched server
rather than spawning the helper itself. That is deliberately not designed in
speculatively.

---

## Testing

**Testable on Linux, in this repo** — essentially the whole Python surface,
via the existing injected-runner seam:

- `MacRecorder`: segment naming, ffmpeg mix invocation, spawn-before-mix
  ordering, SIGTERM stop;
- a parametrized test asserting **both** recorders satisfy the full `Recorder`
  ABC — the test that would have caught the `roll_segment` gap;
- factory dispatch, with `sys.platform` monkeypatched both ways;
- doctor checks driven by a mocked `check-permissions` JSON payload;
- bundle generation into `tmp_path`, asserting `Info.plist` parses via
  `plistlib` and carries `NSMicrophoneUsageDescription`;
- an import guard: `audio.factory` on Linux must not pull in macOS-only modules.

**Requires CI on macOS** — a GitHub Actions `macos-14` runner compiles
`bmcapture` and confirms `check-permissions --json` runs and emits valid JSON.
That catches compile breakage and interface drift.

**Requires a human on a real Mac, and cannot be automated** — actual capture
behavior and TCC grants. CI runners are headless, with no audio and no way to
grant TCC. A green CI badge must not be read as covering this.

---

## Phases

| # | Phase | Where | Gate |
|---|---|---|---|
| 1 | **Spike** — throwaway Swift; answer TCC attribution, confirm SCK audio-only on 13 | Mac, manual | blocks everything |
| 2 | **Platform-neutral refactor** — ABC tightening, `audio/factory.py`, `desktop/` split | Linux, fully tested | needs the in-flight `desktop.py` work landed first |
| 3 | **Swift helper + build** — `bmcapture.swift`, `swiftc` in `install.sh`, CI job | Mac + CI | after 1 |
| 4 | **`MacRecorder`** — Python side | Linux, fully tested | after 2, 3 |
| 5 | **Packaging + doctor** — `.app` bundle, permission checks, brew, LaunchAgent | Linux-testable, Mac-verified | after 4 |
| 6 | **Docs** — README requirements table, macOS install steps | — | after 5 |

Phase 2 is worth landing regardless of whether macOS ever ships: it closes an
interface gap that exists today.

---

## Future work

- **Notarized release.** Removes the Xcode CLT requirement entirely. Needs a
  paid Apple Developer account and a signing/notarization release step.
- **CoreAudio process taps on 14.4+.** Drops the Screen Recording permission on
  newer systems; would run alongside the SCK path, selected at runtime.
- **`captureMicrophone` on 15.0+.** Collapses the two-source design into one
  stream and eliminates the drift trade-off entirely.
- **Windows.** WASAPI loopback is the natural equivalent and gives mixed system
  audio directly; the factory seam introduced in Phase 2 is what makes it
  tractable.

---

## References

- [Capturing screen content in macOS](https://developer.apple.com/documentation/ScreenCaptureKit/capturing-screen-content-in-macos) — `capturesAudio`, 13.0+
- [ScreenCaptureKit, Xcode 16 API diff](https://github.com/dotnet/macios/wiki/ScreenCaptureKit-macOS-xcode16.0-b1) — `captureMicrophone`, 15.0+
- [pyobjc#647](https://github.com/ronaldoussoren/pyobjc/issues/647) — system-audio capture failure
- [TCC grants keyed per bundle ID](https://docs.screenpi.pe/permissions)
