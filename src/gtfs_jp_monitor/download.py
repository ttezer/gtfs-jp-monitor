"""Temporary download of one generation ZIP (data-model §1).

The gtfs_url answers 302 to a signed S3 URL that carries temporary AWS credentials. That URL
is followed but never logged, returned or stored. Only https redirects are accepted and the
size is capped both by Content-Length and while streaming.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from .gtfsdatajp import USER_AGENT, is_api_download_url

DEFAULT_MAX_BYTES = 512 * 1024 * 1024
CHUNK = 1 << 16


class DownloadError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code  # SOURCE_UNAVAILABLE, TOO_LARGE, DOWNLOAD_FAILED, UNTRUSTED_URL
        self.detail = detail


@dataclass(frozen=True)
class Downloaded:
    path: Path
    sha256: str
    size: int


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            # Do not echo newurl: a signed URL must not reach logs or error messages.
            raise urllib.error.URLError("refused non-https redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


Opener = Callable[[str, float], tuple[int, dict[str, str], BinaryIO]]


def urllib_opener(url: str, timeout: float) -> tuple[int, dict[str, str], BinaryIO]:
    opener = urllib.request.build_opener(_HttpsOnlyRedirects())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as err:
        return err.code, {}, err
    return response.status, dict(response.headers.items()), response


def download_zip(
    url: str,
    dest: Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: float = 300,
    opener: Opener = urllib_opener,
) -> Downloaded:
    """Stream `url` to `dest`, hashing on the way. `dest` is removed on any failure."""
    if not is_api_download_url(url):
        raise DownloadError("UNTRUSTED_URL", "download URL is not on the gtfs-data.jp API host")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        status, headers, stream = opener(url, timeout)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
        raise DownloadError("DOWNLOAD_FAILED", type(err).__name__) from None
    try:
        if status in (404, 410):
            raise DownloadError("SOURCE_UNAVAILABLE", f"HTTP {status}")
        if status != 200:
            raise DownloadError("DOWNLOAD_FAILED", f"HTTP {status}")
        length = {k.lower(): v for k, v in headers.items()}.get("content-length")
        if length is not None and length.strip().isdigit() and int(length) > max_bytes:
            raise DownloadError("TOO_LARGE", f"Content-Length {int(length)} exceeds {max_bytes}")
        digest = hashlib.sha256()
        size = 0
        with open(dest, "wb") as out:
            while True:
                try:
                    block = stream.read(CHUNK)
                except (TimeoutError, ConnectionError, OSError) as err:
                    raise DownloadError("DOWNLOAD_FAILED", type(err).__name__) from None
                if not block:
                    break
                size += len(block)
                if size > max_bytes:
                    raise DownloadError("TOO_LARGE", f"stream exceeds {max_bytes} bytes")
                digest.update(block)
                out.write(block)
        if size == 0:
            raise DownloadError("DOWNLOAD_FAILED", "empty response")
        return Downloaded(dest, digest.hexdigest(), size)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
