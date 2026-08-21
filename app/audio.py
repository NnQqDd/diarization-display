"""Audio probing.

Cut down from ``diarization-annotate/app/audio.py``: this app never sends audio
anywhere, so the 16 kHz decode that exists there for DiseService is gone and
only the measuring half remains. All it needs to know about a file is how long
it is -- that is the bound every segment is validated against.

Without ffmpeg it can still measure a WAV from its own header, which is why an
uploaded pair is not simply refused on a machine that has no ffmpeg.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

class AudioError(RuntimeError):
    """Raised when an upload cannot be probed or decoded."""


@dataclass(frozen=True)
class AudioInfo:
    duration: float
    sample_rate: int
    channels: int
    codec: str


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(command: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AudioError(f"{command[0]} is not installed") from exc


# ffmpeg and ffprobe quote the file they were given, which here is a scratch
# copy under a temporary directory. That message is shown to whoever uploaded
# the file, and the server's filesystem is none of their business.
_TEMP_PATH_RE = re.compile(r"\S*/diar-annotate-[^\s:]+:?\s*")


def _tail(stderr: bytes, lines: int = 3) -> str:
    text = stderr.decode("utf-8", "replace").strip().splitlines()
    joined = " | ".join(text[-lines:]) if text else "no output"
    return _TEMP_PATH_RE.sub("", joined).strip() or "no output"


def probe(path: Path) -> AudioInfo:
    """Read duration/sample rate/channels via ffprobe."""
    result = _run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels,codec_name:format=duration",
            "-of", "json",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise AudioError(f"ffprobe failed: {_tail(result.stderr)}")

    try:
        payload = json.loads(result.stdout or b"{}")
        streams = payload.get("streams") or []
        if not streams:
            raise AudioError("the file contains no audio stream")
        stream = streams[0]
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
        info = AudioInfo(
            duration=duration,
            sample_rate=int(stream.get("sample_rate") or 0),
            channels=int(stream.get("channels") or 0),
            codec=str(stream.get("codec_name") or "unknown"),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise AudioError(f"could not read audio metadata: {exc}") from exc

    # A duration of 0 is not fatal here: formats without a container duration
    # get measured from the WAV header in `measure` instead.
    return info


def measure(data: bytes, suffix: str) -> AudioInfo:
    """How long an uploaded file is, plus what ffprobe says about its format.

    ffmpeg needs a seekable input for container formats such as MP4/M4A, so the
    upload is staged to a temp file rather than piped.
    """
    if not data:
        raise AudioError("the uploaded file is empty")

    if not ffmpeg_available():
        # Degrade rather than fail: a WAV can be measured from its own header,
        # which covers the common case on a machine with no ffmpeg.
        return AudioInfo(_wav_duration(data), 0, 0, "wav")

    suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ".bin"
    with tempfile.TemporaryDirectory(prefix="diar-display-") as tmp:
        staged = Path(tmp) / f"upload{suffix}"
        staged.write_bytes(data)
        info = probe(staged)

    if info.duration <= 0.0:
        # Raw or streamed containers carry no duration in the container; a WAV
        # still has one in its header.
        try:
            info = AudioInfo(
                duration=_wav_duration(data),
                sample_rate=info.sample_rate,
                channels=info.channels,
                codec=info.codec,
            )
        except AudioError:
            pass
    if info.duration <= 0.0:
        raise AudioError("audio duration is zero")
    return info


async def measure_async(data: bytes, suffix: str) -> AudioInfo:
    """``measure`` off the event loop -- ffprobe on a long file is not instant."""
    return await asyncio.to_thread(measure, data, suffix)


def _wav_duration(data: bytes) -> float:
    """Duration straight from a RIFF header, for the no-ffmpeg fallback."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError(
            "ffmpeg is not installed, so only WAV uploads can be handled"
        )
    byte_rate = 0
    data_size = 0
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        (chunk_size,) = struct.unpack("<I", data[offset + 4:offset + 8])
        body = offset + 8
        if chunk_id == b"fmt " and body + 16 <= len(data):
            (byte_rate,) = struct.unpack("<I", data[body + 8:body + 12])
        elif chunk_id == b"data":
            data_size = min(chunk_size, len(data) - body)
            break
        offset = body + chunk_size + (chunk_size & 1)

    if not byte_rate or not data_size:
        raise AudioError("could not read the WAV header")
    return data_size / float(byte_rate)
