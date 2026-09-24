"""Read a GTFS ZIP into exact string tables (docs/semantic/01-raw-diff.md, "Input reading")."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

CONFIG_SCHEMA = "gtfs-jp-semantic-config/1"
DEFAULT_CONFIG = Path(__file__).with_name("default_config.json")


class ArchiveError(ValueError):
    """The archive cannot be read at all (not a ZIP, too large when decompressed)."""


@dataclass(frozen=True)
class Config:
    bulk_threshold: int
    max_rows_per_file: int
    max_uncompressed_bytes: int

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG) -> "Config":
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if doc.get("schema") != CONFIG_SCHEMA:
            raise ValueError(f"{path}: unexpected schema {doc.get('schema')!r}")
        return cls(int(doc["bulk_threshold"]), int(doc["max_rows_per_file"]), int(doc["max_uncompressed_bytes"]))

    def as_dict(self) -> dict:
        return {
            "bulk_threshold": self.bulk_threshold,
            "max_rows_per_file": self.max_rows_per_file,
            "max_uncompressed_bytes": self.max_uncompressed_bytes,
        }


@dataclass
class Table:
    name: str
    sha256: str
    status: str  # ok | undecodable | too_large | duplicate_header
    encoding: str | None = None
    header: tuple[str, ...] = ()
    rows: list[tuple[str, ...]] = field(default_factory=list)
    row_count: int = 0
    ragged_rows: int = 0


@dataclass
class Feed:
    sha256: str
    tables: dict[str, Table]
    warnings: list[dict]


def _safe_member(name: str) -> bool:
    if "\\" in name or name.startswith("/"):
        return False
    return ".." not in PurePosixPath(name).parts


def _decode(data: bytes) -> tuple[str | None, str | None]:
    for encoding, codec in (("utf-8", "utf-8-sig"), ("cp932", "cp932")):
        try:
            return data.decode(codec), encoding
        except UnicodeDecodeError:
            continue
    return None, None


def _parse(name: str, data: bytes, config: Config) -> Table:
    table = Table(name=name, sha256=hashlib.sha256(data).hexdigest(), status="ok")
    text, encoding = _decode(data)
    if text is None:
        table.status = "undecodable"
        return table
    table.encoding = encoding
    reader = csv.reader(io.StringIO(text, newline=""))
    header_row = next(reader, None)
    if header_row is None:
        return table
    header = tuple(h.strip() for h in header_row)
    if len(set(header)) != len(header):
        table.status = "duplicate_header"
        return table
    table.header = header
    width = len(header)
    rows: list[tuple[str, ...]] = []
    for raw in reader:
        if not raw or all(v == "" for v in raw):
            continue
        if len(raw) > width:
            table.ragged_rows += 1
            raw = raw[:width]
        elif len(raw) < width:
            raw = raw + [""] * (width - len(raw))
        rows.append(tuple(raw))
        if len(rows) > config.max_rows_per_file:
            table.status = "too_large"
            table.rows = []
            table.row_count = sum(1 for _ in reader) + len(rows)
            return table
    table.rows = rows
    table.row_count = len(rows)
    return table


def read_feed(path: Path, config: Config) -> Feed:
    raw = Path(path).read_bytes()
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as err:
        raise ArchiveError(f"not a ZIP archive: {err}") from None
    warnings: list[dict] = []
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > config.max_uncompressed_bytes:
            raise ArchiveError("declared uncompressed size exceeds max_uncompressed_bytes")
        candidates = []
        for info in infos:
            name = info.filename
            if not _safe_member(name):
                warnings.append({"code": "unsafe_member_ignored", "member": name})
            elif name.lower().endswith(".txt"):
                candidates.append(info)
        root = [i for i in candidates if "/" not in i.filename]
        if root:
            chosen, rest = root, [i for i in candidates if "/" in i.filename]
        else:
            tops = {i.filename.split("/", 1)[0] for i in candidates}
            if len(tops) == 1 and all(i.filename.count("/") == 1 for i in candidates):
                chosen, rest = candidates, []
            else:
                chosen, rest = [], candidates
        for info in sorted(rest, key=lambda i: i.filename):
            warnings.append({"code": "archive_layout", "member": info.filename})
        tables: dict[str, Table] = {}
        for info in sorted(chosen, key=lambda i: i.filename):
            base = info.filename.rsplit("/", 1)[-1]
            if base in tables:
                warnings.append({"code": "duplicate_member_ignored", "member": info.filename})
                continue
            tables[base] = _parse(base, archive.read(info), config)
    warnings.sort(key=lambda w: (w["code"], w["member"]))
    return Feed(sha256=hashlib.sha256(raw).hexdigest(), tables=tables, warnings=warnings)
