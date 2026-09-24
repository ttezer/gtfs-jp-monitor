"""Canonical JSON serialization and number rounding (data-model §5.1).

Every file written to the data repository goes through `dumps`/`write_json` so that
the same logical content always produces byte-identical output.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

_ONE_DECIMAL = Decimal("0.1")


def _to_decimal(value: float | int) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"expected a number, got {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite number: {value!r}")
    # str() gives the shortest repr, so 1.25 stays 1.25 instead of 1.2499999...
    return Decimal(str(value))


def round1(value: float | int) -> float:
    """Round to one decimal place, halves away from zero (2.25 -> 2.3, -1.25 -> -1.3)."""
    return float(_to_decimal(value).quantize(_ONE_DECIMAL, rounding=ROUND_HALF_UP))


def delta1(before: float | int, after: float | int) -> float:
    """`after - before` computed exactly, then rounded like `round1` (98.1 - 96.2 -> 1.9)."""
    diff = _to_decimal(after) - _to_decimal(before)
    return float(diff.quantize(_ONE_DECIMAL, rounding=ROUND_HALF_UP))


def yyyymmdd_to_iso(value: int | str) -> str:
    """Convert an Analyzer date (20260401 or "20260401") to "2026-04-01"."""
    text = str(value)
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"expected YYYYMMDD, got {value!r}")
    return date(int(text[0:4]), int(text[4:6]), int(text[6:8])).isoformat()


def dumps(obj: Any) -> str:
    """Serialize `obj` canonically: sorted keys, 2-space indent, UTF-8 text, trailing newline."""
    text = json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
        separators=(",", ": "),
    )
    return text + "\n"


def write_json_if_changed(path: str | os.PathLike[str], obj: Any) -> bool:
    """Write only when the canonical bytes differ from what is on disk. Returns True if written."""
    target = Path(path)
    data = dumps(obj).encode("utf-8")
    if target.is_file() and target.read_bytes() == data:
        return False
    _atomic_write(target, data)
    return True


def write_json(path: str | os.PathLike[str], obj: Any) -> None:
    """Write `obj` canonically; the target is replaced atomically so readers never see a partial file."""
    _atomic_write(Path(path), dumps(obj).encode("utf-8"))


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
