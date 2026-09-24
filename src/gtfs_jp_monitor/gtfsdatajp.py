"""gtfs-data.jp API v2 client (data-model §7).

Every response is untrusted: identifiers are validated, contact fields are dropped, and
download URLs must point to the API host. The client is polite by default (one request
per second, bounded retries honouring Retry-After).
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .ids import InvalidIdentifier, is_path_id, is_uid, rid_rank

BASE_URL = "https://api.gtfs-data.jp/v2"
API_HOST = "api.gtfs-data.jp"
USER_AGENT = "gtfs-jp-monitor (+https://github.com/ttezer/gtfs-jp-monitor)"

# Large enough to return the whole history; the API has no practical upper bound (data-model §1).
ALL_PREV = 10_000
ALL_NEXT = 1_000

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

Transport = Callable[[str, float], tuple[int, dict[str, str], bytes]]


class ApiError(RuntimeError):
    """The API answered with an unusable status or body."""


class NotFound(ApiError):
    """The API answered 404."""


@dataclass(frozen=True)
class Rejected:
    """An API record that was skipped instead of trusted."""

    code: str
    org_id: str | None
    feed_id: str | None
    uid: str | None
    detail: str


@dataclass(frozen=True)
class FeedRecord:
    org_id: str
    feed_id: str
    feed_name: str
    organization_name: str
    feed_pref_id: int | None
    license_raw: str
    license_url: str | None
    is_discontinued: bool
    discontinued_date: str | None
    last_published_at: str | None
    latest_feed_start_date: str | None
    latest_feed_end_date: str | None
    memo: str


@dataclass(frozen=True)
class GenerationRecord:
    uid: str
    rid: str
    gtfs_url: str
    from_date: str | None
    to_date: str | None
    published_at: str | None
    memo: str


@dataclass(frozen=True)
class FeedGenerations:
    org_id: str
    feed_id: str
    license_raw: str
    reported_max_prev: int | None
    reported_max_next: int | None
    generations: tuple[GenerationRecord, ...]
    rejected: tuple[Rejected, ...] = field(default=())

    @property
    def complete(self) -> bool:
        """True when the returned prev_/next_ counts match what the API says exists."""
        prev = sum(1 for g in self.generations if g.rid.startswith("prev_"))
        nxt = sum(1 for g in self.generations if g.rid.startswith("next_"))
        rejected_prev = sum(1 for r in self.rejected if r.detail.startswith("rid=prev_"))
        rejected_next = sum(1 for r in self.rejected if r.detail.startswith("rid=next_"))
        return (self.reported_max_prev is None or prev + rejected_prev == self.reported_max_prev) and (
            self.reported_max_next is None or nxt + rejected_next == self.reported_max_next
        )


def urllib_transport(url: str, timeout: float) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers.items()) if err.headers else {}, err.read() or b""


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def is_api_download_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and parts.hostname == API_HOST and parts.username is None


class GtfsDataJpClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        transport: Transport = urllib_transport,
        min_interval: float = 1.0,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        max_backoff: float = 60.0,
        timeout: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._max_backoff = max_backoff
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self.request_count = 0

    # --- transport with politeness and retries ---

    def _wait_turn(self) -> None:
        if self._last_request is not None:
            remaining = self._min_interval - (self._clock() - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = self._clock()

    def _retry_delay(self, attempt: int, headers: dict[str, str]) -> float:
        retry_after = {k.lower(): v for k, v in headers.items()}.get("retry-after")
        if retry_after is not None and retry_after.strip().isdigit():
            return min(float(retry_after.strip()), self._max_backoff)
        return min(self._backoff_base**attempt, self._max_backoff)

    def get_json(self, path: str, params: dict[str, object] | None = None) -> object:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last_error = ""
        for attempt in range(self._max_retries + 1):
            self._wait_turn()
            self.request_count += 1
            try:
                status, headers, body = self._transport(url, self._timeout)
            except (ssl.SSLCertVerificationError, urllib.error.URLError) as err:
                reason = getattr(err, "reason", err)
                if isinstance(reason, ssl.SSLCertVerificationError):
                    # Not transient: retrying cannot fix a missing or wrong CA bundle.
                    raise ApiError(f"TLS certificate verification failed for {path}: {reason}") from err
                status, headers, body = None, {}, b""
                last_error = f"network error: {err}"
            except (TimeoutError, ConnectionError, OSError) as err:
                status, headers, body = None, {}, b""
                last_error = f"network error: {err}"
            if status == 200:
                try:
                    return json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as err:
                    raise ApiError(f"invalid JSON from {path}: {err}") from err
            if status == 404:
                raise NotFound(f"404 for {path}")
            if status is not None and status not in RETRY_STATUSES:
                raise ApiError(f"HTTP {status} for {path}")
            if status is not None:
                last_error = f"HTTP {status}"
            if attempt < self._max_retries:
                self._sleep(self._retry_delay(attempt, headers))
        raise ApiError(f"giving up on {path} after {self._max_retries + 1} attempts: {last_error}")

    @staticmethod
    def _body(payload: object, path: str) -> object:
        if not isinstance(payload, dict) or "body" not in payload:
            raise ApiError(f"unexpected envelope for {path}")
        return payload["body"]

    # --- endpoints ---

    def list_feeds(self) -> tuple[list[FeedRecord], list[Rejected]]:
        body = self._body(self.get_json("/feeds"), "/feeds")
        if not isinstance(body, list):
            raise ApiError("/feeds body is not a list")
        feeds: list[FeedRecord] = []
        rejected: list[Rejected] = []
        for item in body:
            if not isinstance(item, dict):
                rejected.append(Rejected("FEED_NOT_OBJECT", None, None, None, type(item).__name__))
                continue
            org_id, feed_id = item.get("organization_id"), item.get("feed_id")
            if not (is_path_id(org_id) and is_path_id(feed_id)):
                rejected.append(
                    Rejected("INVALID_FEED_ID", _opt_str(org_id), _opt_str(feed_id), None, "id does not match the path pattern")
                )
                continue
            pref = item.get("feed_pref_id")
            feeds.append(
                FeedRecord(
                    org_id=org_id,
                    feed_id=feed_id,
                    feed_name=_str(item.get("feed_name")),
                    organization_name=_str(item.get("organization_name")),
                    feed_pref_id=pref if isinstance(pref, int) and not isinstance(pref, bool) else None,
                    license_raw=_str(item.get("feed_license")),
                    license_url=_opt_str(item.get("feed_license_url")),
                    is_discontinued=item.get("feed_is_discontinued") is True,
                    discontinued_date=_opt_str(item.get("feed_discontinued_date")),
                    last_published_at=_opt_str(item.get("last_published_at")),
                    latest_feed_start_date=_opt_str(item.get("latest_feed_start_date")),
                    latest_feed_end_date=_opt_str(item.get("latest_feed_end_date")),
                    memo=_str(item.get("feed_memo")),
                )
            )
        return feeds, rejected

    def get_generations(self, org_id: str, feed_id: str, max_prev: int = ALL_PREV, max_next: int = ALL_NEXT) -> FeedGenerations:
        if not (is_path_id(org_id) and is_path_id(feed_id)):
            raise InvalidIdentifier(f"invalid feed path {org_id!r}/{feed_id!r}")
        path = f"/organizations/{org_id}/feeds/{feed_id}"
        body = self._body(self.get_json(path, {"max_prev": max_prev, "max_next": max_next}), path)
        if not isinstance(body, dict) or not isinstance(body.get("gtfs_files"), list):
            raise ApiError(f"{path}: gtfs_files missing")
        generations: list[GenerationRecord] = []
        rejected: list[Rejected] = []
        seen: set[str] = set()
        for item in body["gtfs_files"]:
            if not isinstance(item, dict):
                rejected.append(Rejected("GENERATION_NOT_OBJECT", org_id, feed_id, None, type(item).__name__))
                continue
            uid, rid, url = item.get("gtfs_file_uid"), item.get("rid"), item.get("gtfs_url")
            rid_note = f"rid={rid}" if isinstance(rid, str) else "rid=?"
            if not is_uid(uid):
                rejected.append(Rejected("INVALID_UID", org_id, feed_id, None, f"{rid_note} uid={uid!r}"))
                continue
            try:
                rid_rank(rid)
            except InvalidIdentifier:
                rejected.append(Rejected("INVALID_RID", org_id, feed_id, uid, rid_note))
                continue
            if not is_api_download_url(url):
                rejected.append(Rejected("UNTRUSTED_GTFS_URL", org_id, feed_id, uid, rid_note))
                continue
            if uid in seen:
                rejected.append(Rejected("DUPLICATE_UID", org_id, feed_id, uid, rid_note))
                continue
            seen.add(uid)
            generations.append(
                GenerationRecord(
                    uid=uid,
                    rid=rid,
                    gtfs_url=url,
                    from_date=_opt_str(item.get("from_date")),
                    to_date=_opt_str(item.get("to_date")),
                    published_at=_opt_str(item.get("published_at")),
                    memo=_str(item.get("memo")),
                )
            )

        def _count(key: str) -> int | None:
            value = body.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

        return FeedGenerations(
            org_id=org_id,
            feed_id=feed_id,
            license_raw=_str(body.get("feed_license")),
            reported_max_prev=_count("max_prev"),
            reported_max_next=_count("max_next"),
            generations=tuple(generations),
            rejected=tuple(rejected),
        )
