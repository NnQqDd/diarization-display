"""HTTP layer: the viewer/editor UI and the JSON API it talks to.

Route map
---------
``GET  /``                       the UI
``GET  /api/access``             what this browser is allowed to do here
``GET  /api/config``             where the dataset is, and whether it is there
``GET  /api/entries``            every entry in dataset/, newest first
``GET  /api/entries/{stem}``     one entry: metadata, segments, parse problems
``GET  /api/entries/{stem}/audio`` its audio, when the folder holds any
``PUT  /api/entries/{stem}``     replace that entry's annotation
``DELETE /api/entries/{stem}``   take the entry out of dataset/
``POST /api/parse``              read an uploaded .txt into segments
``POST /api/entries``            file an uploaded audio + .txt pair as an entry

This app is for whoever runs the annotation effort, not for annotators. It
shows the sidecar in full, IP addresses and user agents included, and it can
overwrite and delete other people's work -- so it binds to localhost and is
meant to stay there. Nothing here belongs on a public link.

Every request passes ``_access_control`` first: a browser on ``blacklist.csv``
is turned away at the door, and only a browser on ``whitelist.csv`` may make a
request that writes. See ``app/access.py`` for what those lists can and cannot
promise.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import access as access_mod
from . import annotation as ann
from . import audio as audio_mod
from . import entry as entry_mod
from . import identity as identity_mod
from .config import settings

ANNOTATION_GRID = ann.GRID_MS

app = FastAPI(title="Diarization Dataset Viewer", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #


#: The two lists are re-read whenever they change, so every request gets the
#: answer that is true right now -- which is worth nothing if the browser or a
#: proxy in between serves an older one back. Granting and revoking both have
#: to take effect on the next refresh.
NO_STORE = {"Cache-Control": "no-store"}


def _verdict(request: Request) -> access_mod.Verdict:
    return access_mod.decide(
        access_mod.client_id_from(request.headers, request.cookies),
        whitelist=access_mod.roster(settings.whitelist_path),
        blacklist=access_mod.roster(settings.blacklist_path),
    )


_BLOCKED_PAGE = """<!DOCTYPE html>
<meta charset="utf-8"><title>Not available</title>
<style>
 body{background:#0d1117;color:#c9d1d9;font:15px/1.6 ui-sans-serif,system-ui,sans-serif;
      display:grid;place-items:center;height:100vh;margin:0}
 div{max-width:34rem;padding:0 2rem;text-align:center}
 h1{font-size:1.1rem;margin:0 0 .6rem}
 p{color:#8b949e;margin:0}
</style>
<div><h1>This page is not available to you.</h1>
<p>If that is a mistake, ask whoever runs the annotation effort.</p></div>
"""


@app.middleware("http")
async def _access_control(request: Request, call_next):
    """The two lists, applied to everything, before any route sees the request.

    Blanket rather than per-route on purpose: a guard you have to remember to
    add to the next route is a guard that will be missing from the next route.
    """
    verdict = _verdict(request)
    if verdict.blocked:
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                status_code=403,
                content={"detail": {
                    "message": "This browser is not allowed to use this app.",
                    "access": "blocked",
                }},
                headers=NO_STORE,
            )
        return HTMLResponse(_BLOCKED_PAGE, status_code=403, headers=NO_STORE)

    if access_mod.changes_data(request.method, request.url.path) and not verdict.may_edit:
        return JSONResponse(
            status_code=403,
            content={"detail": {
                "message": (
                    "This browser can look but not change anything. Its id has "
                    "to be added to whitelist.csv on the machine running this."
                ),
                "access": "readonly",
            }},
            headers=NO_STORE,
        )
    return await call_next(request)


@app.get("/api/access")
async def access(request: Request, response: Response) -> dict[str, Any]:
    """What this browser may do, and everything needed to change the answer.

    The id and the path to the file are both here because they are the two
    halves of the only fix: an admin pastes the one into the other. Naming a
    path is fine in this app -- the person reading this screen is the person
    who set it.
    """
    response.headers.update(NO_STORE)
    whitelist = access_mod.roster(settings.whitelist_path)
    blacklist = access_mod.roster(settings.blacklist_path)
    verdict = _verdict(request)
    return {
        "clientId": verdict.client_id,
        "role": "editor" if verdict.may_edit else "viewer",
        "userName": verdict.user_name,
        "editorCount": len(whitelist.names),
        "whitelistPath": str(whitelist.path),
        "whitelistExists": whitelist.exists,
        "blacklistPath": str(blacklist.path),
        # Lines an admin meant as ids and this app did not read as ids.
        "problems": list(whitelist.problems) + list(blacklist.problems),
    }


@app.exception_handler(ann.AnnotationError)
async def _annotation_error(_request: Request, exc: ann.AnnotationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    # Never cached, for the same reason as /api/access: whether this browser
    # gets the app at all is a question answered by a file on disk, and a
    # cached "yes" would outlive the answer.
    return HTMLResponse(
        (settings.static_dir / "index.html").read_text(encoding="utf-8"),
        headers=NO_STORE,
    )


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
        "ffmpeg": audio_mod.ffmpeg_available(),
        "annotationGrid": ANNOTATION_GRID,
        "maxUploadMb": settings.max_upload_bytes // (1024 * 1024),
        "whitelistPath": str(settings.whitelist_path),
        "blacklistPath": str(settings.blacklist_path),
    }


# --------------------------------------------------------------------------- #
# Entries
# --------------------------------------------------------------------------- #


@app.get("/api/entries")
async def list_entries() -> dict[str, Any]:
    """Every entry, with enough of each to fill a row and to filter on."""
    entries = await asyncio.to_thread(
        entry_mod.list_entries, settings.dataset_dir
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
    entry = entry_mod.read_entry(stem, settings.dataset_dir)
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
    """Take one entry out of dataset/ -- annotation, sidecar, and its own audio.

    Its own audio means an upload. A recording the dataset already held stays:
    the two share a folder, so this is where the line is drawn, and leaving it
    is also what puts the recording back in front of annotators as work.
    """
    entry = _load(stem)
    keep_audio = not entry.audio_is_upload
    removed = await asyncio.to_thread(
        entry_mod.remove_entry, settings.dataset_dir, entry.stem,
        keep_audio=keep_audio,
    )
    return {"stem": entry.stem, "removed": removed, "keptAudio": keep_audio}


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
