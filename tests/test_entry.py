"""Reading and writing one entry: the file format, and what must survive a save."""

from __future__ import annotations

import json

from app import annotation as ann
from app import entry as entry_mod
from conftest import ANNOTATION, make_wav


# --------------------------------------------------------------- reading .txt


def test_a_line_round_trips_through_writer_and_reader():
    """The reader is the inverse of the annotation UI's writer, or it is wrong."""
    original = [
        ann.Segment("speaker_0", 1.0, 2.5, "hello there"),
        ann.Segment("speaker_1", 3.0, 4.0, ""),
    ]
    text = "\n".join(ann.to_dataset_lines(original))
    parsed, problems = ann.segments_from_dataset_lines(text)

    assert not problems
    assert parsed == original


def test_the_transcript_keeps_its_spaces():
    """Three splits and the remainder -- the reason the text is the last field."""
    parsed, _ = ann.segments_from_dataset_lines(
        "speaker_0 00:00:00.000 00:00:01.000 well now, that is a whole sentence\n"
    )
    assert parsed[0].text == "well now, that is a whole sentence"


def test_an_unreadable_line_is_reported_not_skipped():
    """Dropping it silently would show a short annotation and then save it."""
    parsed, problems = ann.segments_from_dataset_lines(
        "speaker_0 00:00:01.000 00:00:02.000 fine\n"
        "this line is nonsense\n"
        "speaker_1 nope 00:00:03.000\n"
        "\n"
    )
    assert len(parsed) == 1
    assert [p.line_number for p in problems] == [2, 3]


# --------------------------------------------------------------- finding audio


def test_audio_is_found_beside_the_annotation(dataset, audios, uploaded):
    path, source = entry_mod.audio_for(uploaded, dataset, audios)
    assert source == "dataset" and path.name == f"{uploaded}.wav"


def test_audio_falls_back_to_the_audios_folder(dataset, audios, from_shelf):
    """The whole point: an entry from the shelf keeps no copy of its own."""
    assert not list(dataset.glob("*.wav"))
    path, source = entry_mod.audio_for(from_shelf, dataset, audios)
    assert source == "audios" and path.name == "meeting 01.wav"


def test_a_dataset_copy_wins_over_a_shelf_file_of_the_same_name(dataset, audios, from_shelf):
    (dataset / f"{from_shelf}.wav").write_bytes(make_wav())
    _, source = entry_mod.audio_for(from_shelf, dataset, audios)
    assert source == "dataset"


def test_an_entry_with_no_audio_anywhere_is_still_an_entry(dataset, audios):
    (dataset / "orphan.txt").write_text(ANNOTATION)
    entry = entry_mod.read_entry("orphan", dataset, audios)
    assert entry is not None
    assert entry.audio_path is None and entry.audio_source == "missing"
    assert len(entry.segments) == 2


def test_audio_without_an_annotation_is_not_an_entry(dataset, audios):
    (dataset / "leftover.wav").write_bytes(make_wav())
    assert entry_mod.read_entry("leftover", dataset, audios) is None
    assert entry_mod.list_entries(dataset, audios) == []


# --------------------------------------------------------------- writing back


def test_saving_never_touches_the_audio(dataset, audios, uploaded):
    before = (dataset / f"{uploaded}.wav").read_bytes()
    entry_mod.write_entry(
        uploaded, [ann.Segment("speaker_0", 0.5, 1.5, "edited")], dataset
    )
    assert (dataset / f"{uploaded}.wav").read_bytes() == before


def test_saving_a_shelf_entry_leaves_the_shelf_alone(dataset, audios, from_shelf):
    before = (audios / "meeting 01.wav").read_bytes()
    entry_mod.write_entry(
        from_shelf, [ann.Segment("speaker_0", 0.5, 1.5, "")], dataset
    )
    assert (audios / "meeting 01.wav").read_bytes() == before
    assert not list(dataset.glob("*.wav"))


def test_the_original_annotator_is_not_overwritten_by_an_edit(dataset, audios, uploaded):
    """An edit is a new fact recorded beside the old one, not a replacement."""
    entry_mod.write_entry(
        uploaded,
        [ann.Segment("speaker_0", 0.5, 1.5, "")],
        dataset,
        editor={"user": "duy", "clientId": None, "ip": "127.0.0.1", "userAgent": "t"},
    )
    meta = json.loads((dataset / f"{uploaded}.meta.json").read_text())

    assert meta["annotator"]["clientId"] == "7fee2aa2-cab0-44ce-8b40-741850d738fd"
    assert meta["savedAt"] == "2026-08-20T10:00:00+00:00"
    # A key this app never reads is still a key it must not lose.
    assert meta["diarizer"] == "pyannote"
    assert meta["editedBy"]["user"] == "duy"
    assert meta["editCount"] == 1


def test_edits_are_counted_not_reset(dataset, audios, uploaded):
    for _ in range(3):
        entry_mod.write_entry(uploaded, [ann.Segment("s", 0.0, 1.0, "")], dataset)
    meta = json.loads((dataset / f"{uploaded}.meta.json").read_text())
    assert meta["editCount"] == 3


def test_derived_fields_follow_the_segments(dataset, audios, uploaded):
    entry_mod.write_entry(
        uploaded,
        [ann.Segment("speaker_9", 0.0, 1.0, ""), ann.Segment("speaker_2", 2.0, 3.0, "")],
        dataset,
        profiles=[ann.SpeakerProfile("speaker_2", gender="female", language="")],
    )
    meta = json.loads((dataset / f"{uploaded}.meta.json").read_text())
    assert meta["segmentCount"] == 2
    # speaker_2 before speaker_9: the numeric sort, same as everywhere else.
    assert meta["speakers"] == ["speaker_2", "speaker_9"]
    # A blank field is recorded as nothing, exactly as the annotation UI does.
    assert meta["speakerProfiles"] == [
        {"speaker": "speaker_2", "gender": "female", "language": None}
    ]


def test_the_annotation_is_written_chronologically(dataset, audios, uploaded):
    entry_mod.write_entry(
        uploaded,
        [ann.Segment("speaker_1", 5.0, 6.0, ""), ann.Segment("speaker_0", 1.0, 2.0, "")],
        dataset,
    )
    lines = (dataset / f"{uploaded}.txt").read_text().splitlines()
    assert lines[0].startswith("speaker_0") and lines[1].startswith("speaker_1")


def test_a_failed_write_does_not_truncate_the_previous_one(dataset, audios, uploaded, monkeypatch):
    """The file being overwritten is the only copy of somebody's work."""
    before = (dataset / f"{uploaded}.txt").read_text()

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(entry_mod.os, "replace", boom)
    try:
        entry_mod.write_entry(uploaded, [ann.Segment("s", 0.0, 1.0, "")], dataset)
    except OSError:
        pass
    assert (dataset / f"{uploaded}.txt").read_text() == before
    assert not list(dataset.glob(".*.tmp"))


# ------------------------------------------------------------------- removal


def test_removing_takes_the_dataset_files_and_nothing_else(dataset, audios, from_shelf):
    removed = entry_mod.remove_entry(dataset, from_shelf)
    assert sorted(removed) == ["meeting_01.meta.json", "meeting_01.txt"]
    assert not list(dataset.iterdir())
    assert (audios / "meeting 01.wav").is_file()


def test_removal_cannot_climb_out_of_the_dataset(dataset, audios, tmp_path):
    victim = tmp_path / "keepme.txt"
    victim.write_text("not yours")
    assert entry_mod.remove_entry(dataset, "../keepme") == []
    assert victim.is_file()
