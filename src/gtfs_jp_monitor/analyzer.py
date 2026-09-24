"""GTFS Analyzer CLI: pinned release, safe installation and one validation run (data-model §6).

Only binaries extracted from a release archive whose SHA-256 matches `analyzer.lock.json`
count as pinned. Anything else (e.g. a local build) is "unpinned" and must never write to
the data repository.
"""

from __future__ import annotations

import hashlib
import json
import platform as _platform
import re
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

LOCK_SCHEMA = "gtfs-jp-monitor-analyzer-lock/1"
BINARY_NAME = "gtfs-analyzer"
PROFILES = ("auto", "v3", "v4")

# Exit codes of `gtfs-analyzer validate` (data-model §6): 0 clean, 1 notices, 2 fatal or CLI error.
EXIT_OK = frozenset({0, 1})
EXIT_FATAL = 2


class AnalyzerError(RuntimeError):
    """Installation or invocation of the analyzer failed."""


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def current_platform() -> tuple[str, str]:
    system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(_platform.system())
    machine = {"x86_64": "x86_64", "AMD64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(_platform.machine())
    if system is None or machine is None:
        raise AnalyzerError(f"unsupported platform {_platform.system()}/{_platform.machine()}")
    return system, machine


@dataclass(frozen=True)
class Lock:
    repository: str
    release_tag: str
    version: str
    production_platform: str
    assets: dict[str, dict[str, str]]

    @classmethod
    def load(cls, path: Path) -> "Lock":
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        if doc.get("schema") != LOCK_SCHEMA:
            raise AnalyzerError(f"{path}: unexpected schema {doc.get('schema')!r}")
        if doc["release_tag"] != f"v{doc['version']}":
            raise AnalyzerError(f"{path}: release_tag {doc['release_tag']!r} does not match version {doc['version']!r}")
        return cls(doc["repository"], doc["release_tag"], doc["version"], doc["production_platform"], doc["assets"])

    def asset_for(self, os_name: str, arch: str) -> dict[str, str]:
        key = f"{arch}-{os_name}"
        if key not in self.assets:
            raise AnalyzerError(f"no pinned asset for {key}")
        return self.assets[key]

    def download_url(self, os_name: str, arch: str) -> str:
        name = self.asset_for(os_name, arch)["name"]
        return f"https://github.com/{self.repository}/releases/download/{self.release_tag}/{name}"


def install_from_archive(archive: Path, expected_sha256: str, dest_dir: Path) -> Path:
    """Verify a release archive and extract only the analyzer binary. Returns the binary path."""
    actual = sha256_file(archive)
    if actual != expected_sha256:
        raise AnalyzerError(f"archive checksum mismatch: expected {expected_sha256}, got {actual}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / BINARY_NAME
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name in (BINARY_NAME, f"./{BINARY_NAME}")]
        if len(members) != 1 or not members[0].isfile():
            raise AnalyzerError(f"archive must contain exactly one regular file named {BINARY_NAME}")
        source = tar.extractfile(members[0])
        if source is None:
            raise AnalyzerError("cannot read analyzer binary from archive")
        with source, open(target, "wb") as out:
            shutil.copyfileobj(source, out)
    target.chmod(0o755)
    return target


INSTALL_MARKER = "INSTALLED.json"


def write_install_marker(binary: Path, lock: Lock) -> Path:
    """Record which pinned release a freshly installed binary came from."""
    marker = Path(binary).parent / INSTALL_MARKER
    marker.write_text(
        json.dumps({"release_tag": lock.release_tag, "binary_sha256": sha256_file(binary)}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return marker


def pinned_release_for(binary: Path, lock: Lock) -> str | None:
    """The lock's release tag if `binary` is the one installed from it, else None.

    Both the marker and the binary hash are checked, so copying a marker next to another
    binary (or replacing the binary afterwards) does not make it pinned.
    """
    marker = Path(binary).parent / INSTALL_MARKER
    if not marker.is_file():
        return None
    try:
        doc = json.loads(marker.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if doc.get("release_tag") != lock.release_tag or doc.get("binary_sha256") != sha256_file(binary):
        return None
    return lock.release_tag


def install_release(lock: Lock, dest_dir: Path, fetch: Callable[[str, Path], None]) -> Path:
    """Download the pinned archive for this platform, verify it, extract the binary and mark it."""
    os_name, arch = current_platform()
    asset = lock.asset_for(os_name, arch)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive = dest_dir / asset["name"]
    try:
        fetch(lock.download_url(os_name, arch), archive)
        binary = install_from_archive(archive, asset["sha256"], dest_dir)
    finally:
        archive.unlink(missing_ok=True)
    write_install_marker(binary, lock)
    return binary


@dataclass(frozen=True)
class AnalyzerBinary:
    path: Path
    version: str
    sha256: str
    os: str
    arch: str
    pinned_release: str | None  # release tag when the binary matches the lock, else None

    @property
    def is_pinned(self) -> bool:
        return self.pinned_release is not None

    @classmethod
    def inspect(cls, path: Path, pinned_release: str | None = None, timeout: float = 30) -> "AnalyzerBinary":
        path = Path(path)
        try:
            proc = subprocess.run([str(path), "--version"], capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise AnalyzerError(f"cannot run {path}: {err}") from err
        match = re.search(rb"\b([0-9]+\.[0-9]+\.[0-9]+)\b", proc.stdout)
        if proc.returncode != 0 or match is None:
            raise AnalyzerError(f"unexpected --version output from {path}: {proc.stdout[:200]!r}")
        version = match.group(1).decode()
        if pinned_release is not None and pinned_release != f"v{version}":
            raise AnalyzerError(f"binary reports {version}, lock pins {pinned_release}")
        os_name, arch = current_platform()
        return cls(path, version, sha256_file(path), os_name, arch, pinned_release)


@dataclass(frozen=True)
class AnalyzerRun:
    report: dict | None
    exit_code: int | None
    duration_ms: int
    error_code: str | None  # TIMEOUT, ANALYZER_CRASH, INVALID_REPORT, ... when report is None
    stderr_tail: str


def iso_to_today_arg(iso_date: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", iso_date or ""):
        raise ValueError(f"expected YYYY-MM-DD, got {iso_date!r}")
    return iso_date.replace("-", "")


def run_validate(
    binary: AnalyzerBinary,
    zip_path: Path,
    validate_date: str,
    profile: str,
    workdir: Path,
    timeout: float = 600,
) -> AnalyzerRun:
    """Run `validate --json` with an explicit --today; never lets the analyzer pick the date."""
    if profile not in PROFILES:
        raise ValueError(f"unknown GTFS-JP profile {profile!r}")
    out = Path(workdir) / "report.json"
    out.unlink(missing_ok=True)
    cmd = [
        str(binary.path), "validate", str(zip_path), "--json",
        "--today", iso_to_today_arg(validate_date),
        "--gtfs-jp-profile", profile,
        "--lang", "en",
        "-o", str(out),
    ]
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as err:
        tail = (err.stderr or b"")[-2000:].decode("utf-8", "replace")
        return AnalyzerRun(None, None, int((time.monotonic() - start) * 1000), "TIMEOUT", tail)
    duration_ms = int((time.monotonic() - start) * 1000)
    tail = proc.stderr[-2000:].decode("utf-8", "replace")
    if proc.returncode not in EXIT_OK and proc.returncode != EXIT_FATAL:
        return AnalyzerRun(None, proc.returncode, duration_ms, "ANALYZER_CRASH", tail)
    if not out.is_file():
        return AnalyzerRun(None, proc.returncode, duration_ms, "NO_REPORT", tail)
    try:
        report = json.loads(out.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return AnalyzerRun(None, proc.returncode, duration_ms, "INVALID_REPORT", tail)
    finally:
        out.unlink(missing_ok=True)
    status = report.get("status") if isinstance(report, dict) else None
    # Exit 2 must come with a fatal report and vice versa; anything else is a CLI error.
    if (proc.returncode == EXIT_FATAL) != (status == "fatal"):
        return AnalyzerRun(None, proc.returncode, duration_ms, "INCONSISTENT_EXIT", tail)
    return AnalyzerRun(report, proc.returncode, duration_ms, None, tail)
