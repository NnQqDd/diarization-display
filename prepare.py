"""Turn an ``output.py`` folder into a dataset for diarization-display.

``--metadata-dir`` holds one ``<stem>.rttm`` per recording, and a ``<stem>.txt``
only when ``output.py`` was run with ``--text``. That text file is
``[hh:mm:ss.mmm - hh:mm:ss.mmm] [speaker]`` plus a transcript when one exists.
The display server instead lists a recording when ``<stem>.txt`` is::

    <speaker> <start> <end> <transcript>

This writes that annotation, and a ``<stem>.meta.json`` sidecar, into
``--dataset-dir``. Point the display server's ``DATASET_DIR`` there.
``--audio-dir`` is optional and is not walked. Each audio file directly inside
it whose name matches an RTTM stem is hardlinked into ``--dataset-dir`` as
``<stem><suffix>``. The two folders must be on the same filesystem.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import sys
from pathlib import Path


def seconds_to_hms(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60  # keep fractional part
    return f"{hours:02}:{minutes:02}:{secs:06.3f}"  # 3 decimal places


_TEXT_LINE = re.compile(
    r"^\[(\d+:\d{2}:\d{2}\.\d{3}) - (\d+:\d{2}:\d{2}\.\d{3})\] \[([^\]]*)\](?: (.*))?$"
)
_SPEAKER_NUMBER = re.compile(r"(\d+)\s*$")

# Suffixes diarization-display treats as playable. Anything else stays missing.
_DISPLAY_AUDIO = {
    ".wav", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".mp4",
    ".aac", ".wma", ".webm", ".aiff", ".aif", ".aifc", ".caf", ".amr", ".3gp",
}


def speaker_sort_key(speaker: str) -> tuple:
    """Order ``2`` before ``10``, then other labels alphabetically."""
    match = _SPEAKER_NUMBER.search(speaker)
    if match:
        return (0, float(match.group(1)), speaker.lower())
    return (1, 0.0, speaker.lower())


def read_rttm(path: Path) -> list[tuple[float, float, str]]:
    """Return ``(start, end, speaker)`` from one RTTM. Bad lines are skipped."""
    rows = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 8 or parts[0] != "SPEAKER":
            print(f"skip {path.name}:{lineno}: expected a SPEAKER row", file=sys.stderr)
            continue
        try:
            start = float(parts[3])
            end = start + float(parts[4])
        except ValueError:
            print(f"skip {path.name}:{lineno}: start or duration is not a number", file=sys.stderr)
            continue
        speaker = parts[7]
        if not speaker:
            print(f"skip {path.name}:{lineno}: speaker is empty", file=sys.stderr)
            continue
        if end <= start:
            print(f"skip {path.name}:{lineno}: end is not after start", file=sys.stderr)
            continue
        rows.append((start, end, speaker))
    rows.sort(key=lambda item: (item[0], item[1], speaker_sort_key(item[2])))
    return rows


def read_texts(path: Path) -> dict[tuple[str, str, str], list[str]]:
    """Map ``(start, end, speaker)`` clock text to transcripts, in file order."""
    found: dict[tuple[str, str, str], list[str]] = {}
    if not path.is_file():
        return found
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        match = _TEXT_LINE.match(line)
        if match is None:
            print(f"skip {path.name}:{lineno}: expected [start - end] [speaker]", file=sys.stderr)
            continue
        start, end, speaker, text = match.groups()
        transcript = " ".join((text or "").split())
        found.setdefault((start, end, speaker), []).append(transcript)
    return found


def annotation_line(start: float, end: float, speaker: str, text: str) -> str:
    """One display line. An empty transcript leaves the three time fields only."""
    line = f"{speaker} {seconds_to_hms(start)} {seconds_to_hms(end)}"
    if text:
        line += f" {text}"
    return line


def sidecar(stem: str, rows: list[tuple[float, float, str]], audio_name: str | None) -> dict:
    """Duration, speakers, and counts. There is no annotator record."""
    speakers = sorted({speaker for _start, _end, speaker in rows}, key=speaker_sort_key)
    duration = max((end for _start, end, _speaker in rows), default=0.0)
    return {
        "originalFilename": audio_name or stem,
        "audioSource": "unknown",
        "durationSeconds": round(duration, 3),
        "segmentCount": len(rows),
        "speakers": speakers,
        "speakerProfiles": [],
    }


def audio_index(audio_dir: Path) -> dict[str, list[Path]]:
    """Playable files directly in ``audio_dir``, keyed by the RTTM stem."""
    grouped: dict[str, list[Path]] = {}
    for path in sorted(audio_dir.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") or not path.is_file():
            continue
        if path.suffix.lower() not in _DISPLAY_AUDIO:
            continue
        grouped.setdefault("_".join(path.stem.split()), []).append(path)
    return grouped


def hardlink_audio(src: Path, dest: Path) -> None:
    """Hardlink ``src`` to ``dest``. A link that is already in place is kept."""
    if dest.is_symlink():
        dest.unlink()
    if dest.exists():
        src_stat = src.stat()
        dest_stat = dest.stat()
        if src_stat.st_dev == dest_stat.st_dev and src_stat.st_ino == dest_stat.st_ino:
            return
        raise SystemExit(f"{dest} already exists and is not a hard link of {src}")
    try:
        os.link(src.resolve(), dest)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise SystemExit(
                f"Cannot hardlink {src.name} into {dest.parent}: "
                "the audio folder and the dataset folder are on different filesystems"
            ) from exc
        raise SystemExit(f"Cannot hardlink {src} to {dest}: {exc}") from exc


def choose_audio(stem: str, grouped: dict[str, list[Path]]) -> Path | None:
    """The audio file for this RTTM stem, or ``None`` when the folder has none."""
    matches = grouped.get(stem) or []
    if not matches:
        print(f"missing audio for {stem}", file=sys.stderr)
        return None
    chosen = matches[0]
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        print(f"{stem} matches {names}; hardlinking {chosen.name}", file=sys.stderr)
    return chosen


def prepare_dir(
    metadata_dir: Path,
    dataset_dir: Path,
    audio_dir: Path | None = None,
) -> tuple[int, int, int, int]:
    """Write one annotation per RTTM. Return recording, segment, text, and link counts."""
    dataset_dir.mkdir(parents=True, exist_ok=True)
    grouped = audio_index(audio_dir) if audio_dir is not None else {}
    recordings = 0
    segments = 0
    with_text = 0
    linked = 0
    for path in sorted(item for item in metadata_dir.glob("*.rttm") if item.is_file()):
        rows = read_rttm(path)
        texts = read_texts(path.with_suffix(".txt"))
        lines = []
        used_text = False
        for start, end, speaker in rows:
            key = (seconds_to_hms(start), seconds_to_hms(end), speaker)
            queued = texts.get(key) or []
            transcript = queued.pop(0) if queued else ""
            if transcript:
                used_text = True
            lines.append(annotation_line(start, end, speaker, transcript))
        audio = choose_audio(path.stem, grouped) if audio_dir is not None else None
        if audio is not None:
            hardlink_audio(audio, dataset_dir / f"{path.stem}{audio.suffix}")
            linked += 1
        body = ("\n".join(lines) + "\n") if lines else ""
        (dataset_dir / f"{path.stem}.txt").write_text(body, encoding="utf-8")
        audio_name = audio.name if audio is not None else None
        (dataset_dir / f"{path.stem}.meta.json").write_text(
            json.dumps(sidecar(path.stem, rows, audio_name), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        recordings += 1
        segments += len(rows)
        if used_text:
            with_text += 1
    return recordings, segments, with_text, linked


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write a diarization-display dataset from an output.py metadata folder."
    )
    parser.add_argument(
        "--metadata-dir",
        required=True,
        help="Folder of <stem>.rttm files from output.py. A <stem>.txt is optional.",
    )
    parser.add_argument(
        "--dataset-dir",
        required=True,
        help="Folder the display server should use as DATASET_DIR.",
    )
    parser.add_argument(
        "--audio-dir",
        default=None,
        help="Folder of audio files, not walked. Matching stems are hardlinked into --dataset-dir.",
    )
    args = parser.parse_args()

    metadata_dir = Path(args.metadata_dir)
    if not metadata_dir.is_dir():
        raise SystemExit(f"Metadata directory not found: {metadata_dir}")
    dataset_dir = Path(args.dataset_dir)
    if dataset_dir.resolve() == metadata_dir.resolve():
        raise SystemExit("--dataset-dir must be a different folder from --metadata-dir")
    rttms = [path for path in metadata_dir.glob("*.rttm") if path.is_file()]
    if not rttms:
        raise SystemExit(f"No RTTM files in {metadata_dir}")
    audio_dir = None
    if args.audio_dir is not None:
        audio_dir = Path(args.audio_dir)
        if not audio_dir.is_dir():
            raise SystemExit(f"Audio directory not found: {audio_dir}")

    try:
        recordings, segments, with_text, linked = prepare_dir(metadata_dir, dataset_dir, audio_dir)
    except OSError as exc:
        raise SystemExit(f"Cannot write {dataset_dir}: {exc}") from exc
    print(f"recordings: {recordings}")
    print(f"segments:   {segments}")
    print(f"with text:  {with_text}")
    if audio_dir is not None:
        print(f"audio:      {linked} hardlinked, {recordings - linked} missing")
    print(f"wrote:      {dataset_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
