"""macOS-only prerequisite checks.

Registered only when running on Darwin, so a Linux machine never constructs
them and never sees them in the wizard.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ..desktop_macos import app_bundle_path, helper_path, install_app_bundle
from .base import Check, CheckResult

# Deep link straight to the right pane; hunting for it in System Settings is
# where most people give up.
SCREEN_RECORDING_SETTINGS = (
    "x-apple.systempreferences:com.apple.preference.security"
    "?Privacy_ScreenCapture"
)
MICROPHONE_SETTINGS = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
)


def _run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout.strip()


class XcodeToolsCheck(Check):
    id = "xcode-tools"
    label = "Xcode command line tools"
    description = (
        "Needed once, to compile the audio capture helper. beyondMeetings does "
        "not ship a prebuilt binary, so nothing unsigned has to be trusted."
    )
    required = True

    def detect(self) -> CheckResult:
        if not shutil.which("swiftc"):
            return CheckResult(
                status="missing",
                detail=(
                    "swiftc not found. Install the command line tools with "
                    "`xcode-select --install`, then re-run the installer."
                ),
            )
        code, out = _run(["swiftc", "--version"])
        if code != 0:
            return CheckResult(status="broken", detail="swiftc is present but failed to run.")
        return CheckResult(status="ok", detail=out.splitlines()[0] if out else "swiftc found")


class CaptureHelperCheck(Check):
    id = "capture-helper"
    label = "Audio capture helper"
    description = (
        "bmcapture records system audio and your microphone. It lives inside "
        "the app bundle so macOS can attach its permissions to beyondMeetings."
    )
    required = True

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else None

    def detect(self) -> CheckResult:
        helper = helper_path(self.home)
        if not helper.is_file():
            return CheckResult(
                status="missing",
                detail=(
                    f"Not built yet — expected at {helper}. Re-run the "
                    "installer to compile it."
                ),
            )
        return CheckResult(status="ok", detail=str(helper))


class AppBundleCheck(Check):
    id = "app-bundle"
    label = "App bundle"
    description = (
        "macOS grants privacy permissions per app, so beyondMeetings needs to "
        "be a real application rather than a bare command."
    )
    required = True

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else None

    def detect(self) -> CheckResult:
        bundle = app_bundle_path(self.home)
        if not (bundle / "Contents" / "Info.plist").is_file():
            return CheckResult(
                status="missing", detail=f"No app bundle at {bundle}."
            )
        return CheckResult(status="ok", detail=str(bundle))

    @property
    def fixable(self) -> bool:
        return True

    def fix(self, **kwargs) -> CheckResult:
        built = helper_path(self.home)
        install_app_bundle(
            home=self.home, helper=built if built.is_file() else None
        )
        return self.detect()


class ScreenRecordingPermissionCheck(Check):
    id = "screen-recording"
    label = "Screen recording permission"
    description = (
        "macOS delivers system audio — everyone else on the call — under the "
        "screen recording permission. Nothing is recorded from your screen."
    )
    required = True

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else None

    def _permissions(self) -> dict | None:
        helper = helper_path(self.home)
        if not helper.is_file():
            return None
        code, out = _run([str(helper), "check-permissions", "--json"])
        if code != 0 or not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None

    def detect(self) -> CheckResult:
        permissions = self._permissions()
        if permissions is None:
            return CheckResult(
                status="missing",
                detail="Cannot check yet — the capture helper is not built.",
            )
        if permissions.get("screen_recording"):
            return CheckResult(status="ok", detail="Granted.")
        return CheckResult(
            status="missing",
            detail=(
                "Not granted. Open System Settings → Privacy & Security → "
                f"Screen Recording and enable beyondMeetings ({SCREEN_RECORDING_SETTINGS}). "
                "macOS requires the app to be reopened afterwards before the "
                "permission takes effect."
            ),
        )


class MicrophonePermissionCheck(ScreenRecordingPermissionCheck):
    id = "microphone"
    label = "Microphone permission"
    description = "Records your own voice. Without it only the other participants are captured."
    required = False

    def detect(self) -> CheckResult:
        permissions = self._permissions()
        if permissions is None:
            return CheckResult(
                status="missing",
                detail="Cannot check yet — the capture helper is not built.",
            )
        if permissions.get("microphone"):
            return CheckResult(status="ok", detail="Granted.")
        return CheckResult(
            status="missing",
            detail=(
                "Not granted. Open System Settings → Privacy & Security → "
                f"Microphone and enable beyondMeetings ({MICROPHONE_SETTINGS}). "
                "Recording still works without it, but your own voice will be "
                "missing from the transcript."
            ),
        )
