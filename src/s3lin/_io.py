"""Reading and writing histories as JSON Lines, optionally gzip-compressed."""

from __future__ import annotations

import gzip
import json
import os
from collections.abc import Callable, Iterable
from typing import IO, Final, cast

from ._model import HistoryError, Op

_decode: Final[Callable[[str], object]] = json.loads


def _is_gzip(path: str | os.PathLike[str]) -> bool:
    return os.fspath(path).endswith(".gz")


def parse_history(lines: Iterable[str], *, source: str = "<history>") -> list[Op]:
    """Parse JSON Lines into operations.

    Blank lines are skipped, and so are objects with neither an ``id`` nor an
    ``op`` field (run metadata). Raises HistoryError naming the bad line.
    """
    ops: list[Op] = []
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            obj = _decode(line)
        except json.JSONDecodeError as e:
            raise HistoryError(f"{source}:{lineno}: not JSON ({e.msg})") from None
        if not isinstance(obj, dict):
            raise HistoryError(f"{source}:{lineno}: expected a JSON object")
        record = cast("dict[str, object]", obj)  # JSON object keys are strings
        if "id" not in record and "op" not in record:
            continue
        try:
            ops.append(Op.from_dict(record))
        except HistoryError as e:
            raise HistoryError(f"{source}:{lineno}: {e}") from None
    return ops


def _open_text(path: str | os.PathLike[str], write: bool) -> IO[str]:
    if _is_gzip(path):
        if write:
            return gzip.open(path, "wt", encoding="utf-8")
        return gzip.open(path, "rt", encoding="utf-8")
    if write:
        return open(path, "w", encoding="utf-8")
    return open(path, encoding="utf-8")


def read_history(path: str | os.PathLike[str]) -> list[Op]:
    """Read a history file; names ending in ``.gz`` are decompressed."""
    with _open_text(path, write=False) as f:
        return parse_history(f, source=os.fspath(path))


def write_history(history: Iterable[Op], path: str | os.PathLike[str]) -> None:
    """Write a history file, one JSON object per line; ``.gz`` names are compressed."""
    with _open_text(path, write=True) as f:
        for op in history:
            f.write(json.dumps(op.to_dict()) + "\n")
