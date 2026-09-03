"""Windows recording through WASAPI loopback and the default microphone."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .base import (
    Recorder, RecordingState, build_filename_base, clear_state, load_state, save_state,
)


class SubprocessRunner:
    def spawn(self, args: list[str]) -> int:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags,
        ).pid

    def kill(self, pid: int) -> None:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, check=False,
        )


class WindowsRecorder(Recorder):
    """Segmented recorder whose worker keeps WAV headers valid on shutdown."""

    def __init__(self, data_dir: Path, runner=None, segment_minutes: int = 50):
        self.data_dir = Path(data_dir)
        self.runner = runner or SubprocessRunner()
        self.segment_minutes = segment_minutes
        self.state_path = self.data_dir / "recording-state.json"
        self._lock = threading.RLock()
        self._state_error: str | None = None

    def _segment_path(self, state: RecordingState, index: int) -> Path:
        folder = self.data_dir / "recordings" / state.date
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{state.filename_base}_seg{index:03d}.wav"

    @staticmethod
    def _signal_paths(target: Path) -> tuple[Path, Path, Path]:
        return (
            target.with_suffix(".stop"),
            target.with_suffix(".done"),
            target.with_suffix(".error"),
        )

    def _spawn(self, target: Path, microphone_enabled: bool = True) -> int:
        stop, done, error = self._signal_paths(target)
        stop.unlink(missing_ok=True)
        done.unlink(missing_ok=True)
        error.unlink(missing_ok=True)
        command = [sys.executable, "-m", "beyondmeetings.audio.windows_worker"]
        if not microphone_enabled:
            command.append("--no-microphone")
        command.append(str(target))
        return self.runner.spawn(command)

    def _finish(self, target: Path, pid: int, timeout: float = 12.0) -> None:
        stop, done, error = self._signal_paths(target)
        stop.touch()
        deadline = time.monotonic() + timeout
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not done.exists():
            self.runner.kill(pid)
            raise RuntimeError("Windows audio capture did not stop cleanly")
        stop.unlink(missing_ok=True)
        done.unlink(missing_ok=True)
        if error.exists():
            detail = error.read_text(encoding="utf-8", errors="replace")
            error.unlink(missing_ok=True)
            raise RuntimeError(f"Windows audio capture failed: {detail}")

    def _check_started(self, target: Path, pid: int) -> None:
        _stop, done, error = self._signal_paths(target)
        if error.exists():
            detail = error.read_text(encoding="utf-8", errors="replace")
            error.unlink(missing_ok=True)
            done.unlink(missing_ok=True)
            self.runner.kill(pid)
            raise RuntimeError(f"Windows audio capture failed: {detail}")

    def _resume_capture(self, state: RecordingState) -> None:
        target = self._segment_path(state, len(state.segments))
        pid = None
        try:
            pid = self._spawn(target, state.microphone_enabled)
            self._check_started(target, pid)
        except Exception:
            if pid is not None:
                self.runner.kill(pid)
            target.unlink(missing_ok=True)
            state.pid = None
            state.paused = True
            save_state(state, self.state_path)
            raise
        state.segments.append(str(target))
        state.pid = pid
        state.paused = False
        save_state(state, self.state_path)

    def start(self, name: str, microphone_enabled: bool = True) -> RecordingState:
        with self._lock:
            stale = self.status()
            if stale:
                if stale.pid is not None:
                    self._finish(Path(stale.segments[-1]), stale.pid)
                clear_state(self.state_path)
            now = datetime.now()
            day = now.strftime("%Y-%m-%d")
            state = RecordingState(
                name=name,
                filename_base=build_filename_base(name, day, now.strftime("%H-%M")),
                date=day, pid=0, segments=[], started_at=now.isoformat(timespec="seconds"),
                microphone_enabled=microphone_enabled,
            )
            target = self._segment_path(state, 0)
            state.segments.append(str(target))
            try:
                state.pid = self._spawn(target, state.microphone_enabled)
                self._check_started(target, state.pid)
            except Exception:
                clear_state(self.state_path)
                target.unlink(missing_ok=True)
                raise
            save_state(state, self.state_path)
            return state

    def roll_segment(self) -> str:
        with self._lock:
            state = self.status()
            if not state:
                raise RuntimeError("no active recording")
            if state.paused or state.pid is None:
                raise RuntimeError("recording is paused")
            finished = Path(state.segments[-1])
            self._finish(finished, state.pid)
            state.pid = None
            state.paused = True
            save_state(state, self.state_path)
            self._resume_capture(state)
            return str(finished)

    def pause(self) -> RecordingState:
        with self._lock:
            state = self.status()
            if not state:
                raise RuntimeError("no active recording")
            if state.paused or state.pid is None:
                raise RuntimeError("recording is already paused")
            self._finish(Path(state.segments[-1]), state.pid)
            state.pid = None
            state.paused = True
            save_state(state, self.state_path)
            return state

    def resume(self) -> RecordingState:
        with self._lock:
            state = self.status()
            if not state:
                raise RuntimeError("no active recording")
            if not state.paused:
                raise RuntimeError("recording is not paused")
            self._resume_capture(state)
            return state

    def set_microphone_enabled(self, enabled: bool) -> RecordingState:
        with self._lock:
            state = self.status()
            if not state:
                raise RuntimeError("no active recording")
            if state.microphone_enabled == enabled:
                return state
            was_paused = state.paused
            if not was_paused:
                self.pause()
                state = self.status()
            state.microphone_enabled = enabled
            save_state(state, self.state_path)
            if not was_paused:
                return self.resume()
            return state

    def stop(self) -> RecordingState:
        with self._lock:
            state = self.status()
            if not state:
                raise RuntimeError("no active recording")
            if state.pid is not None:
                self._finish(Path(state.segments[-1]), state.pid)
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
                if stale.pid is not None:
                    try:
                        self._finish(Path(stale.segments[-1]), stale.pid)
                    except RuntimeError:
                        pass
            clear_state(self.state_path)
            self._state_error = None
