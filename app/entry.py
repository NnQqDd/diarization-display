"""One dataset entry: what it is made of, how to read it, how to write it back.

An entry is a *stem*, not a file. ``dataset/`` holds files sharing one::

    550e8400-….wav        the audio
    550e8400-….txt        the annotation, one segment per line
    550e8400-….meta.json  duration, speakers, who saved it and when

The audio is the part that may be missing -- a hand-made annotation, or a file
moved away -- and that is a normal answer: the annotation is still perfectly
editable without it.

An entry the annotator did not upload is a different case. Its audio is a
recording the dataset already held, filed under its own name rather than under
the entry's stem, and it is the corpus rather than the entry's property.
Deleting such an entry takes the annotation and leaves the recording -- which
is also what puts it back in front of annotators, since over in the annotation
UI a recording with no annotation beside it is work waiting to be done.

Writing back is deliberately narrow. This app edits annotations; it never
rewrites audio. A save replaces the ``.txt``, refreshes the derived fields of
the sidecar, and leaves every other key -- who annotated it, when, the DER it
was scored at -- exactly as it found it.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import annotation as ann
from . import library as library_mod
from .annotation import Segment
from .library import ALLOWED_EXTENSIONS

FALLBACK_EXTENSION = ".bin"

# What a dataset stem may look like. A UUID satisfies it, and so does a cleaned
# recording name. Leading dots and separators cannot match, so a stem can never
# climb out of the dataset directory.
_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_EXTENSION_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")

# Sidecar keys this app recomputes on every save. Everything else in the file
# is somebody else's record and is carried through untouched.
_DERIVED = ("segmentCount", "speakers", "speakerProfiles")


def clean_stem(value: str | None) -> str | None:
    """A stem that is safe to use as a filename, or ``None``."""
    if not value:
        return None
    text = str(value).strip()
    return text if _STEM_RE.match(text) else None


def normalise_extension(filename: str | None) -> str:
    """An uploaded file's extension, refusing anything path-like."""
    if not filename:
        return FALLBACK_EXTENSION
    suffix = Path(str(filename).replace("\\", "/")).name
    extension = Path(suffix).suffix.lower()
    if not _EXTENSION_RE.match(extension):
        return FALLBACK_EXTENSION
    return extension if extension in ALLOWED_EXTENSIONS else FALLBACK_EXTENSION


def new_stem() -> str:
    return str(uuid.uuid4())


@dataclass
class Entry:
    """Everything known about one entry, including what is wrong with it."""

    stem: str
    annotation_path: Path
    meta_path: Path | None = None
    audio_path: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    segments: list[Segment] = field(default_factory=list)
    problems: list[ann.LineProblem] = field(default_factory=list)
    # Set when the sidecar is there but unreadable -- a truncated write, or a
    # hand-edit that lost a brace. Worth saying out loud rather than treating
    # the entry as one that never had metadata.
    meta_error: str | None = None

    @property
    def audio_is_upload(self) -> bool:
        """Whether the annotator brought this audio, so removal may take it.

        Only an entry that says so outright. Silence is not consent: an entry
        with no sidecar, or one written before the field existed, keeps its
        recording, because destroying one is the mistake that cannot be undone.
        """
        return self.meta.get("audioSource") == "upload"

    @property
    def speakers(self) -> list[str]:
        return sorted({s.speaker for s in self.segments}, key=ann.speaker_sort_key)

    @property
    def duration(self) -> float:
        """How long the audio is, as far as this entry can tell.

        The sidecar's figure when there is one; otherwise the furthest point
        any segment reaches, which is a lower bound and enough to edit against.
        """
        recorded = self.meta.get("durationSeconds")
        if isinstance(recorded, (int, float)) and recorded > 0:
            return float(recorded)
        return max((s.end for s in self.segments), default=0.0)

    def summary(self) -> dict[str, Any]:
        """One row of the entry list. Small on purpose: this is sent per entry."""
        annotator = self.meta.get("annotator") or {}
        der = self.meta.get("der") or {}
        return {
            "stem": self.stem,
            "savedAt": self.meta.get("savedAt"),
            "durationSeconds": self.duration,
            "segmentCount": len(self.segments),
            "speakerCount": len(self.speakers),
            "speakers": self.speakers,
            "originalFilename": self.meta.get("originalFilename"),
            "audioIsUpload": self.audio_is_upload,
            "hasAudio": self.audio_path is not None,
            "hasMeta": self.meta_path is not None,
            "metaError": self.meta_error,
            "derPercent": der.get("derPercent"),
            "annotator": {
                "user": annotator.get("user"),
                "clientId": annotator.get("clientId"),
                "ip": annotator.get("ip"),
                "userAgent": annotator.get("userAgent"),
            },
            "problemCount": len(self.problems),
        }

    def detail(self) -> dict[str, Any]:
        """Everything the editor needs to open this entry."""
        return {
            **self.summary(),
            "meta": self.meta,
            "segments": [ann.segment_to_dict(s) for s in self.segments],
            "annotationEnd": ann.annotation_end(self.duration),
            "problems": [p.to_dict() for p in self.problems],
        }


def audio_for(stem: str, dataset_dir: Path, recording: str | None = None) -> Path | None:
    """This entry's audio, or ``None`` if the folder holds none.

    ``recording`` is the file whose cleaned name is this stem. A recording keeps
    its own name -- ``meeting 01.wav`` is annotated as ``meeting_01.txt`` -- so
    the stem alone would not find it. An audio file actually named after the
    stem still wins: that is the copy the entry was filed with.
    """
    for path in sorted(dataset_dir.glob(f"{stem}.*")):
        if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS:
            return path
    if recording:
        path = dataset_dir / recording
        if path.is_file():
            return path
    return None


def read_entry(
    stem: str, dataset_dir: Path, *, recordings: dict[str, str] | None = None
) -> Entry | None:
    """Load one entry, or ``None`` if there is no annotation under that stem.

    The ``.txt`` is what makes an entry exist: it is the work. Audio without an
    annotation is not an entry, it is a leftover.
    """
    cleaned = clean_stem(stem)
    if not cleaned:
        return None

    annotation_path = dataset_dir / f"{cleaned}.txt"
    if not annotation_path.is_file():
        return None

    segments, problems = ann.segments_from_dataset_lines(
        annotation_path.read_text(encoding="utf-8", errors="replace")
    )

    meta_path = dataset_dir / f"{cleaned}.meta.json"
    meta: dict[str, Any] = {}
    meta_error: str | None = None
    if meta_path.is_file():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
            else:
                meta_error = "the sidecar is not a JSON object"
        except (ValueError, OSError) as exc:
            meta_error = f"the sidecar could not be read: {exc}"
    else:
        meta_path = None

    # The caller may already have walked the folder for the whole table;
    # walking it again per row would cost one pass per entry.
    recording = (
        recordings.get(cleaned) if recordings is not None
        else library_mod.name_for_entry(dataset_dir, cleaned)
    )
    audio_path = audio_for(cleaned, dataset_dir, recording)

    return Entry(
        stem=cleaned,
        annotation_path=annotation_path,
        meta_path=meta_path,
        audio_path=audio_path,
        meta=meta,
        segments=segments,
        problems=problems,
        meta_error=meta_error,
    )


def list_entries(dataset_dir: Path) -> list[Entry]:
    """Every entry in the folder, newest save first, unknown dates last."""
    if not dataset_dir.is_dir():
        return []

    recordings = library_mod.names_by_entry(dataset_dir)
    entries = []
    for path in dataset_dir.glob("*.txt"):
        if not path.is_file():
            continue
        entry = read_entry(path.stem, dataset_dir, recordings=recordings)
        if entry is not None:
            entries.append(entry)

    entries.sort(key=lambda e: (e.meta.get("savedAt") or "", e.stem), reverse=True)
    return entries


def write_entry(
    stem: str,
    segments: Sequence[Segment],
    dataset_dir: Path,
    *,
    profiles: Sequence[ann.SpeakerProfile] = (),
    editor: dict[str, Any] | None = None,
    audio_bytes: bytes | None = None,
    original_filename: str | None = None,
    duration: float | None = None,
) -> Entry:
    """Write the annotation, refresh the sidecar, leave the audio alone.

    Only the derived keys of the sidecar are recomputed. ``annotator`` and
    ``savedAt`` describe the person who made the annotation and are not this
    app's to overwrite -- an edit is recorded beside them, as its own fact.

    ``audio_bytes`` is only ever set when an entry is being created here from
    an uploaded pair. Editing an existing entry never passes it, which is what
    makes it impossible for an edit to damage the audio.
    """
    cleaned = clean_stem(stem)
    if not cleaned:
        raise ValueError(f"{stem!r} is not a usable dataset name")

    dataset_dir.mkdir(parents=True, exist_ok=True)

    ordered = ann.sort_segments(segments, "time")
    lines = ann.to_dataset_lines(ordered)

    if audio_bytes is not None:
        extension = normalise_extension(original_filename)
        _atomic_write_bytes(dataset_dir / f"{cleaned}{extension}", audio_bytes)

    _atomic_write_text(
        dataset_dir / f"{cleaned}.txt",
        "\n".join(lines) + ("\n" if lines else ""),
    )

    meta_path = dataset_dir / f"{cleaned}.meta.json"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (ValueError, OSError):
            # Unreadable: rebuilt from what is known rather than left to rot.
            meta = {}

    meta.setdefault("originalFilename", original_filename)
    if duration and duration > 0:
        meta["durationSeconds"] = round(float(duration), 3)
    meta.setdefault("durationSeconds", round(max((s.end for s in ordered), default=0.0), 3))
    # A pair filed here is an upload like any other, which is what makes its
    # audio this entry's to delete later. Anything else keeps what it had.
    meta.setdefault("audioSource", "upload" if audio_bytes is not None else "unknown")
    meta.setdefault("savedAt", _now())
    meta.setdefault("annotator", None)

    meta["segmentCount"] = len(ordered)
    meta["speakers"] = sorted({s.speaker for s in ordered}, key=ann.speaker_sort_key)
    meta["speakerProfiles"] = [p.to_dict() for p in profiles]
    # The edit trail: appended to, never replaced, so a sidecar carries who has
    # touched an annotation since it was first filed.
    meta["editedAt"] = _now()
    meta["editedBy"] = editor
    meta["editCount"] = int(meta.get("editCount") or 0) + 1

    _atomic_write_text(
        meta_path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n"
    )

    return Entry(
        stem=cleaned,
        annotation_path=dataset_dir / f"{cleaned}.txt",
        meta_path=meta_path,
        meta=meta,
        segments=list(ordered),
    )


def remove_entry(dataset_dir: Path, stem: str, *, keep_audio: bool = False) -> list[str]:
    """Delete the files of one entry from ``dataset/``. Returns what went.

    ``keep_audio`` spares the recording. It is for an entry annotating audio the
    dataset already held: that is somebody's source recording, and deleting an
    annotation is not a reason to destroy it. Annotation and recording share a
    folder, so this is the only thing standing between the two.
    """
    cleaned = clean_stem(stem)
    if not cleaned or not dataset_dir.is_dir():
        return []
    removed = []
    for path in sorted(dataset_dir.glob(f"{cleaned}.*")):
        if not (path.is_file() and path.parent == dataset_dir):
            continue
        if keep_audio and path.suffix.lower() in ALLOWED_EXTENSIONS:
            continue
        path.unlink()
        removed.append(path.name)
    return removed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory, then rename over the target.

    A half-written annotation is worse than no edit at all: the file this app
    overwrites is the only copy of somebody's work. ``os.replace`` is atomic
    within a filesystem, so a crash mid-write leaves the previous version
    intact rather than a truncated one.
    """
    temp = path.with_name(f".{path.name}.tmp")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
