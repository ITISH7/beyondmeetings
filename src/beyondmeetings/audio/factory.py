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
