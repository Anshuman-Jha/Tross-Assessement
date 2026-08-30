"""Date normalisation.

Voyager gives dates two different ways depending on which tier answered:

* **Structured** — the legacy REST tier returns
  ``{"startDate": {"month": 3, "year": 2021}, "endDate": {...}}``.
* **Free text** — the GraphQL card tier returns a rendered caption like
  ``"Mar 2021 - Present · 3 yrs 2 mos"``, already localised for display.

Both collapse to the same ``DateRange`` here so the section mappers never have
to care which tier produced their input.
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

_PRESENT = {"present", "current", "now", "today"}

#: "Mar 2021 - Present · 3 yrs 2 mos"  /  "2019 - 2023"  /  "Jan 2020"
#: The en and em dashes are deliberate: LinkedIn renders ranges with typographic
#: dashes, not ASCII hyphens, so matching only "-" silently drops every date.
_RANGE_SPLIT = re.compile("\\s*[-\u2013\u2014]\\s*")
_DURATION_SEP = re.compile(r"\s*[·|]\s*")
_MONTH_YEAR = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$")
_YEAR_ONLY = re.compile(r"^(\d{4})$")


class PartialDate(BaseModel):
    year: int | None = None
    month: int | None = None
    day: int | None = None

    def is_empty(self) -> bool:
        return self.year is None and self.month is None and self.day is None

    def to_iso(self) -> str | None:
        """Best-effort ISO string; unknown components are omitted."""
        if self.year is None:
            return None
        if self.month is None:
            return f"{self.year:04d}"
        if self.day is None:
            return f"{self.year:04d}-{self.month:02d}"
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"


class DateRange(BaseModel):
    start: PartialDate | None = None
    end: PartialDate | None = None
    is_current: bool = False
    #: LinkedIn's own rendered duration, kept verbatim when present.
    duration: str | None = None
    duration_months: int | None = Field(
        default=None, description="Computed span in months when both ends are known."
    )


def parse_partial_date(raw: object) -> PartialDate | None:
    """Read Voyager's structured ``{year, month, day}`` object."""
    if not isinstance(raw, dict):
        return None
    year = _as_int(raw.get("year"))
    month = _as_int(raw.get("month"))
    day = _as_int(raw.get("day"))
    if year is None and month is None and day is None:
        return None
    if month is not None and not 1 <= month <= 12:
        month = None
    return PartialDate(year=year, month=month, day=day)


def parse_structured_range(raw: object) -> DateRange | None:
    """Read a ``dateRange``/``timePeriod`` object from the REST tier."""
    if not isinstance(raw, dict):
        return None
    start = parse_partial_date(raw.get("start") or raw.get("startDate"))
    end = parse_partial_date(raw.get("end") or raw.get("endDate"))
    is_current = end is None or bool(raw.get("currentlyWorksHere"))
    return _finish(DateRange(start=start, end=end, is_current=is_current and end is None))


def parse_caption_range(caption: str | None) -> DateRange | None:
    """Read a rendered caption such as ``"Mar 2021 - Present · 3 yrs 2 mos"``."""
    if not caption or not caption.strip():
        return None

    text = caption.strip()
    duration: str | None = None

    # Split the trailing "· 3 yrs 2 mos" duration off, if present.
    parts = _DURATION_SEP.split(text)
    if len(parts) > 1:
        text = parts[0].strip()
        duration = parts[-1].strip() or None

    halves = _RANGE_SPLIT.split(text)
    start = _parse_token(halves[0]) if halves else None
    end: PartialDate | None = None
    is_current = False

    if len(halves) > 1:
        tail = halves[1].strip()
        if tail.lower() in _PRESENT:
            is_current = True
        else:
            end = _parse_token(tail)
    elif start is None:
        # Neither a range nor a recognisable date — this caption is something
        # else entirely (a location, a headline), so report no date at all.
        return None

    if start is None and end is None and not is_current:
        return None

    return _finish(
        DateRange(start=start, end=end, is_current=is_current, duration=duration)
    )


def _parse_token(token: str | None) -> PartialDate | None:
    if not token:
        return None
    token = token.strip().rstrip(",")
    if not token or token.lower() in _PRESENT:
        return None

    if m := _MONTH_YEAR.match(token):
        month = _MONTHS.get(m.group(1).lower().rstrip("."))
        return PartialDate(year=int(m.group(2)), month=month)
    if m := _YEAR_ONLY.match(token):
        return PartialDate(year=int(m.group(1)))
    return None


def _finish(dr: DateRange) -> DateRange:
    """Fill in the computed month span where both endpoints are known."""
    start, end = dr.start, dr.end
    if start and start.year:
        end_year, end_month = None, None
        if dr.is_current and not end:
            today = date.today()
            end_year, end_month = today.year, today.month
        elif end and end.year:
            end_year, end_month = end.year, end.month or 12
        if end_year is not None:
            months = (end_year - start.year) * 12 + ((end_month or 12) - (start.month or 1)) + 1
            if months > 0:
                dr.duration_months = months
    return dr


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
