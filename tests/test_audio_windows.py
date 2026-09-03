from pathlib import Path
import sys

import pytest

from beyondmeetings.audio.windows import WindowsRecorder
from beyondmeetings.audio.windows_worker import record


class Runner:
    def __init__(self):
        self.spawned = []
        self.killed = []

    def spawn(self, args):
        self.spawned.append(args)
        target = Path(args[-1])
        target.write_bytes(b"RIFF-fake")
        target.with_suffix(".done").touch()
        return 700 + len(self.spawned)

    def kill(self, pid):
        self.killed.append(pid)


def test_windows_records_segmented_wav_files(tmp_path):
    runner = Runner()
    recorder = WindowsRecorder(tmp_path, runner=runner)
    state = recorder.start("Planning")
    assert state.segments[0].endswith("_seg000.wav")
    assert recorder.roll_segment().endswith("_seg000.wav")
    stopped = recorder.stop()
    assert stopped.segments[-1].endswith("_seg001.wav")
    assert recorder.status() is None
    assert runner.killed == []


def test_windows_reset_clears_corrupt_state(tmp_path):
    recorder = WindowsRecorder(tmp_path, runner=Runner())
    recorder.state_path.parent.mkdir(parents=True, exist_ok=True)
    recorder.state_path.write_text("not json")
    assert recorder.status() is None
    assert recorder.state_error
    recorder.reset()
    assert recorder.state_error is None


def test_windows_can_start_without_opening_the_microphone(tmp_path):
    runner = Runner()
    state = WindowsRecorder(tmp_path, runner=runner).start(
        "Video", microphone_enabled=False
    )

    assert "--no-microphone" in runner.spawned[0]
    assert state.microphone_enabled is False


def test_windows_pause_resume_and_stop_from_pause(tmp_path):
    recorder = WindowsRecorder(tmp_path, runner=Runner())
    recorder.start("Video")

    paused = recorder.pause()
    assert paused.paused is True
    assert paused.pid is None

    resumed = recorder.resume()
    assert resumed.paused is False
    assert len(resumed.segments) == 2

    recorder.pause()
    stopped = recorder.stop()
    assert stopped.paused is True
    assert recorder.status() is None


def test_windows_microphone_change_restarts_capture(tmp_path):
    runner = Runner()
    recorder = WindowsRecorder(tmp_path, runner=runner)
    recorder.start("Video")

    changed = recorder.set_microphone_enabled(False)

    assert changed.microphone_enabled is False
    assert len(changed.segments) == 2
    assert "--no-microphone" in runner.spawned[-1]


def test_windows_failed_rollover_restart_leaves_recording_paused_and_retryable(tmp_path):
    class RolloverFailureRunner(Runner):
        def spawn(self, args):
            if self.spawned:
                self.spawned.append(args)
                raise RuntimeError("worker refused to start")
            return super().spawn(args)

    runner = RolloverFailureRunner()
    recorder = WindowsRecorder(tmp_path, runner=runner)
    recorder.start("Video")

    with pytest.raises(RuntimeError, match="worker refused"):
        recorder.roll_segment()

    state = recorder.status()
    assert state.paused is True
    assert state.pid is None
    assert len(state.segments) == 1

    runner.spawn = Runner.spawn.__get__(runner, RolloverFailureRunner)
    resumed = recorder.resume()
    assert resumed.paused is False
    assert len(resumed.segments) == 2


def test_windows_resume_worker_error_keeps_recording_paused(tmp_path):
    class ErrorRunner(Runner):
        def spawn(self, args):
            pid = super().spawn(args)
            if len(self.spawned) > 1:
                Path(args[-1]).with_suffix(".error").write_text(
                    "device missing", encoding="utf-8"
                )
            return pid

    runner = ErrorRunner()
    recorder = WindowsRecorder(tmp_path, runner=runner)
    recorder.start("Video")
    recorder.pause()

    with pytest.raises(RuntimeError, match="device missing"):
        recorder.resume()

    state = recorder.status()
    assert state.paused is True
    assert state.pid is None
    assert len(state.segments) == 1


def test_windows_worker_does_not_query_microphone_in_laptop_only_mode(
    tmp_path, monkeypatch
):
    microphone_queries = []

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class Device:
        id = "speaker"

        def recorder(self, **kwargs):
            return Stream()

    class Soundcard:
        @staticmethod
        def default_speaker():
            return Device()

        @staticmethod
        def get_microphone(device_id, include_loopback=False):
            return Device()

        @staticmethod
        def default_microphone():
            microphone_queries.append(True)
            return Device()

    monkeypatch.setitem(sys.modules, "soundcard", Soundcard)
    monkeypatch.setitem(sys.modules, "numpy", object())
    target = tmp_path / "system.wav"
    target.with_suffix(".stop").touch()

    record(target, microphone_enabled=False)

    assert microphone_queries == []
    assert target.with_suffix(".done").exists()
