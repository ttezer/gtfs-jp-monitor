"""Service days, day types, periods and the comparison window (docs/semantic/02-matching.md §1-2).

Day types come from each feed's own calendar: calendar categories (weekdays and national
holidays) that run the same services in the same weeks form one day type, for example
"mon,tue,wed,thu,fri" / "sat" / "sun,hol", or "mon" / "tue,wed,thu,fri" for a feed with a
Tuesday-Friday timetable. Two publications are compared on the common refinement of their
day types.
"""

from __future__ import annotations

import bisect
import collections
from dataclasses import dataclass, field
from datetime import date, timedelta

from .holidays import CATEGORIES, HolidayTable
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


def day_type_id(categories) -> str:
    """Stable id of a group of categories, in calendar order: "mon,tue,wed,thu,fri"."""
    return ",".join(c for c in CATEGORIES if c in set(categories))


def day_type_order(day_type: str) -> tuple[int, ...]:
    return tuple(CATEGORIES.index(c) for c in day_type.split(","))


@dataclass
class ServiceCalendar:
    window: tuple[date, date] | None
    days: dict[date, frozenset[str]]
    categories: dict[date, str]
    groups: dict[str, str]  # category -> day type id of this publication
    special: set[date]
    periods: list[Period]
    notes: list[str] = field(default_factory=list)
    service_dates: dict[str, frozenset[date]] = field(default_factory=dict)  # clipped to the window

    @property
    def day_types(self) -> dict[date, str]:
        return {d: self.groups[c] for d, c in self.categories.items()}

    def active_days(self, categories) -> int:
        """Dates with service whose category is one of `categories`."""
        wanted = set(categories)
        return sum(1 for d, s in self.days.items() if s and self.categories[d] in wanted)


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
        return ServiceCalendar(None, {}, {}, {}, set(), [], ["NO_SERVICE_DAYS"])
    start, end = window
    lo, hi = date(holidays.first_year, 1, 1), date(holidays.last_year, 12, 31)
    if start < lo or end > hi:
        start, end = max(start, lo), min(end, hi)
        notes.append("WINDOW_CLIPPED_TO_HOLIDAY_TABLE")
        if start > end:
            return ServiceCalendar(None, {}, {}, {}, set(), [], notes)

    by_date: dict[date, set[str]] = collections.defaultdict(set)
    for sid, ds in services.items():
        for d in ds:
            if start <= d <= end:
                by_date[d].add(sid)
    days: dict[date, frozenset[str]] = {}
    categories: dict[date, str] = {}
    d = start
    while d <= end:
        days[d] = frozenset(by_date.get(d, ()))
        categories[d] = holidays.category(d)
        d += timedelta(days=1)

    groups = _day_groups(days, categories, config.matching["day_group_min_agreement"])
    day_types = {d: groups[c] for d, c in categories.items()}
    special = _special_days(days, day_types, config.special_max_days)
    service_dates = {sid: frozenset(d for d in ds if start <= d <= end) for sid, ds in services.items()}
    return ServiceCalendar((start, end), days, categories, groups, special, _periods(days, day_types, special), notes,
                           {sid: ds for sid, ds in service_dates.items() if ds})


def _day_groups(days: dict[date, frozenset[str]], categories: dict[date, str], min_agreement: float) -> dict[str, str]:
    """category -> day type id.

    Each date is compared with the nearest date (at most 7 days away) of every other category;
    two categories join when at least `min_agreement` of these comparisons find the same
    services. A holiday is thus compared with the weekday it replaced one week before or after.
    Isolated exceptions (a date whose services differ from both neighbouring dates of its own
    category) take no part; they are special days. Two dates without service say nothing;
    categories that never run form one day type. Joins are transitive.
    """
    by_cat: dict[str, list[date]] = collections.defaultdict(list)
    for d in sorted(days):
        by_cat[categories[d]].append(d)
    usable: dict[str, list[date]] = {}
    for c, ds in by_cat.items():
        usable[c] = [d for i, d in enumerate(ds)
                     if not (0 < i < len(ds) - 1 and days[d] != days[ds[i - 1]] and days[d] != days[ds[i + 1]])]
    present = [c for c in CATEGORIES if usable.get(c)]

    def nearest(ds: list[date], d: date) -> date | None:
        i = bisect.bisect_left(ds, d)
        best = [x for x in ds[max(0, i - 1):i + 1] if abs((x - d).days) <= 7]
        return min(best, key=lambda x: (abs((x - d).days), x)) if best else None

    parent = {c: c for c in present}

    def find(c: str) -> str:
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    for i, a in enumerate(present):
        for b in present[i + 1:]:
            same = total = 0
            for x, y in ((a, b), (b, a)):
                for d in usable[x]:
                    e = nearest(usable[y], d)
                    if e is not None and (days[d] or days[e]):  # two days without service say nothing
                        total += 1
                        same += days[d] == days[e]
            if total and same / total >= min_agreement:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb, key=CATEGORIES.index)] = min(ra, rb, key=CATEGORIES.index)
    idle = [c for c in present if not any(days[d] for d in usable[c])]
    for c in idle[1:]:  # categories that never run form one day type
        ra, rb = find(idle[0]), find(c)
        if ra != rb:
            parent[max(ra, rb, key=CATEGORIES.index)] = min(ra, rb, key=CATEGORIES.index)
    members: dict[str, list[str]] = collections.defaultdict(list)
    for c in present:
        members[find(c)].append(c)
    groups = {c: day_type_id(members[find(c)]) for c in present}
    for c in by_cat:  # a category whose every date is an exception stands alone
        groups.setdefault(c, c)
    return groups


def _special_days(days: dict[date, frozenset[str]], day_types: dict[date, str], max_days: int) -> set[date]:
    """Dates whose service set is rare for their day type, while a regular set exists for it."""
    special: set[date] = set()
    for dt in sorted(set(day_types.values()), key=day_type_order):
        counts = collections.Counter(s for d, s in days.items() if day_types[d] == dt)
        if not counts or max(counts.values()) < max_days:
            continue
        rare = {s for s, n in counts.items() if n < max_days}
        special.update(d for d, s in days.items() if day_types[d] == dt and s in rare)
    return special


def _periods(days: dict[date, frozenset[str]], day_types: dict[date, str], special: set[date]) -> list[Period]:
    periods: list[Period] = []
    for dt in sorted(set(day_types.values()), key=day_type_order):
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
    return sorted(periods, key=lambda p: (day_type_order(p.day_type), p.start))


@dataclass(frozen=True)
class Irregular:
    service_id: str
    dates: frozenset[date]
    mode: str  # adds | replaces | mixed: runs besides regular services, instead of them, or both


def irregular_services(cal: ServiceCalendar, min_days: int) -> list[Irregular]:
    """Services that are part of no regular timetable: no period (a run of dates of one day
    type with the same services) containing them lasts `min_days` dates. Services on scattered
    dates stay irregular even when they add up to many days. Whether they add to the regular
    services or replace them is judged on their own dates."""
    day_types = cal.day_types
    regular: set[str] = set()
    for p in cal.periods:
        n = sum(1 for d in cal.days if p.start <= d <= p.end and day_types[d] == p.day_type and d not in cal.special)
        if n >= min_days:
            regular |= p.services
    out = []
    for sid, dates in sorted(cal.service_dates.items()):
        if sid in regular:
            continue
        beside = sum(1 for d in dates if cal.days.get(d, frozenset()) & regular)
        mode = "adds" if beside == len(dates) else "replaces" if beside == 0 else "mixed"
        out.append(Irregular(sid, dates, mode))
    return sorted(out, key=lambda x: (min(x.dates), x.service_id))


@dataclass(frozen=True)
class DayChoice:
    old_date: date | None
    new_date: date | None
    old_services: frozenset[str]
    new_services: frozenset[str]


@dataclass(frozen=True)
class Comparison:
    mode: str  # same_days | successive_periods
    day_types: dict[str, DayChoice]  # common day type id -> typical days, in calendar order

    def day_type_of(self, category: str) -> str | None:
        return next((dt for dt in self.day_types if category in dt.split(",")), None)


def common_day_types(old: ServiceCalendar, new: ServiceCalendar) -> list[str]:
    """The common refinement of both publications' day types: categories that share a day type
    on both sides stay together."""
    by_pair: dict[tuple, list[str]] = collections.defaultdict(list)
    for c in CATEGORIES:
        if c in old.groups or c in new.groups:
            by_pair[(old.groups.get(c), new.groups.get(c))].append(c)
    return sorted((day_type_id(cs) for cs in by_pair.values()), key=day_type_order)


def _typical(cal: ServiceCalendar, dt: str, dates: set[date] | None, latest: bool = False) -> tuple[date | None, frozenset[str]]:
    """Most frequent service set of a day type (ties: earliest occurrence) and its earliest (or latest) date."""
    members = set(dt.split(","))
    candidates = sorted(d for d in cal.days if cal.categories[d] in members and d not in cal.special and (dates is None or d in dates))
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
    common = common_day_types(old, new)
    if len(overlap) >= config.min_overlap_days:
        for dt in common:
            od, os_ = _typical(old, dt, overlap)
            nd, ns = _typical(new, dt, overlap)
            choices[dt] = DayChoice(od, nd, os_, ns)
        return Comparison("same_days", choices)
    # Each side's dominant timetable: short periods (school holidays, a bundled previous timetable)
    # must not stand in for the regular one.
    for dt in common:
        od, os_ = _typical(old, dt, None, latest=True)
        nd, ns = _typical(new, dt, None)
        choices[dt] = DayChoice(od, nd, os_, ns)
    return Comparison("successive_periods", choices)
