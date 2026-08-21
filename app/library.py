"""The ``audios/`` shelf: source audio the server already has.

The browser asks for one by name, so the whole point of this module is that a
name can never become a path. Nothing is ever joined onto the folder: a name is
matched against a fresh listing of it, and anything that is not one of those
exact entries resolves to nothing. ``..``, absolute paths and symlinks pointing
out of the folder therefore have nowhere to go.

A file here names its own dataset entry -- ``meeting_01.wav`` belongs to
``dataset/meeting_01.*`` rather than to a UUID -- so an annotation lands next
to the name its audio already had.

A copy of ``diarization-annotate/app/library.py``, and one that has to stay
identical: this app looks here for an entry's audio when ``dataset/`` holds no
copy of it, and it can only find it by cleaning a filename into a stem exactly
the way the annotation UI did when it wrote the entry.
"""

from __future__ import annotations

import re
from pathlib import Path

from .entry import ALLOWED_EXTENSIONS

# What may appear in a dataset stem taken from a filename. Deliberately narrow:
# it is a filename on the way out as well as in, and it ends up in a URL.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def list_names(audios_dir: Path) -> list[str]:
    """Every playable file in the folder, sorted, dotfiles and junk excluded."""
    if not audios_dir.is_dir():
        return []
    names = []
    for path in audios_dir.iterdir():
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


def resolve(audios_dir: Path, name: str | None) -> Path | None:
    """The file with that exact name, or ``None``.

    Membership of the listing is the only way in -- the name is never treated
    as a path fragment, so there is nothing to traverse with.
    """
    if not name:
        return None
    return audios_dir / name if name in set(list_names(audios_dir)) else None


def entry_id(name: str | None) -> str | None:
    """The dataset stem a file of this name would be saved under.

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


def name_for_entry(audios_dir: Path, entry: str | None) -> str | None:
    """The shelf file that would be saved under this stem, or ``None``.

    The question ``/api/save`` asks before writing anything: is the audio for
    this entry already on the server? If it is, the entry keeps only its
    annotation and the audio stays here, under the name it already had.
    """
    if not entry:
        return None
    for name in list_names(audios_dir):
        if entry_id(name) == entry:
            return name
    return None


def known_entry_id(audios_dir: Path, value: str | None) -> str | None:
    """``value`` if some file on the shelf would be saved under that name.

    This is what lets ``/api/save`` accept a stem that is not a UUID without
    trusting the client: the id has to be one the server itself would have
    produced from a file it can see right now.
    """
    return value if name_for_entry(audios_dir, value) else None
