"""macOS capture via the bundled `bmcapture` helper.

macOS 13 and 14 cannot deliver system audio and the microphone from one API —
`SCStreamConfiguration.captureMicrophone` arrived in 15.0 — so the helper
records two streams and ffmpeg mixes them when a segment closes. Each segment
mixes independently, so clock drift between the two devices cannot accumulate
across a long meeting.

Linux never imports this module: `audio/factory.py` imports it inside its
`darwin` branch.
"""
from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from .base import (
    Recorder,
    RecordingState,
    build_filename_base,
    clear_state,
    load_state,
    save_state,
)

APP_BUNDLE = "beyondMeetings.app"
HELPER_NAME = "bmcapture"

# Mixed without -ac/-ar on purpose: compress_for_upload already produces mono
# 16 kHz for the transcriber, and resampling twice only loses quality.
MIX_FILTER = "amix=inputs=2:duration=longest:normalize=0"


def resolve_helper(home: Path | None = None) -> str:
    """Where `bmcapture` lives.

    Inside the .app bundle normally — that is what gives the capture its
    permission identity. An explicit override wins, and a bare name is the
    last resort so a developer build on PATH still works.
    """
    override = os.environ.get("BEYONDMEETINGS_CAPTURE_HELPER")
    if override:
        return override

    home = Path(home or Path.home())
    bundled = home / "Applications" / APP_BUNDLE / "Contents" / "MacOS" / HELPER_NAME
    if bundled.is_file():
        return str(bundled)

    return HELPER_NAME


class SubprocessRunner:
    """Deliberately not imported from pipewire — a macOS backend should not
    reach into the Linux one for eight lines."""

    def run(self, args: list[str]) -> str:
        return subprocess.run(
            args, capture_output=True, text=True, check=False
        ).stdout.strip()

    def spawn(self, args: list[str]) -> int:
        return subprocess.Popen(args).pid

    def succeeded(self, args: list[str]) -> bool:
        return subprocess.run(args, capture_output=True, check=False).returncode == 0


class MacRecorder(Recorder):
    def __init__(
        self,
        data_dir: Path,
        runner=None,
        segment_minutes: int = 50,
        helper: str | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.runner = runner or SubprocessRunner()
        self.segment_minutes = segment_minutes
        self.helper = helper or resolve_helper()
        self.state_path = self.data_dir / "recording-state.json"
        # Same reasoning as the Linux backend: the state file, not any Python
        # field, decides whether a recording exists, and roll_segment and stop
        # are two writers.
        self._lock = threading.RLock()
        self._state_error: str | None = None

    # ---------- paths ----------

    def _folder(self, state: RecordingState) -> Path:
        folder = self.data_dir / "recordings" / state.date
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _segment_path(self, state: RecordingState, index: int) -> Path:
        return self._folder(state) / f"{state.filename_base}_seg{index:03d}.wav"

    def _intermediates(self, state: RecordingState, index: int) -> tuple[Path, Path]:
        final = self._segment_path(state, index)
        return (
            final.with_suffix(".system.wav"),
            final.with_suffix(".mic.wav"),
        )

    # ---------- helper process ----------

    def _spawn_capture(self, state: RecordingState, index: int) -> int:
        system, mic = self._intermediates(state, index)
        return self.runner.spawn(
            [self.helper, "record", "--system", str(system), "--mic", str(mic)]
        )

    def _kill(self, pid: int) -> None:
        self.runner.run(["kill", str(pid)])

    def _mix(self, state: RecordingState, index: int) -> str:
        """Combine the segment's two streams. Returns the final path.

        The intermediates are the only copy of the meeting, so they are removed
        only once ffmpeg has reported success.
        """
        system, mic = self._intermediates(state, index)
        final = self._segment_path(state, index)

        present = [p for p in (system, mic) if p.is_file() and p.stat().st_size > 0]

        if len(present) < 2:
            # No microphone, or a denied permission. Keep whatever we captured
            # rather than losing the meeting to a missing second stream.
            if present:
                present[0].replace(final)
            return str(final)

        mixed = self.runner.succeeded(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", str(system), "-i", str(mic),
             "-filter_complex", MIX_FILTER, str(final)]
        )
        if mixed:
            system.unlink(missing_ok=True)
            mic.unlink(missing_ok=True)
        return str(final)

    # ---------- Recorder ----------

    def start(self, name: str) -> RecordingState:
        with self._lock:
            stale = self.status()
            if stale:
                self._kill(stale.pid)
                clear_state(self.state_path)

            now = datetime.now()
            day = now.strftime("%Y-%m-%d")
            state = RecordingState(
                name=name,
                filename_base=build_filename_base(name, day, now.strftime("%H-%M")),
                date=day,
                pid=0,
                module_ids=[],  # PipeWire-only; empty here by design.
                segments=[],
                started_at=now.isoformat(timespec="seconds"),
            )

            state.segments.append(str(self._segment_path(state, 0)))
            state.pid = self._spawn_capture(state, 0)
            save_state(state, self.state_path)
            return state

    def roll_segment(self) -> str:
        with self._lock:
            state = self.status()
            if not state:
                raise RuntimeError("no active recording")

            finished_index = len(state.segments) - 1
            self._kill(state.pid)

            # Respawn before mixing: ffmpeg must not widen the gap in coverage.
            next_index = finished_index + 1
            state.segments.append(str(self._segment_path(state, next_index)))
            state.pid = self._spawn_capture(state, next_index)
            save_state(state, self.state_path)

            return self._mix(state, finished_index)

    def stop(self) -> RecordingState:
        with self._lock:
            state = self.status()
            if not state:
                raise RuntimeError("no active recording")

            self._kill(state.pid)
            self._mix(state, len(state.segments) - 1)
            clear_state(self.state_path)
            return state

    def status(self) -> RecordingState | None:
        with self._lock:
            try:
                state = load_state(self.state_path)
            except ValueError as exc:
                self._state_error = str(exc)
                return None
            self._state_error = None
            return state

    @property
    def state_error(self) -> str | None:
        return self._state_error

    def reset(self) -> None:
        with self._lock:
            try:
                stale = load_state(self.state_path)
            except ValueError:
                stale = None
            if stale:
                self._kill(stale.pid)
            clear_state(self.state_path)
            self._state_error = None
