"""Recorder interface and recording state.

One JSON file replaces the six dotfiles the shell pipeline scattered through
the home directory (.record_pid, .current_recording, .current_name,
.current_filename, .mix_modules, .current_followup).

macOS/Windows support means adding a sibling of pipewire.py implementing
Recorder — nothing else changes.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, Field


class RecordingState(BaseModel):
    name: str
    filename_base: str
    date: str
    pid: int | None
    module_ids: list[int] = Field(default_factory=list)
    segments: list[str] = Field(default_factory=list)
    started_at: str
    paused: bool = False
    microphone_enabled: bool = True
    microphone_module_id: int | None = None


def save_state(state: RecordingState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json(indent=2))


def load_state(path: Path) -> RecordingState | None:
    if not path.exists():
        return None
    try:
        return RecordingState(**json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"corrupt recording state at {path}: {exc}") from exc


def clear_state(path: Path) -> None:
    path.unlink(missing_ok=True)


def build_filename_base(name: str, day: str, clock: str) -> str:
    """The `YYYY-MM-DD_HH-MM_slug` convention every backend and path derives from."""
    slug = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", "-")).strip("-")
    return f"{day}_{clock}_{slug or 'meeting'}"


class Recorder(ABC):
    """A capture backend. One implementation per platform.

    Every member here is called by the application: RolloverWorker calls
    roll_segment() on a timer, SessionManager calls reset() to clear a wedged
    recording and reads state_error to explain one. Declaring them means a new
    backend that forgets one fails at construction, not mid-meeting.
    """

    @abstractmethod
    def start(self, name: str, microphone_enabled: bool = True) -> RecordingState:
        ...

    @abstractmethod
    def pause(self) -> RecordingState:
        ...

    @abstractmethod
    def resume(self) -> RecordingState:
        ...

    @abstractmethod
    def set_microphone_enabled(self, enabled: bool) -> RecordingState:
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
