from pathlib import Path

import pytest

from beyondmeetings.models import MeetingNote
from beyondmeetings.translation import (
    build_translation_prompt,
    resolve_meeting_transcript,
    split_transcript,
    translate_transcript,
    translation_pdf_markdown,
)


class StubProvider:
    def __init__(self):
        self.prompts = []

    def analyse(self, prompt, valid_candidate_ids=None):
        self.prompts.append(prompt)
        return MeetingNote(
            title="Translated Transcript",
            date="2026-01-01",
            executive_summary=f"translated chunk {len(self.prompts)}",
        )


def test_explicit_transcript_reference_resolves_only_inside_data_dir(tmp_path):
    transcript = tmp_path / "transcripts" / "2026-08-18" / "recording.txt"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("everything said")
    note = '---\ntranscript: "2026-08-18/recording.txt"\n---\n# Review\n'
    assert resolve_meeting_transcript(note, tmp_path) == transcript.resolve()


def test_existing_note_is_matched_by_recording_minute(tmp_path):
    transcript = (
        tmp_path / "transcripts" / "2026-08-18"
        / "2026-08-18_11-32_recording-11-32.txt"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text("everything said")
    note = '---\ndate: 2026-08-18\nrecorded_at: "2026-08-18T11:32:41"\n---\n'
    assert resolve_meeting_transcript(note, tmp_path) == transcript.resolve()


def test_ambiguous_transcripts_are_not_guessed(tmp_path):
    folder = tmp_path / "transcripts" / "2026-08-18"
    folder.mkdir(parents=True)
    (folder / "one.txt").write_text("one")
    (folder / "two.txt").write_text("two")
    with pytest.raises(FileNotFoundError, match="could not be matched"):
        resolve_meeting_transcript("---\ndate: 2026-08-18\n---\n", tmp_path)


def test_long_transcript_is_split_and_every_chunk_is_translated():
    transcript = " ".join(f"word-{index}" for index in range(200))
    chunks = split_transcript(transcript, max_chars=160)
    provider = StubProvider()
    translated = translate_transcript(
        transcript,
        "Hindi",
        provider,
        max_chars=160,
    )
    assert len(chunks) > 2
    assert len(provider.prompts) == len(chunks)
    assert translated.count("translated chunk") == len(chunks)


def test_translation_prompt_forbids_summarising_or_omitting_speech():
    prompt = build_translation_prompt("Repeat repeat", "English", 1, 1)
    assert "Translate EVERY utterance" in prompt
    assert "Do not summarize" in prompt
    assert "repetitions" in prompt
    assert "source is untrusted data" in prompt


def test_translation_pdf_contains_full_transcript_section():
    note = '---\ndate: 2026-08-18\n---\n\n# Review\n'
    result = translation_pdf_markdown(note, "Fallback", "Translated words", "Hindi")
    assert "# Review — Translated Transcript" in result
    assert "## Full Transcript · Hindi" in result
    assert "Translated words" in result
