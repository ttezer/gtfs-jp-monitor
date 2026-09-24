"""Japanese national holidays and day types (docs/semantic/02-matching.md §1).

The table is the Cabinet Office list, stored as a versioned data file of the engine. Dates in
years the table does not cover raise instead of being treated as ordinary days, so an outdated
table is noticed immediately.
"""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).with_name("data") / "jp_holidays.json"
SCHEMA = "gtfs-jp-semantic-holidays/1"
DAY_TYPES = ("weekday", "saturday", "sunday_holiday")


class HolidayTableOutOfRange(ValueError):
    """The date lies in a year the holiday table does not cover."""


class HolidayTable:
    def __init__(self, doc: dict) -> None:
        if doc.get("schema") != SCHEMA:
            raise ValueError(f"unexpected holiday table schema {doc.get('schema')!r}")
        self.version: str = doc["version"]
        self.first_year: int = doc["first_year"]
        self.last_year: int = doc["last_year"]
        self._dates = frozenset(date.fromisoformat(d) for d in doc["holidays"])

    @classmethod
    def load(cls, path: Path = DATA_FILE) -> "HolidayTable":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def is_holiday(self, day: date) -> bool:
        if not self.first_year <= day.year <= self.last_year:
            raise HolidayTableOutOfRange(
                f"{day.isoformat()} is outside the holiday table ({self.first_year}-{self.last_year}, {self.version})"
            )
        return day in self._dates

    def day_type(self, day: date) -> str:
        """weekday, saturday or sunday_holiday. Holidays on any weekday count as sunday_holiday."""
        if day.weekday() == 6 or self.is_holiday(day):
            return "sunday_holiday"
        if day.weekday() == 5:
            return "saturday"
        return "weekday"


@lru_cache(maxsize=1)
def default_table() -> HolidayTable:
    return HolidayTable.load()
