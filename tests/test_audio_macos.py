"""macOS capture, driven entirely through the injected runner.

The Swift helper cannot run here, so every assertion is about the commands
MacRecorder issues. That is deliberate: the command surface is the contract
between the Python side and `bmcapture`, and it is the part that can be pinned
down without a Mac.
"""
import pytest

from beyondmeetings.audio.base import Recorder
from beyondmeetings.audio.macos import MacRecorder, resolve_helper


class FakeRunner:
    """Records commands; reports success unless told otherwise."""

    def __init__(self, mix_succeeds=True):
        self.commands = []
        self.mix_succeeds = mix_succeeds
        self._pid = 5000
        self.running = {}

    def run(self, args) -> str:
        self.commands.append(args)
        if args and args[0] == "kill":
            self.running[int(args[-1])] = False
        return ""

    def spawn(self, args) -> int:
        self.commands.append(args)
        self._pid += 1
        self.running[self._pid] = True
        return self._pid

    def is_running(self, pid):
        return self.running.get(pid, False)

    def succeeded(self, args) -> bool:
        self.commands.append(args)
        return self.mix_succeeds

    # --- helpers for assertions ---

    def of(self, program):
        return [c for c in self.commands if program in c[0]]

    @property
    def flat(self):
        return [" ".join(c) for c in self.commands]


def _recorder(tmp_path, runner=None, **kw):
    return MacRecorder(
        tmp_path, runner=runner or FakeRunner(), helper="/opt/bmcapture", **kw
    )


def _capture(recorder, state, index=0):
    """Stand in for the helper actually writing its two streams.

    Without this there is nothing to mix, and MacRecorder correctly skips
    ffmpeg — calling it on files the helper never wrote would only fail.
    """
    system, mic = recorder._intermediates(state, index)
    system.parent.mkdir(parents=True, exist_ok=True)
    system.write_bytes(b"RIFF")
    mic.write_bytes(b"RIFF")
    return system, mic


# --- interface ---

def test_the_mac_backend_satisfies_the_recorder_interface(tmp_path):
    assert isinstance(_recorder(tmp_path), Recorder)


# --- start ---

def test_start_spawns_the_helper_with_both_output_paths(tmp_path):
    runner = FakeRunner()
    _recorder(tmp_path, runner).start("Client Kickoff")

    spawned = runner.of("bmcapture")[0]
    assert spawned[:2] == ["/opt/bmcapture", "record"]
    assert "--system" in spawned and "--mic" in spawned


def test_start_writes_the_two_streams_to_distinguishable_paths(tmp_path):
    runner = FakeRunner()
    _recorder(tmp_path, runner).start("Standup")

    spawned = runner.of("bmcapture")[0]
    system = spawned[spawned.index("--system") + 1]
    mic = spawned[spawned.index("--mic") + 1]
    assert system.endswith("_seg000.system.wav")
    assert mic.endswith("_seg000.mic.wav")
    assert system != mic


def test_start_can_omit_the_microphone_output(tmp_path):
    runner = FakeRunner()
    state = _recorder(tmp_path, runner).start("Video", microphone_enabled=False)

    spawned = runner.of("bmcapture")[0]
    assert "--system" in spawned
    assert "--mic" not in spawned
    assert state.microphone_enabled is False


def test_start_records_the_final_mixed_path_not_the_intermediates(tmp_path):
    """Everything downstream consumes state.segments; it must see one file."""
    state = _recorder(tmp_path).start("Standup")

    assert len(state.segments) == 1
    assert state.segments[0].endswith("_seg000.wav")
    assert ".system." not in state.segments[0]
    assert ".mic." not in state.segments[0]


def test_start_uses_the_shared_filename_convention(tmp_path):
    state = _recorder(tmp_path).start("Client Kickoff!")

    assert state.filename_base.endswith("_client-kickoff")
    assert state.name == "Client Kickoff!"


def test_start_after_a_stale_recording_clears_it(tmp_path):
    runner = FakeRunner()
    recorder = _recorder(tmp_path, runner)
    recorder.start("First")
    recorder.start("Second")

    assert recorder.status().name == "Second"


# --- rollover ---

def test_roll_segment_stops_the_helper_then_starts_the_next(tmp_path):
    runner = FakeRunner()
    recorder = _recorder(tmp_path, runner)
    state = recorder.start("Long meeting")
    _capture(recorder, state, 0)
    runner.commands.clear()

    recorder.roll_segment()

    assert runner.commands[0][0] == "kill"
    assert "bmcapture" in runner.commands[1][0]


def test_roll_segment_respawns_before_mixing(tmp_path):
    """Mixing first would extend the audio gap by however long ffmpeg takes."""
    runner = FakeRunner()
    recorder = _recorder(tmp_path, runner)
    state = recorder.start("Long meeting")
    _capture(recorder, state, 0)
    runner.commands.clear()

    recorder.roll_segment()

    programs = [c[0] for c in runner.commands]
    assert programs.index("/opt/bmcapture") < next(
        i for i, p in enumerate(programs) if "ffmpeg" in p
    ), f"ffmpeg ran before the next segment was spawned: {programs}"


def test_roll_segment_returns_the_finished_mixed_path(tmp_path):
    recorder = _recorder(tmp_path)
    state = recorder.start("Long meeting")
    _capture(recorder, state, 0)

    finished = recorder.roll_segment()

    assert finished.endswith("_seg000.wav")
    assert ".system." not in finished and ".mic." not in finished


def test_roll_segment_appends_the_next_segment_to_the_state(tmp_path):
    recorder = _recorder(tmp_path)
    state = recorder.start("Long meeting")
    _capture(recorder, state, 0)
    recorder.roll_segment()

    segments = recorder.status().segments
    assert len(segments) == 2
    assert segments[1].endswith("_seg001.wav")


def test_failed_rollover_restart_leaves_recording_paused_and_retryable(tmp_path):
    class RolloverFailureRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.spawn_count = 0

        def spawn(self, args):
            self.spawn_count += 1
            if self.spawn_count == 1:
                return super().spawn(args)
            self.commands.append(args)
            raise RuntimeError("helper refused to start")

    runner = RolloverFailureRunner()
    recorder = _recorder(tmp_path, runner)
    state = recorder.start("Long meeting")
    _capture(recorder, state, 0)

    with pytest.raises(RuntimeError, match="helper refused"):
        recorder.roll_segment()

    state = recorder.status()
    assert state.paused is True
    assert state.pid is None
    assert len(state.segments) == 1

    runner.spawn = FakeRunner.spawn.__get__(runner, RolloverFailureRunner)
    resumed = recorder.resume()
    assert resumed.paused is False
    assert len(resumed.segments) == 2


def test_pause_finalizes_capture_and_resume_uses_a_new_segment(tmp_path):
    recorder = _recorder(tmp_path)
    state = recorder.start("Video", microphone_enabled=False)
    system, _ = recorder._intermediates(state, 0)
    system.parent.mkdir(parents=True, exist_ok=True)
    system.write_bytes(b"RIFF")

    paused = recorder.pause()

    assert paused.paused is True
    assert paused.pid is None
    resumed = recorder.resume()
    assert resumed.paused is False
    assert len(resumed.segments) == 2


def test_pause_waits_for_helper_exit_before_mixing(tmp_path):
    class SlowExitRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.kill_requested = set()
            self.polls = 0

        def run(self, args):
            self.commands.append(args)
            if args and args[0] == "kill":
                self.kill_requested.add(int(args[-1]))
            return ""

        def is_running(self, pid):
            if pid not in self.kill_requested:
                return self.running.get(pid, False)
            self.polls += 1
            return self.polls < 3

        def succeeded(self, args):
            assert self.polls >= 3, "mixed before the helper finalized its WAVs"
            return super().succeeded(args)

    runner = SlowExitRunner()
    recorder = _recorder(tmp_path, runner)
    state = recorder.start("Video")
    _capture(recorder, state)

    recorder.pause()

    assert runner.polls >= 3


def test_pause_with_no_audio_reports_failure_and_keeps_recoverable_state(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.start("Video")

    with pytest.raises(RuntimeError, match="no usable audio"):
        recorder.pause()

    state = recorder.status()
    assert state.paused is True
    assert state.pid is None
    assert len(state.segments) == 1


def test_microphone_change_restarts_helper_with_new_mode(tmp_path):
    runner = FakeRunner()
    recorder = _recorder(tmp_path, runner)
    state = recorder.start("Video")
    _capture(recorder, state)
    runner.commands.clear()

    changed = recorder.set_microphone_enabled(False)

    spawned = runner.of("bmcapture")[0]
    assert "--mic" not in spawned
    assert changed.microphone_enabled is False
    assert len(changed.segments) == 2


def test_stop_while_paused_does_not_kill_a_missing_process(tmp_path):
    runner = FakeRunner()
    recorder = _recorder(tmp_path, runner)
    state = recorder.start("Video", microphone_enabled=False)
    system, _ = recorder._intermediates(state, 0)
    system.parent.mkdir(parents=True, exist_ok=True)
    system.write_bytes(b"RIFF")
    recorder.pause()
    runner.commands.clear()

    stopped = recorder.stop()

    assert stopped.paused is True
    assert not runner.of("kill")
    assert recorder.status() is None


# --- mixing ---

def test_the_mix_combines_both_streams_into_one_file(tmp_path):
    runner = FakeRunner()
    recorder = _recorder(tmp_path, runner)
    state = recorder.start("Standup")
    _capture(recorder, state, 0)
    recorder.stop()

    mix = next(c for c in runner.commands if "ffmpeg" in c[0])
    assert mix.count("-i") == 2, f"expected two inputs: {mix}"
    assert any("amix=inputs=2" in part for part in mix)


def test_the_mix_does_not_force_a_format(tmp_path):
    """compress_for_upload already makes it mono 16k; doing it twice loses quality."""
    runner = FakeRunner()
    recorder = _recorder(tmp_path, runner)
    state = recorder.start("Standup")
    _capture(recorder, state, 0)
    recorder.stop()

    mix = next(c for c in runner.commands if "ffmpeg" in c[0])
    assert "-ar" not in mix and "-ac" not in mix


def test_successful_mix_deletes_the_intermediates(tmp_path):
    runner = FakeRunner(mix_succeeds=True)
    recorder = _recorder(tmp_path, runner)
    state = recorder.start("Standup")
    system, mic = recorder._intermediates(state, 0)
    system.parent.mkdir(parents=True, exist_ok=True)
    system.write_bytes(b"RIFF")
    mic.write_bytes(b"RIFF")

    recorder.stop()

    assert not system.exists() and not mic.exists()


def test_a_failed_mix_keeps_the_intermediates(tmp_path):
    """They are the only copy of the meeting. Losing them to a mix failure is fatal."""
    runner = FakeRunner(mix_succeeds=False)
    recorder = _recorder(tmp_path, runner)
    state = recorder.start("Standup")
    system, mic = recorder._intermediates(state, 0)
    system.parent.mkdir(parents=True, exist_ok=True)
    system.write_bytes(b"RIFF")
    mic.write_bytes(b"RIFF")

    with pytest.raises(RuntimeError, match="mix"):
        recorder.stop()

    assert system.exists() and mic.exists()


def test_a_missing_mic_stream_still_produces_a_segment(tmp_path):
    """No microphone, or a denied permission, must not lose the system audio."""
    runner = FakeRunner()
    recorder = _recorder(tmp_path, runner)
    state = recorder.start("Standup")
    system, _ = recorder._intermediates(state, 0)
    system.parent.mkdir(parents=True, exist_ok=True)
    system.write_bytes(b"RIFF")

    recorder.stop()

    final = tmp_path / "recordings" / state.date / f"{state.filename_base}_seg000.wav"
    assert final.exists(), "system-only audio was dropped instead of kept"


# --- stop ---

def test_stop_kills_the_helper(tmp_path):
    runner = FakeRunner()
    recorder = _recorder(tmp_path, runner)
    state = recorder.start("Standup")
    _capture(recorder, state, 0)
    runner.commands.clear()

    recorder.stop()

    assert runner.commands[0][0] == "kill"


def test_stop_without_a_recording_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="no active recording"):
        _recorder(tmp_path).stop()


def test_stop_clears_the_state(tmp_path):
    recorder = _recorder(tmp_path)
    state = recorder.start("Standup")
    _capture(recorder, state, 0)
    recorder.stop()

    assert recorder.status() is None


# --- status, reset, state_error ---

def test_status_is_none_before_anything_starts(tmp_path):
    assert _recorder(tmp_path).status() is None


def test_a_corrupt_state_file_reports_an_error_rather_than_raising(tmp_path):
    """Raising here used to 500 every poll and wedge the UI."""
    recorder = _recorder(tmp_path)
    recorder.state_path.parent.mkdir(parents=True, exist_ok=True)
    recorder.state_path.write_text("{not json")

    assert recorder.status() is None
    assert "corrupt" in recorder.state_error


def test_reset_forgets_a_wedged_recording(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.start("Standup")

    recorder.reset()

    assert recorder.status() is None


def test_reset_survives_a_corrupt_state_file(tmp_path):
    recorder = _recorder(tmp_path)
    recorder.state_path.parent.mkdir(parents=True, exist_ok=True)
    recorder.state_path.write_text("{not json")

    recorder.reset()

    assert recorder.status() is None
    assert recorder.state_error is None


# --- helper resolution ---

def test_the_helper_is_found_inside_the_app_bundle(tmp_path):
    bundle = tmp_path / "Applications" / "beyondMeetings.app" / "Contents" / "MacOS"
    bundle.mkdir(parents=True)
    helper = bundle / "bmcapture"
    helper.write_text("")
    helper.chmod(0o755)

    assert resolve_helper(home=tmp_path) == str(helper)


def test_an_explicit_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("BEYONDMEETINGS_CAPTURE_HELPER", "/custom/bmcapture")
    assert resolve_helper(home=tmp_path) == "/custom/bmcapture"


def test_it_falls_back_to_the_bare_name_on_path(tmp_path):
    assert resolve_helper(home=tmp_path) == "bmcapture"
