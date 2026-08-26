"""Recognising account material lying around on disk.

Importing a fleet means pointing at a backup, not at fifty folders one by one,
so these functions answer "is this a tdata folder", "is this a Telethon session"
and "where are they" by inspecting content rather than trusting a name. A
directory called ``tdata`` that holds no key file is not one, and a file named
``x.session`` that is not SQLite would fail much later, inside Telethon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from ..errors import InvalidInput

#: First bytes of any SQLite file, i.e. of any Telethon ``.session``.
SQLITE_MAGIC: Final = b"SQLite format 3\x00"

#: Telegram Desktop keeps the local key in ``key_<keyFile>``; ``data`` is the
#: default keyFile, so a real tdata folder always has a ``key_*`` entry.
TDATA_MARKERS: Final = ("key_data", "key_datas")


def discover_tdata_dirs(root: Path) -> list[Path]:
    """Find every Telegram Desktop data folder under ``root``.

    Three layouts turn up in backups: ``root`` is itself a tdata folder, tdata
    folders sit one level down inside per-account directories, or they are
    scattered at arbitrary depth. All three are handled because an operator with
    fifty accounts is not going to reshape the tree by hand.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    if looks_like_tdata(root):
        return [root]
    found = {p.resolve() for p in root.rglob("tdata") if p.is_dir() and looks_like_tdata(p)}
    if not found:
        found = {d.resolve() for d in root.iterdir() if d.is_dir() and looks_like_tdata(d)}
    return sorted(found)


def looks_like_tdata(path: Path) -> bool:
    try:
        names = {entry.name for entry in path.iterdir()}
    except OSError:
        return False
    return any(marker in names for marker in TDATA_MARKERS) or any(
        name.startswith("key_") for name in names
    )


def label_for_tdata(path: Path) -> str:
    # Backups name the *account* folder and put a literal "tdata" inside it.
    return path.parent.name if path.name.lower() == "tdata" else path.name


def is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def unique_label(label: str, taken: set[str]) -> str:
    """Derive a free label, so one clash does not abort a fifty-account import."""
    if label not in taken:
        return label
    for suffix in range(2, 1000):
        candidate = f"{label}-{suffix}"
        if candidate not in taken:
            return candidate
    raise InvalidInput(f"cannot derive a free label from {label!r}")
