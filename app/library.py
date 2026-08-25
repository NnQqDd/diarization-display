"""The dataset's audio: what is there, and which file backs which entry.

A copy of the annotation UI's module, so both apps clean a filename into a stem
identically -- that is what lets this app find the recording behind an entry
whose name was cleaned. What that app also uses it for, handing out unannotated
work, is not this app's business and is not copied.

There is no separate list of work over there either: a recording is work if
nothing has annotated it yet. That is why deleting an entry here must leave the
recording alone unless the annotator uploaded it -- doing otherwise would not
free the recording up again, it would destroy it.

The browser asks for one of these by name, so the whole point of this module is
that a name can never become a path. Nothing is ever joined onto the folder: a
name is matched against a fresh listing of it, and anything that is not one of
those exact entries resolves to nothing. ``..``, absolute paths and symlinks
pointing out of the folder therefore have nowhere to go.

A recording names its own dataset entry -- ``meeting 01.wav`` is annotated as
``meeting_01.txt``, right beside the audio it describes -- so coming back to it
later lands on the same entry instead of filing a second one.
"""

from __future__ import annotations

import re
from pathlib import Path


# Extensions an audio file may have. Identical to the annotation UI's list, so
# the same file is recognised by both.
ALLOWED_EXTENSIONS = {
    ".wav", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".mp4",
    ".aac", ".wma", ".webm", ".aiff", ".aif", ".aifc", ".caf", ".amr", ".3gp",
}

# What may appear in a dataset stem taken from a filename. Deliberately narrow:
# it is a filename on the way out as well as in, and it ends up in a URL.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def list_names(dataset_dir: Path) -> list[str]:
    """Every playable file in the folder, sorted, dotfiles and junk excluded."""
    if not dataset_dir.is_dir():
        return []
    names = []
    for path in dataset_dir.iterdir():
        if path.name.startswith("."):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        if entry_id(path.name) is None:
            continue  # no usable dataset name, so it is not offered
        names.append(path.name)
    return sorted(names)


def resolve(dataset_dir: Path, name: str | None) -> Path | None:
    """The file with that exact name, or ``None``.

    Membership of the listing is the only way in -- the name is never treated
    as a path fragment, so there is nothing to traverse with.
    """
    if not name:
        return None
    return dataset_dir / name if name in set(list_names(dataset_dir)) else None


def entry_id(name: str | None) -> str | None:
    """The dataset stem a file of this name would be annotated under.

    The filename without its extension, with anything outside the safe set
    folded to ``_``. Returns ``None`` when nothing usable survives -- ``..`` and
    ``.hidden`` collapse to empty rather than to a name that means something
    else on a filesystem.
    """
    if not name:
        return None
    stem = Path(str(name).replace("\\", "/")).name
    stem = stem[: -len(Path(stem).suffix)] if Path(stem).suffix else stem
    cleaned = _UNSAFE.sub("_", stem).strip("._-")[:64]
    return cleaned if cleaned and _SAFE_ID.match(cleaned) else None


def name_for_entry(dataset_dir: Path, entry: str | None) -> str | None:
    """The recording this entry annotates, or ``None`` if there is none.

    The question asked before writing anything: is the audio for this entry
    already here? If it is, the save adds only the annotation -- no claim from
    the browser is involved, and none is trusted.
    """
    if not entry:
        return None
    for name in list_names(dataset_dir):
        if entry_id(name) == entry:
            return name
    return None


def known_entry_id(dataset_dir: Path, value: str | None) -> str | None:
    """``value`` if some recording here would be annotated under that name.

    This is what lets ``/api/save`` accept a stem that is not a UUID without
    trusting the client: the id has to be one the server itself would have
    produced from a file it can see right now.
    """
    return value if name_for_entry(dataset_dir, value) else None


def names_by_entry(dataset_dir: Path) -> dict[str, str]:
    """Every recording, keyed by the stem it would be annotated under.

    Computed once and passed down, rather than asked per entry: one pass over
    the folder is cheap, and several hundred to draw one table is not.
    """
    return {
        stem: name
        for name, stem in ((n, entry_id(n)) for n in list_names(dataset_dir))
        if stem
    }
