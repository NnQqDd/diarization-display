"""Who may open this page, and who may change what is on it.

Two CSV files beside this checkout, both ``client_id,user_name``:

``whitelist.csv``
    The browsers allowed to save, edit, delete and file new entries. Anyone
    else may look and may not touch: this app can overwrite and delete other
    people's finished work, and that is not a thing to hand out by default.
``blacklist.csv``
    Browsers refused the page outright, before any of it is served.

The id is the one from ``identity.py``: a UUID the browser generated once and
kept in ``localStorage``. So understand what this is and is not. It says *this
browser profile* may edit -- not this person. It resets when site data is
cleared, differs per browser and in private windows, and anybody who can reach
the server can claim any id they like. It is a lock on an office door, not a
wall: it stops the wrong colleague deleting a week of annotation, and it stops
nothing else. The real perimeter is still that this app binds to localhost.

``user_name`` is a label for whoever maintains the file, and for the page to
greet you by. Nothing is decided by it, and it may be empty.

Both files are re-read whenever they change, so adding a line takes effect on
the next request instead of at the next restart -- an admin adding a colleague
should not have to bounce the server to do it.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .identity import clean_client_id

#: Mirrors ``localStorage`` so the *document* request carries an id too. A
#: header only reaches the API, and blocking somebody after the page has
#: already been served is blocking them a step too late.
COOKIE = "clientId"

#: Requests that cannot change anything on disk.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: ``POST`` that only reads: it parses an uploaded ``.txt`` and answers with
#: what is in it. Filing that pair is a separate request, and that one is
#: guarded.
READ_ONLY_PATHS = frozenset({"/api/parse"})

_HEADER_CELLS = frozenset({"client_id", "clientid", "client id"})


def _key(value: str) -> str:
    """How two ids are compared: trimmed, and case does not count."""
    return value.strip().casefold()


@dataclass(frozen=True)
class Roster:
    """One list, as it was last read off disk."""

    path: Path
    exists: bool
    #: Normalised id -> the name beside it, ``""`` when that column was blank.
    names: dict[str, str]
    #: Lines that were not an id, kept so the page can say so rather than
    #: leaving an admin wondering why their entry does nothing.
    problems: tuple[str, ...] = ()

    def __contains__(self, client_id: str | None) -> bool:
        return bool(client_id) and _key(client_id) in self.names

    def name_of(self, client_id: str | None) -> str | None:
        if not client_id:
            return None
        return self.names.get(_key(client_id)) or None


@dataclass(frozen=True)
class Verdict:
    """What one request is allowed to do."""

    client_id: str | None
    blocked: bool
    may_edit: bool
    user_name: str | None = None


def read_roster(path: Path) -> Roster:
    """Parse one list. A missing file is an empty list, not an error.

    Deliberately forgiving about everything except the id itself: a header row,
    ``#`` comments, blank lines and extra columns are all fine, because this
    file is maintained by hand in whatever editor is on the machine. The id is
    checked, and a line that does not hold one is reported rather than dropped
    -- a typo that silently locks somebody out is the failure to avoid here.
    """
    if not path.is_file():
        return Roster(path, exists=False, names={})
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Roster(path, exists=True, names={}, problems=(f"unreadable: {exc}",))

    names: dict[str, str] = {}
    problems: list[str] = []
    for row in csv.reader(text.splitlines()):
        if not row:
            continue
        raw = row[0].strip()
        if not raw or raw.startswith("#") or _key(raw) in _HEADER_CELLS:
            continue
        client_id = clean_client_id(raw)
        if client_id is None:
            problems.append(raw[:80])
            continue
        names[_key(client_id)] = row[1].strip() if len(row) > 1 else ""
    return Roster(path, exists=True, names=names, problems=tuple(problems))


# Keyed by path, stamped with what would change if the file were edited.
_cache: dict[Path, tuple[tuple[int, int] | None, Roster]] = {}


def forget_rosters() -> None:
    """Drop the cache. For tests, which rewrite these files faster than mtime."""
    _cache.clear()


def roster(path: Path) -> Roster:
    """The list at ``path``, re-read only when it has actually changed.

    One ``stat`` per request is cheap next to the read and parse, and it means
    an admin can add a line and have it apply immediately.
    """
    try:
        info = path.stat()
        stamp: tuple[int, int] | None = (info.st_mtime_ns, info.st_size)
    except OSError:
        stamp = None
    cached = _cache.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    fresh = read_roster(path)
    _cache[path] = (stamp, fresh)
    return fresh


def decide(client_id: str | None, *, whitelist: Roster, blacklist: Roster) -> Verdict:
    """What this id may do.

    Two rules, both pointing the same way -- when in doubt, allow less:

    * The blacklist is checked first and beats the whitelist. An id on both
      lists is blocked, because whoever added it to the blacklist did so later
      and meant it.
    * Editing requires being named. No id, an unknown id, an empty list or a
      list that does not exist all mean the same thing: read-only. A whitelist
      that fails open is not a whitelist, and a deleted file must not quietly
      hand the dataset to everybody.

    Being read-only is recoverable in thirty seconds -- the page shows the id
    to add. Discovering that the guard was off since a file got renamed is not.
    """
    if client_id in blacklist:
        return Verdict(client_id, blocked=True, may_edit=False)
    if client_id in whitelist:
        return Verdict(
            client_id, blocked=False, may_edit=True,
            user_name=whitelist.name_of(client_id),
        )
    return Verdict(client_id, blocked=False, may_edit=False)


def client_id_from(headers, cookies) -> str | None:
    """The id this request claims, from the header first, then the cookie.

    The header is what the app's own ``fetch`` sends. The cookie is the same
    value, mirrored so the plain page load carries it too.
    """
    return (
        clean_client_id(headers.get("x-client-id"))
        or clean_client_id(cookies.get(COOKIE))
    )


def changes_data(method: str, path: str) -> bool:
    """Whether this request could write to the dataset.

    Framed as "everything except what is known to be safe", so a route added
    later is guarded by default rather than by remembering to guard it.
    """
    if method.upper() in SAFE_METHODS:
        return False
    return path.rstrip("/") not in READ_ONLY_PATHS
