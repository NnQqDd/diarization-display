"""Runtime configuration, and where the dataset being viewed lives.

This app owns no data. It reads and edits what the annotation UI wrote, so the
one setting that matters is ``ANNOTATEPATH``: the checkout of
``diarization-annotate`` whose ``dataset/`` and ``audios/`` folders are the
subject. Everything else follows from it and can still be overridden one by one
when a deployment splits them up.

Settings come from ``.env`` beside this checkout, then from the real
environment, which wins -- so a one-off run can point somewhere else without
editing the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PORT = 8200


def load_dotenv(path: Path) -> dict[str, str]:
    """Read ``KEY=value`` lines. Absent file, comments and blanks are fine.

    Deliberately small: no interpolation, no export syntax, no multi-line
    values. A dotenv here holds paths and a port, and a parser that does more
    than it is asked to is a parser that surprises somebody at 2am.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # Quotes are stripped so a path with spaces can be written either way.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


_DOTENV = load_dotenv(ROOT / ".env")


def _get(name: str) -> str:
    """The real environment first, then ``.env``. Neither one wins by accident."""
    return (os.environ.get(name) or _DOTENV.get(name) or "").strip()


def _env_int(name: str, default: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # surface the typo instead of silently defaulting
        raise ValueError(f"{name}={raw!r} is not an integer") from exc


def _env_path(name: str, default: Path) -> Path:
    raw = _get(name)
    return Path(raw).expanduser().resolve() if raw else default


@dataclass(frozen=True)
class Settings:
    annotate_path: Path
    dataset_dir: Path
    audios_dir: Path
    static_dir: Path
    max_upload_bytes: int

    @property
    def dataset_ok(self) -> bool:
        return self.dataset_dir.is_dir()

    @property
    def audios_ok(self) -> bool:
        return self.audios_dir.is_dir()


def load_settings() -> Settings:
    # The sibling checkout is the obvious default: these two repos are built to
    # sit next to each other, and a missing .env should still start.
    annotate_path = _env_path("ANNOTATEPATH", (ROOT.parent / "diarization-annotate"))
    return Settings(
        annotate_path=annotate_path,
        # Overridable on their own, for a deployment that keeps the data
        # somewhere other than inside the annotation checkout.
        dataset_dir=_env_path("DATASET_DIR", annotate_path / "dataset"),
        audios_dir=_env_path("AUDIOS_DIR", annotate_path / "audios"),
        static_dir=ROOT / "static",
        max_upload_bytes=_env_int("MAX_UPLOAD_MB", 1024) * 1024 * 1024,
    )


settings = load_settings()
