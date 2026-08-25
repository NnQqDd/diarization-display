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
| `WHITELIST_CSV` | `./whitelist.csv` | Browsers allowed to change anything |
| `BLACKLIST_CSV` | `./blacklist.csv` | Browsers refused the page |

## Who may edit

This app can overwrite and delete other people's finished annotation, so by
default nobody can. Editing is granted per browser, in `whitelist.csv`:

```csv
client_id,user_name
0fdb05da-7ac6-4c24-ba76-7d6cfb09f2f9,Duy
7fee2aa2-cab0-44ce-8b40-741850d738fd,
```

The id is at the top right of the page — click it to copy. Anyone who opens
the app sees their own, along with the line to send you, so the whole exchange
is a paste into this file. The name is a label; leave it blank if you like.
Both files are re-read when they change, so a new line applies immediately.
No restart.

Everyone else gets the app read-only: they can open, play, filter and inspect
every entry, and Save and Delete are refused — in the buttons and again in the
server, which is the one that counts.

`blacklist.csv` is the same two columns and refuses the page outright, before
any of it is served. It beats the whitelist: an id on both lists is blocked.

**What this is and is not.** The id is a UUID the browser keeps in
`localStorage`. It identifies a browser profile, not a person: it resets when
site data is cleared, differs per browser and in private windows, and anyone
who can reach the server can claim any id they like. It is a lock on an office
door — it stops the wrong colleague deleting a week of work, and stops nothing
else. The perimeter is still that this app binds to localhost.

**If nobody can edit,** the whitelist is missing, empty or does not have your
id in it. That is deliberate: a whitelist that fails open when its file gets
renamed is not a whitelist. The page tells you the id and the path; put the
one in the other.

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
  access.py     the two lists: who may edit, who is refused
static/         index.html, css/, js/ (no build step, no CDN)
deploy/         systemd unit, nginx vhost, and a walkthrough that uses both
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
| `GET /api/access` | what this browser may do, and the id and path to change it |
| `GET /api/entries` | every entry, newest first |
| `GET /api/entries/{stem}` | one entry: sidecar, segments, unparseable lines |
| `GET /api/entries/{stem}/audio` | its audio, from wherever it was found |
| `PUT /api/entries/{stem}` | replace that entry's annotation |
| `POST /api/entries` | file an uploaded pair as a new entry |
| `DELETE /api/entries/{stem}` | remove the entry from `dataset/` |
| `POST /api/parse` | read an uploaded `.txt` into segments |

## Running it somewhere

`deploy/` holds a systemd unit that keeps the app up on localhost, an nginx
vhost to put in front of it, and `deploy.example.sh`, which walks through both
plus certbot. Read that script rather than running it blind — the last step
publishes this app on a public name.

Two things to weigh first, because neither is fixed by `whitelist.csv`:

- **Reading is not gated.** Anyone who is not blacklisted can open every
  entry, and that includes each annotator's IP address and user agent. Only
  changing things is restricted.
- **The lists identify browsers, not people.** See the caveat above.

If either matters, uncomment `auth_basic` in the vhost — it is the one thing
in the request a caller cannot choose for itself — or skip the vhost entirely
and reach it over `ssh -N -L 8200:127.0.0.1:8200 <host>`.

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

DATASET_DIR=/tmp/e2e-disp/dataset PORT=8200 \
  WHITELIST_CSV=/tmp/e2e-disp/whitelist.csv \
  BLACKLIST_CSV=/tmp/e2e-disp/blacklist.csv ./run.sh &
E2E_UI=http://127.0.0.1:8200 E2E_DATASET=/tmp/e2e-disp/dataset \
  .venv/bin/python tests/e2e_browser.py
```

It expects the fixtures its header describes: an entry named `mixed` backed by
`mixed.wav`, and one named `orphan` with no audio at all. Before opening
a browser it asks the server which folder it is serving and refuses to run
unless that is the folder you named and those fixtures are in it — a stale
server holding the port is otherwise an easy way to edit real work by accident.
The access lists get the same treatment: the suite writes itself on and off
them to test being locked out, so it refuses to run against this checkout's
own `whitelist.csv`.
