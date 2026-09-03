import pytest

from beyondmeetings.audio.base import RecordingState, clear_state, load_state, save_state


def _state():
    return RecordingState(
        name="standup", filename_base="2026-07-30_14-30_standup",
        date="2026-07-30", pid=4242, module_ids=[101, 102],
        segments=["/data/recordings/2026-07-30/seg_000.wav"],
        started_at="2026-07-30T14:30:00",
    )


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "state.json"
    save_state(_state(), path)
    assert load_state(path) == _state()


def test_load_returns_none_when_absent(tmp_path):
    assert load_state(tmp_path / "state.json") is None


def test_clear_removes_the_file(tmp_path):
    path = tmp_path / "state.json"
    save_state(_state(), path)
    clear_state(path)
    assert not path.exists()


def test_clear_is_safe_when_already_absent(tmp_path):
    clear_state(tmp_path / "state.json")


def test_load_raises_on_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    with pytest.raises(ValueError):
        load_state(path)


def test_segment_paths_accumulate(tmp_path):
    path = tmp_path / "state.json"
    state = _state()
    state.segments.append("/data/recordings/2026-07-30/seg_001.wav")
    save_state(state, path)
    assert len(load_state(path).segments) == 2


def test_old_recording_state_defaults_to_active_with_microphone(tmp_path):
    """State files written before capture controls must remain loadable."""
    path = tmp_path / "recording-state.json"
    path.write_text(
        '{"name":"Old","filename_base":"old","date":"2026-07-30",'
        '"pid":42,"module_ids":[],"segments":[],"started_at":"2026-07-30T10:00:00"}'
    )

    state = load_state(path)

    assert state.paused is False
    assert state.microphone_enabled is True
    assert state.pid == 42


def test_paused_recording_state_accepts_no_capture_process():
    state = RecordingState(
        name="Paused", filename_base="paused", date="2026-07-30", pid=None,
        segments=["/data/seg000.wav"], started_at="2026-07-30T10:00:00",
        paused=True, microphone_enabled=False,
    )

    assert state.pid is None
    assert state.paused is True
    assert state.microphone_enabled is False
