"""Validation of identifiers that come from the gtfs-data.jp API (data-model §3).

API values are untrusted: org_id, feed_id and gtfs_file_uid become directory and file
names, so they are checked against strict patterns before any path is built.
"""

from __future__ import annotations

import re
from pathlib import Path

PATH_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
UID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
RID_RE = re.compile(r"(current)|(prev|next)_([1-9][0-9]*)")


class InvalidIdentifier(ValueError):
    """Raised when an API value cannot be used as an identifier."""


def is_path_id(value: object) -> bool:
    return isinstance(value, str) and value not in (".", "..") and PATH_ID_RE.fullmatch(value) is not None


def is_uid(value: object) -> bool:
    return isinstance(value, str) and UID_RE.fullmatch(value) is not None


def require_path_id(value: object, field: str) -> str:
    if not is_path_id(value):
        raise InvalidIdentifier(f"invalid {field}: {value!r}")
    return value  # type: ignore[return-value]


def require_uid(value: object) -> str:
    if not is_uid(value):
        raise InvalidIdentifier(f"invalid gtfs_file_uid: {value!r}")
    return value  # type: ignore[return-value]


def rid_rank(rid: str) -> int:
    """Newness rank of a rid: next_2 < next_1 < current (0) < prev_1 < prev_2.

    Smaller is newer. Raises InvalidIdentifier for unknown values instead of guessing.
    """
    match = RID_RE.fullmatch(rid) if isinstance(rid, str) else None
    if match is None:
        raise InvalidIdentifier(f"invalid rid: {rid!r}")
    if match.group(1):
        return 0
    n = int(match.group(3))
    return n if match.group(2) == "prev" else -n


def feed_dir(root: Path, org_id: str, feed_id: str) -> Path:
    """Directory of one feed in the data repository, built only from validated components."""
    return Path(root) / "feeds" / require_path_id(org_id, "org_id") / require_path_id(feed_id, "feed_id")
