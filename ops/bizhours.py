"""
Business-hours calendar for SLA deadlines: Mo–Fr 08:00–17:00 Europe/Berlin, excluding the statutory public holidays
of Hamburg (the operator's seat).

Hamburg holidays (FeiertagsG HH): Neujahr, Karfreitag, Ostermontag, 1. Mai, Christi Himmelfahrt, Pfingstmontag,
Tag der Deutschen Einheit (3.10.), Reformationstag (31.10., since 2018), 1. and 2. Weihnachtstag.
24.12. and 31.12. are NOT statutory holidays and count as normal working days.

Our reaction targets, counted in business hours (the exact reading is agreed with each customer):
    P1 8 h · P2 12 h · P3 24 h · routine 3 working days = 27 business hours (3 × 9 h)

    python -m ops.bizhours 2026-12-23T16:00 P1     # prints the deadline
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
START, END = time(8, 0), time(17, 0)
TARGET_HOURS = {"P1": 8, "P2": 12, "P3": 24, "routine": 27}


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


@lru_cache(maxsize=64)
def holidays_hamburg(year: int) -> dict[date, str]:
    e = easter(year)
    days = {
        date(year, 1, 1): "Neujahr",
        e - timedelta(days=2): "Karfreitag",
        e + timedelta(days=1): "Ostermontag",
        date(year, 5, 1): "Tag der Arbeit",
        e + timedelta(days=39): "Christi Himmelfahrt",
        e + timedelta(days=50): "Pfingstmontag",
        date(year, 10, 3): "Tag der Deutschen Einheit",
        date(year, 12, 25): "1. Weihnachtstag",
        date(year, 12, 26): "2. Weihnachtstag",
    }
    if year >= 2018:
        days[date(year, 10, 31)] = "Reformationstag"
    return days


def is_workday(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays_hamburg(d.year)


def _local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return dt.astimezone(TZ)


def is_business_time(dt: datetime) -> bool:
    t = _local(dt)
    return is_workday(t.date()) and START <= t.time() < END


def add_business_hours(start: datetime, hours: float) -> datetime:
    """Deadline = start + `hours` of support time. A request outside support time starts at the next opening."""
    t = _local(start)
    left = timedelta(hours=hours)
    while True:
        d = t.date()
        if is_workday(d):
            open_, close = datetime.combine(d, START, TZ), datetime.combine(d, END, TZ)
            if t < open_:
                t = open_
            if t < close:
                if t + left <= close:
                    return t + left
                left -= close - t
        t = datetime.combine(d + timedelta(days=1), START, TZ)


def business_hours_between(a: datetime, b: datetime) -> float:
    """Support hours elapsed between a and b (0 if b <= a)."""
    a, b = _local(a), _local(b)
    if b <= a:
        return 0.0
    total = timedelta()
    d = a.date()
    while d <= b.date():
        if is_workday(d):
            lo = max(a, datetime.combine(d, START, TZ))
            hi = min(b, datetime.combine(d, END, TZ))
            if hi > lo:
                total += hi - lo
        d += timedelta(days=1)
    return total.total_seconds() / 3600


def deadline(created: datetime, priority: str) -> datetime:
    return add_business_hours(created, TARGET_HOURS.get(priority, TARGET_HOURS["routine"]))


if __name__ == "__main__":
    if len(sys.argv) == 3:
        start = datetime.fromisoformat(sys.argv[1])
        start = start if start.tzinfo else start.replace(tzinfo=TZ)
        print(deadline(start, sys.argv[2]).isoformat())
    else:
        print(__doc__)
