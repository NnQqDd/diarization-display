"""Segment model, ``hh:mm:ss`` time handling, validation and sorting.

This module is the single source of truth for what a valid annotation is. The
browser runs the same rules in ``static/js/annotation.js`` for instant feedback,
but nothing is trusted until it has been through ``validate_segments`` here --
the save endpoint re-validates every segment it is handed.

A copy of ``diarization-annotate/app/annotation.py``, plus the reader that file
has no use for: this app opens ``.txt`` files it did not write, so it needs the
inverse of :func:`to_dataset_lines`. Everything else is identical on purpose --
an annotation this app writes has to be one the annotation UI would accept.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Sequence

# hh:mm:ss, with hours unbounded and an optional fractional part. The plain
# hh:mm:ss form is the canonical one; the fraction is accepted so that
# millisecond-precision mode round-trips through the same parser.
_TIME_RE = re.compile(r"^(\d{1,3}):([0-5]?\d):([0-5]?\d)(?:\.(\d{1,6}))?$")

SORT_MODES = ("time", "speaker")

# The editing lattice. Annotation is millisecond-precise by default, matching
# the `hh:mm:ss.mmm` the UI shows; GRID_SECOND is what the "whole seconds"
# display mode snaps to.
GRID_MS = 0.001
GRID_SECOND = 1.0

# Two segments that differ by less than this are treated as touching rather than
# overlapping, and a time this close to an audio boundary is treated as on it.
# Well below the 1 ms display grid, so it can never mask a real edit.
EPSILON = 1e-6


class AnnotationError(ValueError):
    """Raised when a value cannot be interpreted as a valid annotation."""


@dataclass(frozen=True)
class Segment:
    speaker: str
    start: float
    end: float
    text: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


# Gender and language are free text on purpose: one speaker may switch between
# languages mid-recording, and the annotator is trusted to write "mixed" or list
# them. Long enough for that, short enough to stay a label.
_MAX_TRAIT = 64


@dataclass(frozen=True)
class SpeakerProfile:
    """What is known about one speaker, beyond their segments.

    Both fields are optional, exactly like a segment's transcript: an empty one
    is recorded as nothing rather than as an empty string.
    """

    speaker: str
    gender: str = ""
    language: str = ""

    def is_empty(self) -> bool:
        return not (self.gender or self.language)

    def to_dict(self) -> dict[str, str | None]:
        return {
            "speaker": self.speaker,
            "gender": self.gender or None,
            "language": self.language or None,
        }


@dataclass(frozen=True)
class ValidationIssue:
    index: int
    field: str  # "speaker" | "start" | "end" | "segment"
    message: str


def parse_time(value: Any) -> float:
    """Parse ``hh:mm:ss`` (or ``hh:mm:ss.mmm``) into seconds.

    Bare numbers are accepted too, so API payloads carrying raw float seconds do
    not need to be stringified first.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds != seconds or seconds in (float("inf"), float("-inf")):
            raise AnnotationError("time must be a finite number")
        return seconds

    if not isinstance(value, str):
        raise AnnotationError(f"expected a hh:mm:ss time, got {type(value).__name__}")

    text = value.strip()
    if not text:
        raise AnnotationError("time is empty")

    match = _TIME_RE.match(text)
    if not match:
        raise AnnotationError(f"{text!r} is not a valid hh:mm:ss time")

    hours, minutes, seconds, fraction = match.groups()
    total = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    if fraction:
        total += int(fraction) / (10 ** len(fraction))
    return float(total)


def format_time(seconds: float, *, milliseconds: bool = False) -> str:
    """Render seconds as ``hh:mm:ss``, or ``hh:mm:ss.mmm`` when asked.

    Negative input clamps to zero: an annotation never starts before the audio.
    """
    value = max(0.0, float(seconds))
    if milliseconds:
        total_ms = int(round(value * 1000))
        whole, millis = divmod(total_ms, 1000)
        tail = f".{millis:03d}"
    else:
        whole = int(round(value))
        tail = ""
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{tail}"


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    # Newlines would break the one-segment-per-line dataset format.
    return " ".join(value.split())


def _clean_speaker(value: Any) -> str:
    """Speaker labels are whitespace-free tokens -- the dataset format is
    space-delimited, so an embedded space would silently corrupt a line."""
    if value is None:
        raise AnnotationError("speaker is required")
    label = "_".join(str(value).split())
    if not label:
        raise AnnotationError("speaker is required")
    return label


def segment_from_payload(payload: Any) -> Segment:
    """Build a Segment from a JSON object.

    Accepts both shapes a segment arrives in: float seconds with an integer
    speaker, as a dataset file parses into, and the hh:mm:ss strings the editor
    sends back."""
    if not isinstance(payload, dict):
        raise AnnotationError("segment must be an object")

    if "start" not in payload or "end" not in payload:
        raise AnnotationError("segment needs both 'start' and 'end'")

    # A transcript is optional and has been spelled three different ways over
    # the life of this format; accept any of them.
    text = ""
    for key in ("transcript", "text", "transcription"):
        if payload.get(key):
            text = _clean_text(payload[key])
            break

    return Segment(
        speaker=_clean_speaker(payload.get("speaker")),
        start=parse_time(payload["start"]),
        end=parse_time(payload["end"]),
        text=text,
    )


def segments_from_payload(payload: Iterable[Any]) -> list[Segment]:
    out: list[Segment] = []
    for index, item in enumerate(payload):
        try:
            out.append(segment_from_payload(item))
        except AnnotationError as exc:
            raise AnnotationError(f"segment {index + 1}: {exc}") from exc
    return out


def speaker_profiles_from_payload(
    payload: Any, known_speakers: Iterable[str]
) -> list[SpeakerProfile]:
    """Read the speakers table into profiles, keeping only what is real.

    Accepts the UI's list of objects, or a ``{label: {...}}`` mapping. A label
    the annotation does not contain is dropped rather than saved: the speakers
    are derived from the segments, so anything else is a leftover from an edit
    (or a client making things up). Profiles with nothing filled in are left
    out entirely -- an empty field means "not recorded", not "recorded empty".
    """
    known = {s for s in known_speakers if s}
    if isinstance(payload, dict):
        items = [{"speaker": k, **(v if isinstance(v, dict) else {})}
                 for k, v in payload.items()]
    elif isinstance(payload, (list, tuple)):
        items = [item for item in payload if isinstance(item, dict)]
    else:
        items = []

    seen: dict[str, SpeakerProfile] = {}
    for item in items:
        raw_label = item.get("speaker") or item.get("label")
        if raw_label is None:
            continue
        label = "_".join(str(raw_label).split())
        if label not in known or label in seen:
            continue
        profile = SpeakerProfile(
            speaker=label,
            gender=_clean_trait(item.get("gender")),
            language=_clean_trait(item.get("language")),
        )
        if not profile.is_empty():
            seen[label] = profile

    return sorted(seen.values(), key=lambda p: speaker_sort_key(p.speaker))


def _clean_trait(value: Any) -> str:
    """One free-text trait: collapsed, printable, and bounded."""
    if value is None or isinstance(value, bool):
        return ""
    text = " ".join(str(value).split())
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:_MAX_TRAIT].strip()


def validate_segments(
    segments: Sequence[Segment],
    audio_start: float,
    audio_end: float,
    *,
    milliseconds: bool = True,
) -> list[ValidationIssue]:
    """Check ``audio_start <= start < end <= audio_end`` for every segment.

    Returns every issue found rather than raising on the first, so the UI can
    mark up all the bad rows at once. Overlap between *different* speakers is
    not an error -- two people talking at once is legitimate diarization
    output. A speaker overlapping *itself* is, see :func:`_self_overlap_issues`.

    ``milliseconds`` only affects how bounds are quoted back in the messages;
    it must match the precision the annotator is editing at, or the message
    names a bound that differs from the one being enforced.
    """
    issues: list[ValidationIssue] = []

    def bound(value: float) -> str:
        return format_time(value, milliseconds=milliseconds)

    if not (audio_end > audio_start):
        return [ValidationIssue(-1, "segment", "audio duration is unknown or empty")]

    for index, segment in enumerate(segments):
        if not segment.speaker:
            issues.append(ValidationIssue(index, "speaker", "Speaker is required"))

        if segment.start < audio_start - EPSILON:
            issues.append(
                ValidationIssue(
                    index,
                    "start",
                    f"Start is before the audio starts ({bound(audio_start)})",
                )
            )
        if segment.end > audio_end + EPSILON:
            issues.append(
                ValidationIssue(
                    index,
                    "end",
                    f"End is past the audio end ({bound(audio_end)})",
                )
            )
        if segment.start > audio_end + EPSILON:
            issues.append(
                ValidationIssue(
                    index,
                    "start",
                    f"Start is past the audio end ({bound(audio_end)})",
                )
            )
        if segment.end < audio_start - EPSILON:
            issues.append(
                ValidationIssue(
                    index,
                    "end",
                    f"End is before the audio starts ({bound(audio_start)})",
                )
            )
        if segment.end - segment.start <= EPSILON:
            issues.append(
                ValidationIssue(index, "end", "End must be strictly after start")
            )

    issues.extend(_self_overlap_issues(segments, bound))
    return issues


def _self_overlap_issues(
    segments: Sequence[Segment], bound: Any
) -> list[ValidationIssue]:
    """One speaker talking over themselves -- impossible, so an error.

    Two *different* speakers at once is real data and stays legal. The same
    label twice at the same instant cannot have happened, and it does not merely
    record something false: DER counts each overlapping segment separately, so
    the doubled seconds inflate the reference total and are then scored as
    missed detection. A duplicated 4 s row on an 8 s file moves DER from 0% to
    33%, which reads as a model error that never occurred.

    Segments are swept per speaker in start order against the furthest end seen
    so far, so a long segment containing several short ones flags all of them.
    Touching boundaries (``end == next start``) are not an overlap.
    """
    issues: list[ValidationIssue] = []

    by_speaker: dict[str, list[int]] = {}
    for index, segment in enumerate(segments):
        # A row that is already flagged for being empty or inverted has nothing
        # useful to say about overlap; it would just be a second red field.
        if segment.speaker and segment.end - segment.start > EPSILON:
            by_speaker.setdefault(segment.speaker, []).append(index)

    for speaker, indices in by_speaker.items():
        indices.sort(key=lambda i: (segments[i].start, segments[i].end))
        reach = -math.inf
        for index in indices:
            segment = segments[index]
            if segment.start < reach - EPSILON:
                issues.append(
                    ValidationIssue(
                        index,
                        "start",
                        f"{speaker} is already speaking until {bound(reach)}",
                    )
                )
            reach = max(reach, segment.end)

    return issues


def sort_segments(segments: Sequence[Segment], mode: str = "time") -> list[Segment]:
    """Chronological, or grouped by speaker then chronological within a speaker."""
    if mode not in SORT_MODES:
        raise AnnotationError(f"unknown sort mode {mode!r}")
    if mode == "speaker":
        return sorted(segments, key=lambda s: (speaker_sort_key(s.speaker), s.start, s.end))
    return sorted(segments, key=lambda s: (s.start, s.end, speaker_sort_key(s.speaker)))


def speaker_sort_key(speaker: str) -> tuple[int, float, str]:
    """Sort ``speaker_2`` before ``speaker_10`` by ordering on the trailing
    number when there is one, and alphabetically otherwise."""
    match = re.search(r"(\d+)\s*$", speaker)
    if match:
        return (0, float(match.group(1)), speaker.lower())
    return (1, 0.0, speaker.lower())


def annotation_end(duration: float, grid: float = GRID_MS) -> float:
    """The end of the annotation domain for a file of length ``duration``.

    Rounded *up* to the grid, so no speech at the tail becomes un-annotatable
    and the whole domain sits on the lattice -- a displayed boundary is then
    never a rounded stand-in for some other number. At the default 1 ms grid
    this moves the end by under a millisecond.
    """
    if grid <= 0:
        raise AnnotationError("grid must be positive")
    if duration <= 0:
        return 0.0
    steps = math.ceil(duration / grid - EPSILON)
    return round(steps * grid, 6)


def snap_segments(
    segments: Sequence[Segment],
    audio_start: float,
    audio_end: float,
    *,
    grid: float = GRID_MS,
) -> list[Segment]:
    """Round every boundary onto a ``grid``-second lattice and repair the result.

    The annotation is put on the editing lattice up front, so what the table
    shows is exactly what lands in the dataset file with no hidden remainder.
    At the default 1 ms grid that is a rounding of microseconds; at a 1 s grid
    it is a real quantisation. Segments that round down to nothing are widened
    to one grid step where the audio allows it, and dropped when it does not.
    """
    if grid <= 0:
        raise AnnotationError("grid must be positive")

    # Clamp to lattice points inside the domain, so clamping cannot itself
    # reintroduce an off-grid value at either end.
    low = math.ceil(audio_start / grid - EPSILON) * grid
    high = math.floor(audio_end / grid + EPSILON) * grid
    if high - low < grid - EPSILON:
        return []  # the domain cannot hold a single grid step

    out: list[Segment] = []
    for segment in segments:
        start = min(max(round(segment.start / grid) * grid, low), high)
        end = min(max(round(segment.end / grid) * grid, low), high)

        if end - start <= EPSILON:
            # Prefer growing forward; fall back to growing backward at the tail.
            if start + grid <= high + EPSILON:
                end = start + grid
            else:
                start = end - grid
        out.append(replace(segment, start=round(start, 6), end=round(end, 6)))
    return out


def to_dataset_lines(
    segments: Sequence[Segment], *, milliseconds: bool = True
) -> list[str]:
    """``<speaker> <start> <end> <transcript>`` per line, chronologically.

    The order is enforced here rather than trusted from the caller, so the UI's
    current sort mode can never leak into the saved file.

    The transcript is last because it is the only field that may contain
    spaces -- a reader splits three times and takes the remainder, so the text
    needs no quoting. Segments without one produce a three-field line with no
    trailing whitespace.
    """
    lines = []
    for segment in sort_segments(segments, "time"):
        start = format_time(segment.start, milliseconds=milliseconds)
        end = format_time(segment.end, milliseconds=milliseconds)
        line = f"{segment.speaker} {start} {end}"
        if segment.text:
            line += f" {segment.text}"
        lines.append(line)
    return lines


def segment_to_dict(segment: Segment, *, milliseconds: bool = True) -> dict[str, Any]:
    return {
        "speaker": segment.speaker,
        "start": segment.start,
        "end": segment.end,
        "startText": format_time(segment.start, milliseconds=milliseconds),
        "endText": format_time(segment.end, milliseconds=milliseconds),
        "text": segment.text,
    }


# --------------------------------------------------------------------------- #
# Reading a dataset .txt back in
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LineProblem:
    """A line that could not be read, kept so it can be shown rather than lost."""

    line_number: int
    text: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line_number, "text": self.text, "reason": self.reason}


def segments_from_dataset_lines(
    text: str,
) -> tuple[list[Segment], list[LineProblem]]:
    """Parse ``<speaker> <start> <end> <transcript>`` lines back into Segments.

    The exact inverse of :func:`to_dataset_lines`: three splits and the
    remainder, because the transcript is the only field that may hold spaces.

    A line that does not parse is *reported*, never skipped. Silently dropping
    one would show a shorter annotation than the file holds and then write that
    shortened version back over it -- data loss disguised as an edit. Blank
    lines are not a problem, they are nothing.
    """
    segments: list[Segment] = []
    problems: list[LineProblem] = []

    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue

        parts = line.split(" ", 3)
        if len(parts) < 3:
            problems.append(LineProblem(number, raw, "expected <speaker> <start> <end>"))
            continue

        speaker, start_text, end_text = parts[0], parts[1], parts[2]
        transcript = parts[3] if len(parts) > 3 else ""
        try:
            start = parse_time(start_text)
            end = parse_time(end_text)
        except AnnotationError as exc:
            problems.append(LineProblem(number, raw, str(exc)))
            continue

        cleaned = _clean_speaker(speaker)
        if not cleaned:
            problems.append(LineProblem(number, raw, "speaker is empty"))
            continue

        segments.append(
            Segment(speaker=cleaned, start=start, end=end, text=_clean_text(transcript))
        )

    return segments, problems
