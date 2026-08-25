"""The HTTP surface: listing, opening, editing, deleting, uploading a pair."""

from __future__ import annotations

import json

from conftest import ANNOTATION, make_wav


def _payload(segments, **extra):
    return {"segments": segments, "audioStart": 0, "audioEnd": 6.0, **extra}


SEGMENT = {"speaker": "speaker_0", "start": "00:00:01", "end": "00:00:02"}


# ------------------------------------------------------------------- config


def test_config_says_what_it_is_pointed_at(client, dataset):
    body = client.get("/api/config").json()
    assert body["dataset"]["path"] == str(dataset)
    assert body["dataset"]["ok"] is True


# ------------------------------------------------------------------ listing


def test_an_empty_dataset_is_an_empty_list_not_an_error(client):
    body = client.get("/api/entries").json()
    assert body["count"] == 0 and body["entries"] == []


def test_the_list_carries_what_a_row_needs(client, uploaded):
    row = client.get("/api/entries").json()["entries"][0]
    assert row["stem"] == uploaded
    assert row["segmentCount"] == 2
    assert row["speakers"] == ["speaker_0", "speaker_1"]
    assert row["hasAudio"] is True and row["audioIsUpload"] is True
    # The whole reason this app exists rather than a `ls`.
    assert row["annotator"]["ip"] == "203.0.113.7"
    assert row["annotator"]["clientId"].startswith("7fee2aa2")


def test_the_list_flags_an_entry_whose_audio_is_gone(client, dataset):
    (dataset / "orphan.txt").write_text(ANNOTATION)
    body = client.get("/api/entries").json()
    assert body["missingAudio"] == 1
    assert body["entries"][0]["hasAudio"] is False


def test_newest_first(client, dataset, uploaded):
    (dataset / "later.txt").write_text(ANNOTATION)
    (dataset / "later.meta.json").write_text(
        json.dumps({"savedAt": "2026-08-21T10:00:00+00:00"})
    )
    stems = [e["stem"] for e in client.get("/api/entries").json()["entries"]]
    assert stems[0] == "later"


# ------------------------------------------------------------------ opening


def test_opening_an_entry_gives_the_segments_and_the_sidecar(client, uploaded):
    body = client.get(f"/api/entries/{uploaded}").json()
    assert [s["speaker"] for s in body["segments"]] == ["speaker_0", "speaker_1"]
    assert body["segments"][0]["text"] == "hello there"
    # The sidecar goes out as it is on disk, unknown keys and all.
    assert body["meta"]["diarizer"] == "pyannote"
    assert body["problems"] == []


def test_opening_something_that_is_not_there_is_a_404(client):
    assert client.get("/api/entries/nope").status_code == 404


def test_the_audio_comes_from_the_dataset_when_it_is_there(client, uploaded):
    response = client.get(f"/api/entries/{uploaded}/audio")
    assert response.status_code == 200
    assert response.content[:4] == b"RIFF"


def test_a_recording_plays_under_its_own_name(client, from_library):
    """Its stem is the cleaned name, so a stem glob alone would never find it."""
    response = client.get(f"/api/entries/{from_library}/audio")
    assert response.status_code == 200 and response.content[:4] == b"RIFF"


def test_an_entry_says_whether_its_audio_is_its_own(client, from_library, uploaded):
    """What decides, on delete, between an undo and a destroyed recording."""
    rows = {e["stem"]: e for e in client.get("/api/entries").json()["entries"]}
    assert rows[from_library]["audioIsUpload"] is False
    assert rows[uploaded]["audioIsUpload"] is True


def test_an_entry_with_no_audio_says_so_rather_than_500ing(client, dataset):
    (dataset / "orphan.txt").write_text(ANNOTATION)
    response = client.get("/api/entries/orphan/audio")
    assert response.status_code == 404
    assert "no audio" in response.json()["detail"].lower()


def test_a_line_that_will_not_parse_is_reported_to_the_editor(client, dataset):
    (dataset / "messy.txt").write_text(ANNOTATION + "garbage\n")
    body = client.get("/api/entries/messy").json()
    assert len(body["segments"]) == 2
    assert body["problems"][0]["line"] == 3


# ------------------------------------------------------------------ editing


def test_editing_rewrites_the_annotation(client, dataset, uploaded):
    response = client.put(f"/api/entries/{uploaded}", json=_payload([
        {"speaker": "alice", "start": "00:00:00.500", "end": "00:00:01.500",
         "transcript": "rewritten"},
    ]))
    assert response.status_code == 200
    assert (dataset / f"{uploaded}.txt").read_text() == (
        "alice 00:00:00.500 00:00:01.500 rewritten\n"
    )


def test_editing_leaves_the_audio_untouched(client, dataset, uploaded):
    before = (dataset / f"{uploaded}.wav").read_bytes()
    client.put(f"/api/entries/{uploaded}", json=_payload([SEGMENT]))
    assert (dataset / f"{uploaded}.wav").read_bytes() == before


def test_editing_a_library_entry_never_reaches_the_recording(client, dataset, from_library):
    before = (dataset / "meeting 01.wav").read_bytes()
    assert client.put(f"/api/entries/{from_library}", json=_payload([SEGMENT])).status_code == 200
    assert (dataset / "meeting 01.wav").read_bytes() == before
    assert sorted(p.name for p in dataset.iterdir()) == [
        "meeting 01.wav", "meeting_01.meta.json", "meeting_01.txt",
    ]


def test_an_invalid_annotation_is_refused_and_nothing_is_written(client, dataset, uploaded):
    before = (dataset / f"{uploaded}.txt").read_text()
    response = client.put(f"/api/entries/{uploaded}", json=_payload([
        {"speaker": "speaker_0", "start": "00:00:02", "end": "00:00:01"},
    ]))
    assert response.status_code == 422
    assert (dataset / f"{uploaded}.txt").read_text() == before


def test_one_speaker_cannot_be_made_to_overlap_itself(client, uploaded):
    """The same contradiction the annotation UI refuses -- refused here too."""
    response = client.put(f"/api/entries/{uploaded}", json=_payload([
        {"speaker": "speaker_0", "start": "00:00:01", "end": "00:00:04"},
        {"speaker": "speaker_0", "start": "00:00:02", "end": "00:00:05"},
    ]))
    assert response.status_code == 422


def test_a_segment_past_the_end_of_the_audio_is_refused(client, uploaded):
    """The claimed bound cannot widen the real one: the sidecar says 6 seconds."""
    response = client.put(f"/api/entries/{uploaded}", json={
        "segments": [{"speaker": "s", "start": "00:00:01", "end": "00:01:00"}],
        "audioStart": 0,
        "audioEnd": 3600,
    })
    assert response.status_code == 422


def test_saving_nothing_is_refused(client, uploaded):
    assert client.put(f"/api/entries/{uploaded}", json=_payload([])).status_code == 400


def test_editing_an_entry_that_does_not_exist_is_a_404(client):
    """PUT edits; it never quietly files a new entry under a mistyped name."""
    assert client.put("/api/entries/nope", json=_payload([SEGMENT])).status_code == 404


# ------------------------------------------------------------------ deleting


def test_deleting_takes_the_whole_entry(client, dataset, uploaded):
    response = client.delete(f"/api/entries/{uploaded}")
    assert response.status_code == 200
    assert sorted(response.json()["removed"]) == [
        f"{uploaded}.meta.json", f"{uploaded}.txt", f"{uploaded}.wav",
    ]
    assert not list(dataset.iterdir())


def test_deleting_a_library_entry_keeps_the_recording(client, dataset, from_library):
    """The audio is the corpus. Deleting the annotation puts it back to work."""
    response = client.delete(f"/api/entries/{from_library}")
    assert response.status_code == 200
    assert response.json()["keptAudio"] is True
    assert sorted(response.json()["removed"]) == [
        "meeting_01.meta.json", "meeting_01.txt",
    ]
    assert (dataset / "meeting 01.wav").is_file()


def test_deleting_an_entry_named_after_its_file_keeps_the_recording(client, dataset):
    """The real dataset's recordings are ``<uuid>.wav``: stem and name agree.

    That is when the stem glob reaches the audio, so it is the case the guard
    has to hold for. Without it, deleting an annotation destroys the source.
    """
    (dataset / "rec.wav").write_bytes(make_wav())
    (dataset / "rec.txt").write_text(ANNOTATION)

    response = client.delete("/api/entries/rec")
    assert response.json()["keptAudio"] is True
    assert response.json()["removed"] == ["rec.txt"]
    assert (dataset / "rec.wav").is_file()


def test_deleting_something_that_is_not_there_is_a_404(client):
    assert client.delete("/api/entries/nope").status_code == 404


# ------------------------------------------------------------- uploaded pair


def test_an_uploaded_txt_is_read_into_segments(client):
    response = client.post(
        "/api/parse", files={"file": ("ann.txt", ANNOTATION, "text/plain")}
    )
    body = response.json()
    assert [s["speaker"] for s in body["segments"]] == ["speaker_0", "speaker_1"]
    assert body["problems"] == []


def test_parsing_reports_the_lines_it_could_not_read(client):
    body = client.post(
        "/api/parse", files={"file": ("ann.txt", ANNOTATION + "junk\n", "text/plain")}
    ).json()
    assert len(body["segments"]) == 2 and len(body["problems"]) == 1


def test_a_pair_can_be_filed_as_a_new_entry(client, dataset):
    response = client.post("/api/entries", data={
        "payload": json.dumps(_payload([SEGMENT])),
    }, files={"file": ("outside.wav", make_wav(), "audio/wav")})

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True

    stem = body["stem"]
    # An upload is the only copy of its audio, so it is copied in -- the same
    # rule the annotation UI applies.
    assert sorted(p.name for p in dataset.iterdir()) == sorted([
        f"{stem}.wav", f"{stem}.txt", f"{stem}.meta.json",
    ])
    meta = json.loads((dataset / f"{stem}.meta.json").read_text())
    assert meta["originalFilename"] == "outside.wav"
    # An upload, so its audio is this entry's to delete along with it.
    assert meta["audioSource"] == "upload"


def test_filing_a_pair_twice_makes_two_entries(client, dataset):
    """No id is claimed for an uploaded pair, so each one is its own entry."""
    for _ in range(2):
        client.post("/api/entries", data={"payload": json.dumps(_payload([SEGMENT]))},
                    files={"file": ("a.wav", make_wav(), "audio/wav")})
    assert len(list(dataset.glob("*.txt"))) == 2


def test_a_pair_whose_annotation_is_invalid_is_not_filed(client, dataset):
    response = client.post("/api/entries", data={
        "payload": json.dumps(_payload([
            {"speaker": "s", "start": "00:00:05", "end": "00:00:02"},
        ])),
    }, files={"file": ("a.wav", make_wav(), "audio/wav")})
    assert response.status_code == 422
    assert not list(dataset.iterdir())
