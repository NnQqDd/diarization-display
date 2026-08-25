"""A dataset on disk, and a client pointed at it.

Every test gets its own ``dataset/`` under ``tmp_path``, so nothing here can
reach the real one -- this app edits and deletes annotations, and a test that
ran against somebody's work would be a bad way to find out.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main as main_mod           # noqa: E402
from app.config import Settings            # noqa: E402


def make_wav(seconds: float = 6.0, rate: int = 8000) -> bytes:
    """A silent mono 16-bit WAV, long enough to hold the fixture segments."""
    frames = int(seconds * rate)
    data = b"\x00\x00" * frames
    header = (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data" + struct.pack("<I", len(data))
    )
    return header + data


ANNOTATION = (
    "speaker_0 00:00:01.000 00:00:02.500 hello there\n"
    "speaker_1 00:00:03.000 00:00:04.000\n"
)

META = {
    "originalFilename": "meeting 01.wav",
    "audioSource": "upload",
    "durationSeconds": 6.0,
    "segmentCount": 2,
    "speakers": ["speaker_0", "speaker_1"],
    "speakerProfiles": [{"speaker": "speaker_0", "gender": "male"}],
    # A key this app has no concept of. It is here so the tests can prove that
    # editing an entry carries a stranger's field through untouched.
    "diarizer": "pyannote",
    "der": None,
    "savedAt": "2026-08-20T10:00:00+00:00",
    "annotator": {
        "user": None,
        "clientId": "7fee2aa2-cab0-44ce-8b40-741850d738fd",
        "ip": "203.0.113.7",
        "userAgent": "Mozilla/5.0",
    },
}


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    path = tmp_path / "dataset"
    path.mkdir()
    return path


@pytest.fixture
def client(monkeypatch, tmp_path: Path, dataset: Path) -> TestClient:
    settings = Settings(
        annotate_path=tmp_path,
        dataset_dir=dataset,
        static_dir=Path(__file__).resolve().parent.parent / "static",
        max_upload_bytes=16 * 1024 * 1024,
    )
    monkeypatch.setattr(main_mod, "settings", settings)
    return TestClient(main_mod.app)


@pytest.fixture
def uploaded(dataset: Path) -> str:
    """An entry the annotation UI would have written from an upload."""
    stem = "550e8400-e29b-41d4-a716-446655440000"
    (dataset / f"{stem}.wav").write_bytes(make_wav())
    (dataset / f"{stem}.txt").write_text(ANNOTATION)
    (dataset / f"{stem}.meta.json").write_text(json.dumps(META))
    return stem


@pytest.fixture
def from_library(dataset: Path) -> str:
    """An entry annotating a recording the dataset already held.

    The recording keeps its own name; the annotation is filed under the cleaned
    stem. That mismatch is the interesting part: a stem glob finds the
    annotation and not the audio, and both halves have to work.
    """
    (dataset / "meeting 01.wav").write_bytes(make_wav())
    stem = "meeting_01"
    (dataset / f"{stem}.txt").write_text(ANNOTATION)
    (dataset / f"{stem}.meta.json").write_text(
        json.dumps({**META, "audioSource": "dataset"})
    )
    return stem
