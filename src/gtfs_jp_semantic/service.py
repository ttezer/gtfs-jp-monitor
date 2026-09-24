"""Service days, day types, periods and the comparison window (docs/semantic/02-matching.md §1-2)."""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from datetime import date, timedelta

from .holidays import DAY_TYPES, HolidayTable
from .reader import Config, Table

WEEKDAY_COLUMNS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_yyyymmdd(value: str) -> date | None:
    value = (value or "").strip()
    if len(value) != 8 or not value.isdigit():
        return None
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        return None


def _rows(table: Table | None) -> list[dict[str, str]]:
    if table is None or table.status != "ok":
        return []
    return [dict(zip(table.header, row)) for row in table.rows]


@dataclass(frozen=True)
class Period:
    day_type: str
    start: date
    end: date
    services: frozenset[str]


@dataclass
class ServiceCalendar:
    window: tuple[date, date] | None
    days: dict[date, frozenset[str]]
    day_types: dict[date, str]
    special: set[date]
    periods: list[Period]
    notes: list[str] = field(default_factory=list)

    def active_days(self, day_type: str) -> int:
        return sum(1 for d, s in self.days.items() if s and self.day_types[d] == day_type)


def _window(tables: dict[str, Table], validity: tuple[date | None, date | None] | None,
            service_dates: set[date]) -> tuple[date, date] | None:
    info = _rows(tables.get("feed_info.txt"))
    if info:
        start, end = parse_yyyymmdd(info[0].get("feed_start_date", "")), parse_yyyymmdd(info[0].get("feed_end_date", ""))
        if start and end and start <= end:
            return start, end
    if validity and validity[0] and validity[1] and validity[0] <= validity[1]:
        return validity[0], validity[1]
    if service_dates:
        return min(service_dates), max(service_dates)
    return None


def build_calendar(tables: dict[str, Table], holidays: HolidayTable, config: Config,
                   validity: tuple[date | None, date | None] | None = None) -> ServiceCalendar:
    trips = _rows(tables.get("trips.txt"))
    used = {t.get("service_id", "") for t in trips} - {""} if trips else None

    active: dict[str, set[date]] = collections.defaultdict(set)
    for row in _rows(tables.get("calendar.txt")):
        sid = row.get("service_id", "")
        start, end = parse_yyyymmdd(row.get("start_date", "")), parse_yyyymmdd(row.get("end_date", ""))
        if not sid or not start or not end or start > end:
            continue
        flags = [row.get(c, "0").strip() == "1" for c in WEEKDAY_COLUMNS]
        d = start
        while d <= end:
            if flags[d.weekday()]:
                active[sid].add(d)
            d += timedelta(days=1)
    for row in _rows(tables.get("calendar_dates.txt")):
        sid, d, kind = row.get("service_id", ""), parse_yyyymmdd(row.get("date", "")), row.get("exception_type", "").strip()
        if not sid or not d:
            continue
        if kind == "1":
            active[sid].add(d)
        elif kind == "2":
            active[sid].discard(d)

    services = {s: ds for s, ds in active.items() if used is None or s in used}
    notes: list[str] = []
    window = _window(tables, validity, set().union(*services.values()) if services else set())
    if window is None:
        return ServiceCalendar(None, {}, {}, set(), [], ["NO_SERVICE_DAYS"])
    start, end = window
    lo, hi = date(holidays.first_year, 1, 1), date(holidays.last_year, 12, 31)
    if start < lo or end > hi:
        start, end = max(start, lo), min(end, hi)
        notes.append("WINDOW_CLIPPED_TO_HOLIDAY_TABLE")
        if start > end:
            return ServiceCalendar(None, {}, {}, set(), [], notes)

    by_date: dict[date, set[str]] = collections.defaultdict(set)
    for sid, ds in services.items():
        for d in ds:
            if start <= d <= end:
                by_date[d].add(sid)
    days: dict[date, frozenset[str]] = {}
    day_types: dict[date, str] = {}
    d = start
    while d <= end:
        days[d] = frozenset(by_date.get(d, ()))
        day_types[d] = holidays.day_type(d)
        d += timedelta(days=1)

    special = _special_days(days, day_types, config.special_max_days)
    return ServiceCalendar((start, end), days, day_types, special, _periods(days, day_types, special), notes)


def _special_days(days: dict[date, frozenset[str]], day_types: dict[date, str], max_days: int) -> set[date]:
    """Dates whose service set is rare for their day type, while a regular set exists for it."""
    special: set[date] = set()
    for dt in DAY_TYPES:
        counts = collections.Counter(s for d, s in days.items() if day_types[d] == dt)
        if not counts or max(counts.values()) < max_days:
            continue
        rare = {s for s, n in counts.items() if n < max_days}
        special.update(d for d, s in days.items() if day_types[d] == dt and s in rare)
    return special


def _periods(days: dict[date, frozenset[str]], day_types: dict[date, str], special: set[date]) -> list[Period]:
    periods: list[Period] = []
    for dt in DAY_TYPES:
        current: Period | None = None
        for d in sorted(x for x in days if day_types[x] == dt and x not in special):
            s = days[d]
            if current is not None and current.services == s:
                current = Period(dt, current.start, d, s)
            else:
                if current is not None:
                    periods.append(current)
                current = Period(dt, d, d, s)
        if current is not None:
            periods.append(current)
    return sorted(periods, key=lambda p: (DAY_TYPES.index(p.day_type), p.start))


@dataclass(frozen=True)
class DayChoice:
    old_date: date | None
    new_date: date | None
    old_services: frozenset[str]
    new_services: frozenset[str]


@dataclass(frozen=True)
class Comparison:
    mode: str  # same_days | successive_periods
    day_types: dict[str, DayChoice]


def _typical(cal: ServiceCalendar, dt: str, dates: set[date] | None, latest: bool = False) -> tuple[date | None, frozenset[str]]:
    """Most frequent service set of a day type (ties: earliest occurrence) and its earliest (or latest) date."""
    candidates = sorted(d for d in cal.days if cal.day_types[d] == dt and d not in cal.special and (dates is None or d in dates))
    if not candidates:
        return None, frozenset()
    counts = collections.Counter(cal.days[d] for d in candidates)
    first_seen = {}
    for d in candidates:
        first_seen.setdefault(cal.days[d], d)
    best = max(counts, key=lambda s: (counts[s], -first_seen[s].toordinal()))
    if latest:
        return max(d for d in candidates if cal.days[d] == best), best
    return first_seen[best], best


def choose_comparison(old: ServiceCalendar, new: ServiceCalendar, config: Config) -> Comparison:
    overlap = set(old.days) & set(new.days)
    choices: dict[str, DayChoice] = {}
    if len(overlap) >= config.min_overlap_days:
        for dt in DAY_TYPES:
            od, os_ = _typical(old, dt, overlap)
            nd, ns = _typical(new, dt, overlap)
            choices[dt] = DayChoice(od, nd, os_, ns)
        return Comparison("same_days", choices)
    # Each side's dominant timetable: short periods (school holidays, a bundled previous timetable)
    # must not stand in for the regular one.
    for dt in DAY_TYPES:
        od, os_ = _typical(old, dt, None, latest=True)
        nd, ns = _typical(new, dt, None)
        choices[dt] = DayChoice(od, nd, os_, ns)
    return Comparison("successive_periods", choices)
