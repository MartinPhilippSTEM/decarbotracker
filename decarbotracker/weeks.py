"""Práce s ISO týdny (YYYY-Www)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta

WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def parse_week(week: str) -> tuple[int, int]:
    m = WEEK_RE.match(week.strip().upper())
    if not m:
        raise ValueError(f"Neplatný týden '{week}', očekávám formát YYYY-Www, např. 2026-W39")
    year, num = int(m.group(1)), int(m.group(2))
    date.fromisocalendar(year, num, 1)  # validace (vyhodí ValueError)
    return year, num


def week_bounds(week: str) -> tuple[datetime, datetime]:
    """Začátek (pondělí 00:00 UTC) a konec (neděle 23:59:59.999999 UTC) ISO týdne."""
    year, num = parse_week(week)
    start = datetime.combine(date.fromisocalendar(year, num, 1), time.min, tzinfo=UTC)
    end = start + timedelta(days=7) - timedelta(microseconds=1)
    return start, end


def week_of(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def last_completed_week(now: datetime | None = None) -> str:
    """Týden, který právě skončil (v pondělí ráno = předchozí týden)."""
    now = now or datetime.now(UTC)
    return week_of(now - timedelta(days=7))


def week_slug(week: str) -> str:
    return week.lower()


def format_period_cs(start: datetime, end: datetime) -> str:
    if start.year == end.year:
        return f"{start.day}. {start.month}. – {end.day}. {end.month}. {end.year}"
    return f"{start.day}. {start.month}. {start.year} – {end.day}. {end.month}. {end.year}"
