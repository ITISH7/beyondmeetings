from pathlib import Path

import pytest

from beyondmeetings.audio.pipewire import PipeWireRecorder, build_filename_base


@pytest.fixture(autouse=True)
def capture_tools(monkeypatch):
    monkeypatch.setattr(
        "beyondmeetings.audio.pipewire.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )


class FakeRunner:
    """Records commands; returns canned stdout per command shape."""

    def __init__(self):
        self.commands = []
        self.next_module_id = 100
        self.running = {}

    def run(self, args) -> str:
        self.commands.append(args)
        if args and args[0] == "kill":
            self.running[int(args[-1])] = False
            return ""
        if args[:2] == ["pactl", "load-module"]:
            self.next_module_id += 1
            return str(self.next_module_id)
        if args[:3] == ["pactl", "list", "sources"]:
            return "1\talsa_output.pci.monitor\n2\tmeeting_mix.monitor\n"
        if args[:2] == ["pactl", "info"]:
            return "Default Source: alsa_input.pci\n"
        return ""

    def spawn(self, args) -> int:
        self.commands.append(args)
        Path(args[-1]).write_bytes(b"RIFF" + b"\0" * 40)
        self.running[4242] = True
        return 4242

    def is_running(self, pid) -> bool:
        return self.running.get(pid, False)


def test_filename_base_is_slugified_with_timestamp():
    base = build_filename_base("Client Kickoff!", "2026-07-30", "14-30")
    assert base == "2026-07-30_14-30_client-kickoff"


def test_filename_base_falls_back_when_name_has_no_safe_chars():
    assert build_filename_base("!!!", "2026-07-30", "14-30") == "2026-07-30_14-30_meeting"


def test_start_creates_null_sink_first(tmp_path):
    runner = FakeRunner()
    PipeWireRecorder(data_dir=tmp_path, runner=runner).start("Standup")
    assert runner.commands[0][:3] == ["pactl", "load-module", "module-null-sink"]


def test_start_loops_monitors_excluding_the_mix_itself(tmp_path):
    runner = FakeRunner()
    PipeWireRecorder(data_dir=tmp_path, runner=runner).start("Standup")
    loopbacks = [c for c in runner.commands if "module-loopback" in c]
    sources = " ".join(" ".join(c) for c in loopbacks)
    assert "alsa_output.pci.monitor" in sources
    assert "source=meeting_mix.monitor" not in sources


def test_start_also_loops_the_default_microphone(tmp_path):
    runner = FakeRunner()
    PipeWireRecorder(data_dir=tmp_path, runner=runner).start("Standup")
    sources = " ".join(" ".join(c) for c in runner.commands)
    assert "alsa_input.pci" in sources


def test_start_can_capture_laptop_audio_without_the_microphone(tmp_path):
    runner = FakeRunner()
    state = PipeWireRecorder(data_dir=tmp_path, runner=runner).start(
        "Video", microphone_enabled=False
    )

    loopbacks = [" ".join(c) for c in runner.commands if "module-loopback" in c]
    assert not any("alsa_input.pci" in command for command in loopbacks)
    assert state.microphone_enabled is False
    assert state.microphone_module_id is None


def test_start_records_module_ids_into_state(tmp_path):
    runner = FakeRunner()
    state = PipeWireRecorder(data_dir=tmp_path, runner=runner).start("Standup")
    assert len(state.module_ids) >= 2
    assert state.pid == 4242


def test_start_writes_first_segment_path(tmp_path):
    runner = FakeRunner()
    state = PipeWireRecorder(data_dir=tmp_path, runner=runner).start("Standup")
    assert state.segments[0].endswith("_seg000.wav")


def test_start_uses_parec_with_the_mix_monitor(tmp_path):
    runner = FakeRunner()
    PipeWireRecorder(data_dir=tmp_path, runner=runner).start("Standup")
    capture = next(c for c in runner.commands if c and c[0] == "parec")
    assert "--device=meeting_mix.monitor" in capture
    assert "--file-format=wav" in capture


def test_start_falls_back_to_pw_record_when_parec_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "beyondmeetings.audio.pipewire.shutil.which",
        lambda name: None if name == "parec" else f"/usr/bin/{name}",
    )
    runner = FakeRunner()
    PipeWireRecorder(data_dir=tmp_path, runner=runner).start("Standup")
    capture = next(c for c in runner.commands if c and c[0] == "pw-record")
    assert capture[1:3] == ["--target", "meeting_mix.monitor"]


def test_start_explains_when_neither_capture_tool_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "beyondmeetings.audio.pipewire.shutil.which", lambda _name: None
    )
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=FakeRunner())
    with pytest.raises(RuntimeError, match="neither parec nor pw-record"):
        recorder.start("Standup")
    assert recorder.status() is None


def test_start_fails_immediately_when_capture_process_exits(tmp_path):
    class DeadRunner(FakeRunner):
        def spawn(self, args):
            self.commands.append(args)
            self.running[4242] = False
            return 4242

    recorder = PipeWireRecorder(data_dir=tmp_path, runner=DeadRunner())
    with pytest.raises(RuntimeError, match="exited before writing"):
        recorder.start("Standup")
    assert recorder.status() is None


def test_status_reflects_persisted_state(tmp_path):
    runner = FakeRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    recorder.start("Standup")
    assert recorder.status().name == "Standup"


def test_stop_unloads_every_module(tmp_path):
    runner = FakeRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    state = recorder.start("Standup")
    runner.commands.clear()
    recorder.stop()
    unloaded = [c[-1] for c in runner.commands if c[:2] == ["pactl", "unload-module"]]
    assert sorted(unloaded) == sorted(str(m) for m in state.module_ids)


def test_stop_clears_state(tmp_path):
    runner = FakeRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    recorder.start("Standup")
    recorder.stop()
    assert recorder.status() is None


def test_stop_without_start_raises(tmp_path):
    with pytest.raises(RuntimeError, match="no active recording"):
        PipeWireRecorder(data_dir=tmp_path, runner=FakeRunner()).stop()


def test_roll_segment_appends_a_new_file_and_returns_the_finished_one(tmp_path):
    runner = FakeRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    recorder.start("Standup")
    finished = recorder.roll_segment()
    state = recorder.status()
    assert finished.endswith("_seg000.wav")
    assert len(state.segments) == 2
    assert state.segments[1].endswith("_seg001.wav")


def test_pause_finalizes_capture_and_resume_starts_a_new_segment(tmp_path):
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=FakeRunner())
    recorder.start("Video")

    paused = recorder.pause()

    assert paused.paused is True
    assert paused.pid is None
    assert len(paused.segments) == 1

    resumed = recorder.resume()

    assert resumed.paused is False
    assert resumed.pid == 4242
    assert len(resumed.segments) == 2
    assert resumed.segments[-1].endswith("_seg001.wav")


def test_microphone_change_restarts_active_capture_with_a_new_segment(tmp_path):
    runner = FakeRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    recorder.start("Video")
    mic_module = recorder.status().microphone_module_id
    runner.commands.clear()

    state = recorder.set_microphone_enabled(False)

    assert state.microphone_enabled is False
    assert state.microphone_module_id is None
    assert len(state.segments) == 2
    assert ["pactl", "unload-module", str(mic_module)] in runner.commands


def test_microphone_change_while_paused_only_updates_the_next_capture(tmp_path):
    runner = FakeRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    recorder.start("Video")
    recorder.pause()
    runner.commands.clear()

    state = recorder.set_microphone_enabled(False)

    assert state.paused is True
    assert state.microphone_enabled is False
    assert not any(c and c[0] in {"parec", "pw-record"} for c in runner.commands)


def test_stop_from_pause_returns_all_segments_and_clears_state(tmp_path):
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=FakeRunner())
    recorder.start("Video")
    recorder.pause()

    stopped = recorder.stop()

    assert len(stopped.segments) == 1
    assert recorder.status() is None


def test_failed_resume_kills_new_process_and_keeps_completed_segments(
    tmp_path, monkeypatch
):
    class ResumeFailureRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.spawn_count = 0

        def spawn(self, args):
            self.spawn_count += 1
            if self.spawn_count == 1:
                return super().spawn(args)
            self.commands.append(args)
            self.running[5252] = True
            return 5252

    runner = ResumeFailureRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    recorder.start("Video")
    recorder.pause()
    monkeypatch.setattr("beyondmeetings.audio.pipewire.CAPTURE_START_TIMEOUT", 0)

    with pytest.raises(RuntimeError, match="did not create"):
        recorder.resume()

    state = recorder.status()
    assert state.paused is True
    assert state.pid is None
    assert len(state.segments) == 1
    assert ["kill", "5252"] in runner.commands


def test_failed_resume_waits_for_signal_ignoring_process_before_forgetting_pid(
    tmp_path, monkeypatch
):
    class StubbornResumeRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.spawn_count = 0

        def run(self, args):
            if args and args[0] == "kill":
                self.commands.append(args)
                return ""
            return super().run(args)

        def spawn(self, args):
            self.spawn_count += 1
            if self.spawn_count == 1:
                return super().spawn(args)
            self.commands.append(args)
            self.running[5252] = True
            return 5252

    runner = StubbornResumeRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    recorder.start("Video")
    recorder.pause()
    monkeypatch.setattr("beyondmeetings.audio.pipewire.CAPTURE_START_TIMEOUT", 0)
    monkeypatch.setattr("beyondmeetings.audio.pipewire.CAPTURE_STOP_TIMEOUT", 0)

    with pytest.raises(RuntimeError, match="did not create"):
        recorder.resume()

    assert ["kill", "-KILL", "5252"] in runner.commands
    state = recorder.status()
    assert state.paused is True
    assert state.pid is None


def test_failed_rollover_restart_leaves_recording_paused_and_retryable(
    tmp_path, monkeypatch
):
    class RolloverFailureRunner(FakeRunner):
        def __init__(self):
            super().__init__()
            self.spawn_count = 0
            self.fail_replacements = True

        def spawn(self, args):
            self.spawn_count += 1
            if self.spawn_count == 1 or not self.fail_replacements:
                return super().spawn(args)
            self.commands.append(args)
            self.running[5252] = True
            return 5252

    runner = RolloverFailureRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    recorder.start("Video")
    monkeypatch.setattr("beyondmeetings.audio.pipewire.CAPTURE_START_TIMEOUT", 0)

    with pytest.raises(RuntimeError, match="did not create"):
        recorder.roll_segment()

    state = recorder.status()
    assert state.paused is True
    assert state.pid is None
    assert len(state.segments) == 1

    runner.fail_replacements = False
    monkeypatch.setattr("beyondmeetings.audio.pipewire.CAPTURE_START_TIMEOUT", 3.0)
    resumed = recorder.resume()
    assert resumed.paused is False
    assert len(resumed.segments) == 2


def test_failed_microphone_enable_leaves_recording_safely_paused(tmp_path):
    class MicrophoneFailureRunner(FakeRunner):
        def run(self, args):
            if (
                args[:3] == ["pactl", "load-module", "module-loopback"]
                and any("alsa_input.pci" in arg for arg in args)
            ):
                raise RuntimeError("microphone unavailable")
            return super().run(args)

    runner = MicrophoneFailureRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    recorder.start("Video", microphone_enabled=False)

    with pytest.raises(RuntimeError, match="microphone unavailable"):
        recorder.set_microphone_enabled(True)

    state = recorder.status()
    assert state.paused is True
    assert state.pid is None
    assert state.microphone_enabled is False
    assert len(state.segments) == 1


def test_stale_modules_are_cleaned_before_a_new_start(tmp_path):
    runner = FakeRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    recorder.start("First")
    runner.commands.clear()
    recorder.start("Second")
    assert any(c[:2] == ["pactl", "unload-module"] for c in runner.commands)


# --- Review finding #9: corrupt state must not wedge the app ---

def test_status_returns_none_on_corrupt_state_instead_of_raising(tmp_path):
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=FakeRunner())
    (tmp_path / "recording-state.json").write_text("{ not json")
    assert recorder.status() is None
    assert "corrupt" in recorder.state_error


def test_state_error_clears_once_the_file_is_readable(tmp_path):
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=FakeRunner())
    (tmp_path / "recording-state.json").write_text("{ not json")
    recorder.status()
    recorder.reset()
    assert recorder.status() is None
    assert recorder.state_error is None


def test_reset_removes_a_corrupt_state_file(tmp_path):
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=FakeRunner())
    (tmp_path / "recording-state.json").write_text("{ not json")
    recorder.reset()
    assert not (tmp_path / "recording-state.json").exists()


def test_reset_tears_down_modules_of_a_live_recording(tmp_path):
    runner = FakeRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    state = recorder.start("Test")
    runner.commands.clear()
    recorder.reset()
    unloaded = [c[-1] for c in runner.commands if c[:2] == ["pactl", "unload-module"]]
    assert sorted(unloaded) == sorted(str(m) for m in state.module_ids)
    assert recorder.status() is None


def test_roll_and_stop_do_not_interleave(tmp_path):
    """roll_segment and stop are two writers of the same state file."""
    import threading

    runner = FakeRunner()
    recorder = PipeWireRecorder(data_dir=tmp_path, runner=runner)
    recorder.start("Test")

    order = []
    original = recorder._spawn_capture

    def slow_spawn(target):
        order.append("spawn-enter")
        threading.Event().wait(0.05)
        order.append("spawn-exit")
        return original(target)

    recorder._spawn_capture = slow_spawn
    roller = threading.Thread(target=recorder.roll_segment)
    roller.start()
    threading.Event().wait(0.01)
    try:
        recorder.stop()
    except RuntimeError:
        pass
    roller.join()

    # The lock must not let stop() slip between spawn-enter and spawn-exit.
    assert order == ["spawn-enter", "spawn-exit"]
