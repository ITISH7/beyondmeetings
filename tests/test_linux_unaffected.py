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
