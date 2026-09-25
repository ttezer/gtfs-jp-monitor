"""Content signature of a publication's ZIP (data-model §4.2).

Two publications have the same signature only when every file holds the same values. The
signature may call equal content different (the cost is one more semantic report), but must
never call different values equal, so nothing is trimmed, case-folded, rounded or decoded
lossily.

A `.txt` file is read as strict UTF-8 CSV (a leading BOM and blank lines are dropped); columns
are ordered by name and rows form a multiset (order-free, duplicates count), hashed as the sum
of row hashes modulo 2^256 so that memory stays constant. A file that is not regular CSV (bad
encoding or quoting, duplicate columns, ragged rows, no header) and any other file are hashed
as raw bytes. File paths are part of the signature.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
import zlib
from pathlib import Path

SCHEMA = "gtfs-jp-monitor-content/1"
_MOD = 1 << 256
_CHUNK = 1 << 20


def _raw(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    h = hashlib.sha256()
    with zf.open(info) as f:
        while block := f.read(_CHUNK):
            h.update(block)
    return h.hexdigest()


def _csv(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    with zf.open(info) as f:
        rows = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", errors="strict", newline=""), strict=True)
        header = next(rows)
        if len(set(header)) != len(header):
            raise ValueError("duplicate column")
        order = sorted(range(len(header)), key=lambda i: header[i])
        total, count = 0, 0
        for row in rows:
            if not row:
                continue
            if len(row) != len(header):
                raise ValueError("ragged row")
            digest = hashlib.sha256(json.dumps([row[i] for i in order], ensure_ascii=False).encode("utf-8")).digest()
            total = (total + int.from_bytes(digest, "big")) % _MOD
            count += 1
    key = json.dumps([[header[i] for i in order], count, format(total, "064x")], ensure_ascii=False)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _file(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> dict:
    if info.filename.lower().endswith(".txt"):
        try:
            return {"method": "canonical_csv", "hash": _csv(zf, info)}
        except (UnicodeDecodeError, csv.Error, ValueError, StopIteration):
            pass
    return {"method": "raw_bytes", "hash": _raw(zf, info)}


def content_signature(zip_path: Path, zip_sha256: str) -> dict:
    """Signature record for a downloaded ZIP; `zip_sha256` ties it to the analysed bytes."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [info for info in zf.infolist() if not info.is_dir()]
            if len({info.filename for info in members}) != len(members):
                raise zipfile.BadZipFile("duplicate member names")
            files = {info.filename: _file(zf, info) for info in members}
        parts = [[name, v["method"], v["hash"]] for name, v in sorted(files.items())]
        signature = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, OSError, EOFError, zlib.error):
        files = None  # not a readable ZIP: the whole file is the content
        signature = zip_sha256
    return {"schema": SCHEMA, "zip_sha256": zip_sha256, "signature": signature, "files": files}
