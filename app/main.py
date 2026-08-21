"""HTTP layer: the viewer/editor UI and the JSON API it talks to.

Route map
---------
``GET  /``                       the UI
``GET  /api/config``             where the dataset is, and whether it is there
``GET  /api/entries``            every entry in dataset/, newest first
``GET  /api/entries/{stem}``     one entry: metadata, segments, parse problems
``GET  /api/entries/{stem}/audio`` its audio -- from dataset/, else audios/
``PUT  /api/entries/{stem}``     replace that entry's annotation
``DELETE /api/entries/{stem}``   take the entry out of dataset/
``POST /api/parse``              read an uploaded .txt into segments
``POST /api/entries``            file an uploaded audio + .txt pair as an entry

This app is for whoever runs the annotation effort, not for annotators. It
shows the sidecar in full, IP addresses and user agents included, and it can
overwrite and delete other people's work -- so it binds to localhost and is
meant to stay there. Nothing here belongs on a public link.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import annotation as ann
from . import audio as audio_mod
from . import entry as entry_mod
from . import identity as identity_mod
from .config import settings

ANNOTATION_GRID = ann.GRID_MS

app = FastAPI(title="Diarization Dataset Viewer", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")


@app.exception_handler(ann.AnnotationError)
async def _annotation_error(_request: Request, exc: ann.AnnotationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    return HTMLResponse((settings.static_dir / "index.html").read_text(encoding="utf-8"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(settings.static_dir / "favicon.svg", media_type="image/svg+xml")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@app.get("/api/config")
async def config() -> dict[str, Any]:
    """What this app is pointed at, and whether it is actually there.

    The paths *are* part of the answer here, unlike in the annotation UI: the
    person reading this screen is the one who set ANNOTATEPATH, and a viewer
    that hides which folder it opened is a viewer you cannot debug.
    """
    return {
        "annotatePath": str(settings.annotate_path),
        "dataset": {"path": str(settings.dataset_dir), "ok": settings.dataset_ok},
        "audios": {"path": str(settings.audios_dir), "ok": settings.audios_ok},
        "ffmpeg": audio_mod.ffmpeg_available(),
        "annotationGrid": ANNOTATION_GRID,
        "maxUploadMb": settings.max_upload_bytes // (1024 * 1024),
    }


# --------------------------------------------------------------------------- #
# Entries
# --------------------------------------------------------------------------- #


@app.get("/api/entries")
async def list_entries() -> dict[str, Any]:
    """Every entry, with enough of each to fill a row and to filter on."""
    entries = await asyncio.to_thread(
        entry_mod.list_entries, settings.dataset_dir, settings.audios_dir
    )
    summaries = [e.summary() for e in entries]
    return {
        "count": len(summaries),
        "datasetOk": settings.dataset_ok,
        "totalSeconds": round(sum(s["durationSeconds"] for s in summaries), 3),
        "missingAudio": sum(1 for s in summaries if not s["hasAudio"]),
        "entries": summaries,
    }


def _load(stem: str) -> entry_mod.Entry:
    entry = entry_mod.read_entry(stem, settings.dataset_dir, settings.audios_dir)
    if entry is None:
        raise HTTPException(status_code=404, detail="No entry by that name.")
    return entry


@app.get("/api/entries/{stem}")
async def get_entry(stem: str) -> dict[str, Any]:
    return await asyncio.to_thread(lambda: _load(stem).detail())


@app.get("/api/entries/{stem}/audio")
async def get_entry_audio(stem: str) -> FileResponse:
    """The entry's audio, wherever it turned out to live.

    ``FileResponse`` answers range requests, which is what makes seeking in a
    long recording cost one request instead of a re-download.
    """
    entry = _load(stem)
    if entry.audio_path is None:
        raise HTTPException(
            status_code=404,
            detail="This entry has no audio: none in dataset, and none on the shelf.",
        )
    return FileResponse(entry.audio_path)


@app.put("/api/entries/{stem}")
async def update_entry(stem: str, request: Request) -> dict[str, Any]:
    """Replace one entry's annotation with the edited version.

    The entry has to exist already. Creating one is what ``POST /api/entries``
    is for, and keeping them apart means a typo in a name cannot quietly file a
    new entry that nobody will ever look for.
    """
    existing = _load(stem)
    body = await _json_body(request)

    segments = ann.segments_from_payload(body.get("segments") or [])
    if not segments:
        raise HTTPException(status_code=400, detail="There are no segments to save.")

    # The same bound the annotation UI validates against, so an annotation this
    # app writes is one that app would accept.
    audio_end = _audio_end(body, existing.duration)
    _reject_invalid(segments, audio_end)

    profiles = ann.speaker_profiles_from_payload(
        body.get("speakers"), (s.speaker for s in segments)
    )
    editor = identity_mod.identify(request, body.get("clientId")).to_dict()

    saved = await asyncio.to_thread(
        entry_mod.write_entry,
        stem,
        segments,
        settings.dataset_dir,
        profiles=profiles,
        editor=editor,
    )
    return {
        "stem": saved.stem,
        "segmentCount": len(saved.segments),
        "editCount": saved.meta.get("editCount"),
        "created": False,
    }


@app.post("/api/entries")
async def create_entry(
    request: Request,
    payload: str = Form(...),
    file: UploadFile = File(...),
    clientId: str | None = Form(None),
) -> dict[str, Any]:
    """File an uploaded audio + annotation pair as a new dataset entry.

    The audio is copied in, because this is now the only copy of it -- the same
    rule the annotation UI applies to anything that arrives by upload.
    """
    body = _parse_payload(payload)

    segments = ann.segments_from_payload(body.get("segments") or [])
    if not segments:
        raise HTTPException(status_code=400, detail="There are no segments to save.")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The audio file is empty.")
    if len(data) > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes / (1024 * 1024)
        raise HTTPException(
            status_code=413, detail=f"File is larger than the {limit_mb:.0f} MB limit."
        )

    filename = file.filename or "audio"
    duration = await _probe_duration(data, filename)
    audio_end = _audio_end(body, duration)
    _reject_invalid(segments, audio_end)

    profiles = ann.speaker_profiles_from_payload(
        body.get("speakers"), (s.speaker for s in segments)
    )
    editor = identity_mod.identify(request, clientId).to_dict()

    saved = await asyncio.to_thread(
        entry_mod.write_entry,
        entry_mod.new_stem(),
        segments,
        settings.dataset_dir,
        profiles=profiles,
        editor=editor,
        audio_bytes=data,
        original_filename=filename,
        duration=duration,
    )
    return {
        "stem": saved.stem,
        "segmentCount": len(saved.segments),
        "editCount": saved.meta.get("editCount"),
        "created": True,
    }


@app.delete("/api/entries/{stem}")
async def delete_entry(stem: str) -> dict[str, Any]:
    """Take one entry out of dataset/ -- and nothing out of audios/."""
    entry = _load(stem)
    removed = await asyncio.to_thread(
        entry_mod.remove_entry, settings.dataset_dir, entry.stem
    )
    return {"stem": entry.stem, "removed": removed}


# --------------------------------------------------------------------------- #
# Uploaded pairs
# --------------------------------------------------------------------------- #


@app.post("/api/parse")
async def parse_annotation(file: UploadFile = File(...)) -> dict[str, Any]:
    """Read an uploaded ``.txt`` into segments, and say what would not read.

    Only the annotation comes here. The audio stays in the browser, which needs
    the bytes anyway to draw the waveform and play it, and does not need the
    server's opinion about them until the pair is filed.
    """
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(
            status_code=413, detail="That is far too large to be an annotation file."
        )

    text = data.decode("utf-8", errors="replace")
    segments, problems = ann.segments_from_dataset_lines(text)
    if not segments and not problems:
        raise HTTPException(status_code=400, detail="That file holds no segments.")

    ordered = ann.sort_segments(segments, "time")
    return {
        "filename": file.filename,
        "segments": [ann.segment_to_dict(s) for s in ordered],
        "speakers": sorted({s.speaker for s in ordered}, key=ann.speaker_sort_key),
        "annotationEnd": ann.annotation_end(
            max((s.end for s in ordered), default=0.0)
        ),
        "problems": [p.to_dict() for p in problems],
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return body


def _parse_payload(payload: str) -> dict[str, Any]:
    try:
        body = json.loads(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="payload is not valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    return body


def _audio_end(body: dict[str, Any], known_duration: float) -> float:
    """The upper bound segments are validated against.

    The client's figure is what the annotation was edited against, but it is
    the client's; where the server knows the audio's real length it wins, so a
    request cannot widen the bounds to smuggle an out-of-range segment past.
    """
    claimed = float(body.get("audioEnd") or 0.0)
    if known_duration > 0:
        bound = ann.annotation_end(known_duration, ANNOTATION_GRID)
        return min(claimed, bound) if claimed > 0 else bound
    return claimed


def _reject_invalid(segments: list[ann.Segment], audio_end: float) -> None:
    issues = ann.validate_segments(segments, 0.0, audio_end)
    if issues:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "The annotation has validation errors and was not saved.",
                "issues": [
                    {"index": i.index, "field": i.field, "message": i.message}
                    for i in issues
                ],
            },
        )


async def _probe_duration(data: bytes, filename: str) -> float:
    """How long an uploaded audio is, or 0.0 if it cannot be read.

    A failed probe is not fatal: without ffmpeg the pair can still be filed,
    it just gets validated against the times in the annotation instead.
    """
    suffix = Path(filename.replace("\\", "/")).suffix or ".bin"
    try:
        info = await audio_mod.measure_async(data, suffix)
    except audio_mod.AudioError:
        return 0.0
    return info.duration
