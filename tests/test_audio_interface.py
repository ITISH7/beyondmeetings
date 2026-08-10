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
