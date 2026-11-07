"""Simulated-clock helpers. D0 00:00 == 2027-01-04T00:00:00+05:30 (docs/SIMULATOR.md §1)."""
from __future__ import annotations

import datetime as dt

D0 = dt.date(2027, 1, 4)
MIN_PER_DAY = 1440
_day_offset: dict[str, int] = {}


def ts_minute(ts: str) -> int:
    """'2027-01-06T14:07:00+05:30' -> minutes since D0 00:00 (negative during history)."""
    d = ts[:10]
    off = _day_offset.get(d)
    if off is None:
        off = (dt.date.fromisoformat(d) - D0).days * MIN_PER_DAY
        _day_offset[d] = off
    return off + int(ts[11:13]) * 60 + int(ts[14:16])


def fmt(minute: int | None) -> str:
    """minutes since D0 -> 'D3 14:00' (history days are negative, e.g. 'D-2 09:15')."""
    if minute is None:
        return "—"
    day, mod = divmod(int(minute), MIN_PER_DAY)
    return f"D{day} {mod // 60:02d}:{mod % 60:02d}"


def parse_when(s: str) -> int:
    """'D3 14:00' -> minute. Accepts an int-like string too."""
    s = s.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    day, hm = s.split()
    h, m = hm.split(":")
    return int(day.lstrip("Dd")) * MIN_PER_DAY + int(h) * 60 + int(m)


def day_of(minute: int) -> int:
    return minute // MIN_PER_DAY


def hour_of_day(minute: int) -> int:
    return (minute % MIN_PER_DAY) // 60
