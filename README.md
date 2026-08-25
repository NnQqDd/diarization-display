# Diarization Dataset Viewer

A local admin tool for the dataset the
[annotation UI](../diarization-annotate) writes: list what has been annotated,
open an entry, listen to it, fix the segments, save it back — or delete it.

It edits real annotations in place and shows the full sidecar, IP addresses and
user agents included. **It binds to localhost and belongs there.** No auth, no
vhost, no public link.

## Setup

```bash
cp .env.example .env        # point ANNOTATEPATH at your diarization-annotate checkout
./run.sh                    # http://127.0.0.1:8200
```

`run.sh` creates `.venv` on first run and prints the two folders it resolved
before it binds, which is the thing to check when a screen comes up empty.

Requirements: Python 3.10+, and **ffprobe** on `PATH` if you want an uploaded
pair measured properly (without it, a WAV is read from its own header and
anything else is validated against the times in its annotation).

## Configuration

`.env` beside this checkout, overridden by the real environment.

| Variable | Default | Purpose |
|---|---|---|
| `ANNOTATEPATH` | `../diarization-annotate` | The checkout whose data this opens |
| `DATASET_DIR` | `$ANNOTATEPATH/dataset` | Set only if the data is not inside that checkout |
| `PORT` | `8200` | |
| `HOST` | `127.0.0.1` | Leave it |
| `MAX_UPLOAD_MB` | `1024` | Ceiling on an uploaded pair's audio |

## What an entry is

A **stem**, not a file. `dataset/` holds files sharing one:

```
550e8400-….wav        the audio
550e8400-….txt        the annotation, one segment per line
550e8400-….meta.json  duration, speakers, who saved it and when
```

The `.txt` is what makes an entry exist — it is the work. Audio with no
annotation beside it is a leftover and is not listed.

### Finding the audio

Automatic, and not something the screen talks about: there is one folder, so
*where* a recording was found changes nothing anyone can act on. The UI says
only when there is no recording at all.

1. **`<stem>.<ext>`** — the usual case, and the one that wins when both exist:
   it is the file the entry was filed under.
2. **A recording whose name cleans to the stem** — an entry made with
   **Randomize audio** is named after the recording it annotates, cleaned the
   same way the annotation UI does it, so `meeting 01.wav` backs `meeting_01`
   and a stem glob alone would never find it.
3. **Nothing** — the row is tagged `missing`, the only case the list marks,
   and the entry still opens. Times are the work; without the sound you can
   still read, retime and delete them, validated against the length recorded in
   the sidecar.

### What a save does, and does not

Saving rewrites the `.txt` and refreshes the sidecar's derived fields
(`segmentCount`, `speakers`, `speakerProfiles`), then records `editedAt`,
`editedBy` and `editCount` beside them.

**It never writes audio for an existing entry.** Every other key — `annotator`, `savedAt`, and anything another program put
there — is the original annotation's record and is carried through untouched.
An edit is a new fact recorded next to the old one, not a replacement. Keys
this app has no concept of are never given a label in the metadata panel, but
the **Raw sidecar** panel always shows the file exactly as it is on disk.

Writes go through a temp file and `os.replace`, so a crash mid-save leaves the
previous version intact rather than a truncated one.

**Delete** removes the entry's annotation and sidecar, and its audio only when
the sidecar says `"audioSource": "upload"` — the annotator's own file, filed
with the entry. Anything else is a recording the dataset already held: it stays,
and with no annotation beside it the annotation UI counts it as work again. That
is the only thing between a tidy-up and a lost recording, so silence is not
consent: an entry with no sidecar keeps its audio.

## Uploading a pair

**Upload a pair instead** takes an audio file and an annotation `.txt` — the
same `<speaker> <start> <end> <transcript>` format, not JSON — and opens them
for editing without writing anything. Press **Add to dataset** and it is filed
as a new entry under a fresh UUID, audio copied in, because that upload is now
the only copy of it.

Lines that will not parse are listed above the table rather than dropped
quietly: they are not in the editor, so saving would lose them, and you should
see that before you press the button.

## Layout

```
app/
  main.py       HTTP routes; the only module that knows about FastAPI
  config.py     .env + environment -> Settings
  entry.py      one entry: read, find its audio, write it back, delete it
  annotation.py segment model, validation — a copy of the annotation UI's, plus a reader
  library.py    the dataset's audio — a copy, so both apps clean a name identically
  audio.py      ffprobe duration
  identity.py   who made an edit
static/         index.html, css/, js/ (no build step, no CDN)
```

`annotation.js`, `segments.js`, `speakers.js`, `waveform.js` and `app.css` are
copied verbatim from the annotation UI, so the two apps look and behave like
one product — and so an annotation this app writes is one that app accepts.
`app/annotation.py` is the same copy plus `segments_from_dataset_lines`, the
reader the writing-only app has no use for.

### API

| Route | Purpose |
|---|---|
| `GET /api/config` | which folders were resolved, and whether they exist |
| `GET /api/entries` | every entry, newest first |
| `GET /api/entries/{stem}` | one entry: sidecar, segments, unparseable lines |
| `GET /api/entries/{stem}/audio` | its audio, from wherever it was found |
| `PUT /api/entries/{stem}` | replace that entry's annotation |
| `POST /api/entries` | file an uploaded pair as a new entry |
| `DELETE /api/entries/{stem}` | remove the entry from `dataset/` |
| `POST /api/parse` | read an uploaded `.txt` into segments |

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Every test builds its own dataset under `tmp_path`; nothing in the suite can
reach a real one.

The browser suite is a script, not a pytest module, because it needs a running
server — and because it edits and deletes, **give it a copy**:

```bash
.venv/bin/pip install playwright && .venv/bin/playwright install chromium

mkdir -p /tmp/e2e-disp/dataset
cp /path/to/dataset/* /tmp/e2e-disp/dataset/

DATASET_DIR=/tmp/e2e-disp/dataset PORT=8200 ./run.sh &
E2E_UI=http://127.0.0.1:8200 E2E_DATASET=/tmp/e2e-disp/dataset \
  .venv/bin/python tests/e2e_browser.py
```

It expects the fixtures its header describes: an entry named `mixed` backed by
`mixed.wav`, and one named `orphan` with no audio at all. Before opening
a browser it asks the server which folder it is serving and refuses to run
unless that is the folder you named and those fixtures are in it — a stale
server holding the port is otherwise an easy way to edit real work by accident.
