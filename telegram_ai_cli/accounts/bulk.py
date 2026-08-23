"""Importing a fleet at once, where a single bad folder must not stop the rest.

The single-account imports on :class:`~telegram_ai_cli.accounts.registry.AccountRegistry`
raise, which is right when an operator names one thing. Pointed at a backup of
fifty accounts, raising is wrong: the eleventh folder being corrupt should cost
that one account, not the other forty-nine. So these functions catch per item and
return a verdict list instead.

They live outside the registry class because that is what they are — an
orchestration over its single-item operations, adding label deduplication and
error collection and nothing else.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import InvalidInput, TelegramAIError
from .discovery import discover_tdata_dirs, label_for_tdata, unique_label
from .paths import sanitize_label

if TYPE_CHECKING:  # pragma: no cover
    from .registry import AccountRegistry


@dataclass(frozen=True, slots=True)
class ImportResult:
    """One entry of a batch import. ``ok=False`` never aborts the batch."""

    label: str
    source_path: Path
    ok: bool
    detail: str = ""


def import_tdata_batch(
    registry: AccountRegistry,
    root: Path | str,
    *,
    prefix: str = "",
    proxy_url: str | None = None,
    api_id: int | None = None,
    api_hash: str | None = None,
    copy: bool = True,
    skip_existing: bool = True,
) -> list[ImportResult]:
    """Import every tdata folder under ``root``.

    ``skip_existing`` decides what a name clash means. Skipping is the default
    because re-running an import over the same backup is the normal way to pick
    up what failed last time, and silently overwriting a working account there
    would be a poor reward for retrying.
    """
    root = Path(root).expanduser()
    results: list[ImportResult] = []
    taken = {view.label for view in registry.list_accounts()}
    for tdata in discover_tdata_dirs(root):
        try:
            wanted = sanitize_label(f"{prefix}{label_for_tdata(tdata)}")
        except InvalidInput as exc:
            results.append(ImportResult(str(tdata), tdata, False, exc.message))
            continue
        if wanted in taken and skip_existing:
            results.append(ImportResult(wanted, tdata, False, "label already registered, skipped"))
            continue
        name = unique_label(wanted, taken)
        try:
            view = registry.import_tdata(
                tdata,
                label=name,
                proxy_url=proxy_url,
                api_id=api_id,
                api_hash=api_hash,
                copy=copy,
            )
        except (TelegramAIError, OSError) as exc:
            results.append(ImportResult(name, tdata, False, f"{type(exc).__name__}: {exc}"))
            continue
        taken.add(view.label)
        results.append(ImportResult(view.label, tdata, True, "imported"))
    return results


def import_session_files(
    registry: AccountRegistry,
    paths: Iterable[Path | str],
    *,
    api_id: int | None = None,
    api_hash: str | None = None,
    proxy_url: str | None = None,
    copy: bool = True,
) -> list[ImportResult]:
    """Adopt several ``.session`` files, reporting each one separately."""
    results: list[ImportResult] = []
    for item in paths:
        src = Path(item)
        try:
            view = registry.import_session_file(
                src, api_id=api_id, api_hash=api_hash, proxy_url=proxy_url, copy=copy
            )
        except (TelegramAIError, OSError) as exc:
            results.append(ImportResult(src.stem, src, False, f"{type(exc).__name__}: {exc}"))
            continue
        results.append(ImportResult(view.label, src, True, "imported"))
    return results
