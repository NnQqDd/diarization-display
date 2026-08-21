"""End-to-end browser test: open an entry, edit it, save it, put it back.

Needs a running server and a dataset it is allowed to *modify* -- this suite
edits and deletes entries, so point it at a copy, never at real work::

    mkdir -p /tmp/e2e-disp/dataset /tmp/e2e-disp/audios
    cp /path/to/real/dataset/* /tmp/e2e-disp/dataset/
    DATASET_DIR=/tmp/e2e-disp/dataset AUDIOS_DIR=/tmp/e2e-disp/audios PORT=8200 ./run.sh &
    E2E_UI=http://127.0.0.1:8200 E2E_DATASET=/tmp/e2e-disp/dataset \
      .venv/bin/python tests/e2e_browser.py

Environment overrides: E2E_UI, E2E_DATASET, E2E_AUDIOS, E2E_SHOTS.
"""

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
UI = os.environ.get("E2E_UI", "http://127.0.0.1:8200")
DATASET = Path(os.environ.get("E2E_DATASET", ROOT.parent / "diarization-annotate" / "dataset"))
AUDIOS = Path(os.environ.get("E2E_AUDIOS", ROOT.parent / "diarization-annotate" / "audios"))
SHOTS = Path(os.environ.get("E2E_SHOTS", ROOT / ".e2e-shots"))
SHOTS.mkdir(parents=True, exist_ok=True)

problems: list[str] = []
console_errors: list[str] = []


def refuse(reason: str) -> None:
    print(f"REFUSING TO RUN: {reason}", file=sys.stderr)
    sys.exit(2)


def guard() -> None:
    """Prove this is a throwaway dataset before touching anything.

    This suite edits and deletes entries. Pointing it at real work would
    destroy it, and the ways that happens are mundane: a stale server already
    holding the port, a forgotten ``E2E_DATASET``, a copy-pasted command. So
    nothing opens a browser until the *server* has confirmed which folder it is
    serving and that folder is carrying this suite's fixtures.
    """
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{UI}/api/config", timeout=10) as response:
            config = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        refuse(f"no server answering at {UI} ({exc})")

    serving = Path(config["dataset"]["path"]).resolve()
    expected = DATASET.resolve()
    if serving != expected:
        refuse(
            f"the server at {UI} is serving {serving}, not {expected}.\n"
            "  Something else is on that port. Stop it, or use another PORT."
        )

    # Fixtures, not somebody's corpus: `mixed` is edited and `orphan` deleted.
    missing = [name for name in ("mixed.txt", "orphan.txt")
               if not (expected / name).is_file()]
    if missing:
        refuse(
            f"{expected} is missing {', '.join(missing)} — see this file's header.\n"
            "  Never point this suite at a dataset you care about."
        )


guard()


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if not condition and detail else ""))
    if not condition:
        problems.append(name)


def open_entry(page, stem, timeout=60_000):
    """Click an entry and wait until *that* entry is the one on screen.

    Waiting for "the table has rows" is not enough: the entry opened before
    this one already satisfies it, so the checks would read the previous
    annotation. The list marks the open row, so that is what to wait for.
    """
    page.click(f'tr[data-stem="{stem}"]')
    page.wait_for_selector(f'tr[data-stem="{stem}"].is-open', timeout=timeout)
    page.wait_for_selector("#segmentsCard:not([hidden])", timeout=timeout)
    page.wait_for_function(
        "() => document.getElementById('segmentsBody').rows.length > 0", timeout=timeout
    )


with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    page.on("console", lambda message: message.type == "error"
            and console_errors.append(message.text))
    page.on("pageerror", lambda error: console_errors.append(f"pageerror: {error}"))
    # Every confirm() in this app guards a discard; the suite always means yes.
    page.on("dialog", lambda dialog: dialog.accept())

    page.goto(UI, wait_until="networkidle")
    page.wait_for_function(
        "() => document.getElementById('entriesBody').rows.length > 0", timeout=30_000)

    # --- the list ---------------------------------------------------------
    rows = page.eval_on_selector_all(
        "#entriesBody tr", "rows => rows.map(r => r.dataset.stem)")
    check("the dataset is listed", len(rows) > 0, str(rows))
    check("the entry with no audio is flagged",
          "missing" in page.inner_text('tr[data-stem="orphan"] .col-audio'),
          page.inner_text('tr[data-stem="orphan"] .col-audio'))
    # A playable entry says nothing: which folder its audio came out of is not
    # something anyone can act on, so the column is only for the exception.
    check("a playable entry is not labelled at all",
          page.inner_text('tr[data-stem="mixed"] .col-audio').strip() == "",
          repr(page.inner_text('tr[data-stem="mixed"] .col-audio')))
    check("the annotator is on the row",
          "abc12345" in page.inner_text('tr[data-stem="mixed"] .col-annotator'),
          page.inner_text('tr[data-stem="mixed"] .col-annotator'))
    page.screenshot(path=SHOTS / "01-list.png", full_page=True)

    # --- filtering --------------------------------------------------------
    page.fill("#entryFilter", "10.0.0.5")
    page.wait_for_timeout(200)
    check("filtering by annotator address narrows the list",
          page.locator("#entriesBody tr").count() == 1,
          str(page.locator("#entriesBody tr").count()))
    page.fill("#entryFilter", "")
    page.wait_for_timeout(200)

    # --- an entry whose audio lives on the shelf --------------------------
    check("this entry keeps no audio of its own",
          not list(DATASET.glob("mixed.*"))[0].parent.joinpath("mixed.wav").exists())
    served = page.request.get(f"{UI}/api/entries/mixed/audio")
    check("and it is served anyway, from the audios folder",
          served.status == 200 and served.body()[:4] == b"RIFF", str(served.status))

    open_entry(page, "mixed")
    page.wait_for_function("() => document.getElementById('waveLoading').hidden",
                           timeout=60_000)
    check("the waveform decoded it",
          page.evaluate("document.getElementById('waveCanvas').width") > 0)
    check("the player knows its duration",
          page.evaluate("document.getElementById('audio').duration") > 1)
    check("the metadata panel shows the annotator's address",
          "10.0.0.5" in page.inner_text("#metaGrid"))
    check("nothing is dirty on open", page.is_disabled("#saveBtn"))
    page.screenshot(path=SHOTS / "02-open.png", full_page=True)

    # --- editing ----------------------------------------------------------
    first = page.locator("#segmentsBody tr").first
    first.locator('[data-field="speaker"]').fill("renamed")
    first.locator('[data-field="speaker"]').press("Enter")
    page.wait_for_timeout(200)
    check("an edit makes the entry dirty", not page.is_disabled("#saveBtn"))
    check("and says so", "Unsaved" in page.inner_text("#saveStatus"),
          page.inner_text("#saveStatus"))

    page.click("#addSegment")
    page.wait_for_timeout(200)
    check("a segment can be added",
          page.locator("#segmentsBody tr").count() == 3,
          str(page.locator("#segmentsBody tr").count()))

    page.locator("#segmentsBody tr").last.locator('[data-action="delete"]').click()
    page.wait_for_timeout(200)
    check("and deleted again", page.locator("#segmentsBody tr").count() == 2)

    page.click("#saveBtn")
    page.wait_for_function(
        "() => /no unsaved edits/.test(document.getElementById('saveStatus').textContent)",
        timeout=30_000)
    saved = (DATASET / "mixed.txt").read_text()
    # Not the first line: the segment added above starts earlier, and the file
    # is always written chronologically whatever the table was sorted by.
    check("the rename reached the file on disk",
          "renamed 00:00:00.500 00:00:02.000 hello" in saved, saved)
    check("the file is chronological",
          [l.split()[1] for l in saved.splitlines()]
          == sorted(l.split()[1] for l in saved.splitlines()), saved)
    check("the audio was not copied into the dataset",
          not (DATASET / "mixed.wav").exists())
    check("the source audio is untouched", (AUDIOS / "mixed.wav").is_file())
    page.screenshot(path=SHOTS / "03-saved.png", full_page=True)

    # --- the sidecar keeps its history ------------------------------------
    import json
    meta = json.loads((DATASET / "mixed.meta.json").read_text())
    check("the original annotator survived the edit",
          meta["annotator"]["ip"] == "10.0.0.5", str(meta.get("annotator")))
    check("the edit was recorded beside it", meta.get("editCount") == 1, str(meta.get("editCount")))
    check("the speaker list followed the rename",
          "renamed" in meta["speakers"], str(meta["speakers"]))

    # --- revert -----------------------------------------------------------
    speaker_box = lambda: page.locator("#segmentsBody tr").first.locator('[data-field="speaker"]')
    on_disk = speaker_box().input_value()
    speaker_box().fill("scratch")
    speaker_box().press("Enter")
    page.wait_for_timeout(200)
    check("the scratch edit took", not page.is_disabled("#revertBtn"))
    page.click("#revertBtn")
    page.wait_for_function(
        "() => /no unsaved edits/.test(document.getElementById('saveStatus').textContent)",
        timeout=30_000)
    check("revert threw the edit away and re-read the file",
          speaker_box().input_value() == on_disk,
          f"{speaker_box().input_value()!r} != {on_disk!r}")
    check("and the scratch name never reached disk",
          "scratch" not in (DATASET / "mixed.txt").read_text())

    # --- an entry with no audio at all ------------------------------------
    page.click("#refreshBtn")
    page.wait_for_timeout(500)
    open_entry(page, "orphan")
    check("an entry with no audio still opens",
          page.locator("#segmentsBody tr").count() == 1)
    check("and says why there is no waveform",
          "No audio" in page.inner_text("#audioMissing"),
          page.inner_text("#audioMissing"))
    check("its times are still editable",
          page.locator("#segmentsBody tr").first
              .locator('[data-field="start"]').is_editable())
    page.screenshot(path=SHOTS / "04-no-audio.png", full_page=True)

    # --- deleting ---------------------------------------------------------
    page.click("#deleteBtn")
    page.wait_for_timeout(300)
    check("delete asks twice", "Click again" in page.inner_text("#deleteBtn"),
          page.inner_text("#deleteBtn"))
    page.click("#deleteBtn")
    page.wait_for_timeout(1200)
    check("the entry is gone from disk", not (DATASET / "orphan.txt").exists())
    check("and gone from the list",
          page.locator('tr[data-stem="orphan"]').count() == 0)
    check("the editor closed with it", page.is_hidden("#segmentsCard"))

    # --- uploading a pair --------------------------------------------------
    page.click("#pairToggle")
    page.wait_for_selector("#pairCard:not([hidden])")
    pair_txt = SHOTS / "pair.txt"
    pair_txt.write_text("speaker_0 00:00:00.500 00:00:02.000 uploaded pair\n")
    page.set_input_files("#audioInput", str(AUDIOS / "mixed.wav"))
    page.set_input_files("#textInput", str(pair_txt))
    page.wait_for_timeout(200)
    check("both halves are needed before it will open",
          not page.is_disabled("#openPairBtn"))
    page.click("#openPairBtn")
    page.wait_for_selector("#segmentsCard:not([hidden])", timeout=60_000)
    check("the uploaded annotation is in the table",
          page.locator("#segmentsBody tr").count() == 1)
    check("it is not in the dataset yet",
          "Not in the dataset" in page.inner_text("#saveStatus"),
          page.inner_text("#saveStatus"))
    page.screenshot(path=SHOTS / "05-pair.png", full_page=True)

    before = {p.name for p in DATASET.iterdir()}
    page.click("#saveBtn")
    page.wait_for_function(
        "() => /no unsaved edits/.test(document.getElementById('saveStatus').textContent)",
        timeout=60_000)
    new_files = {p.name for p in DATASET.iterdir()} - before
    check("filing the pair wrote a full entry, audio included",
          len(new_files) == 3, str(sorted(new_files)))
    stem = next(iter(n.rsplit(".", 1)[0] for n in new_files if n.endswith(".txt")))
    check("and the audio came with it", (DATASET / f"{stem}.wav").is_file())

    # Housekeeping: the pair was this suite's, so it goes back out.
    page.evaluate("id => fetch('/api/entries/' + id, {method: 'DELETE'})", stem)
    page.wait_for_timeout(600)
    check("the suite leaves no pair behind",
          not list(DATASET.glob(f"{stem}.*")))

    browser.close()

print("\n--- console errors ---")
for error in console_errors:
    print(" ", error)

if problems or console_errors:
    print(f"\n{len(problems)} FAILED, {len(console_errors)} console error(s)")
    sys.exit(1)
print("\nALL CHECKS PASSED")
