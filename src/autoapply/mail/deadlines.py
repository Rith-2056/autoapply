"""Turn deadline phrases in emails into absolute timestamps."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
WEEKDAYS = {d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fourteen": 14, "twenty-four": 24, "forty-eight": 48, "seventy-two": 72}

_REL = re.compile(r"\b(?:within|in|over)\s+(?:the\s+next\s+)?(\d{1,3}|[a-z-]+)\s+(hours?|days?|business days?|weeks?)\b", re.I)
_BY_DATE = re.compile(
    r"\b(?:by|before|until|no later than|due(?: on)?|expires?(?: on)?|deadline(?: is|:)?|complete(?: it)? by|through)\s+"
    r"(?:the\s+)?(?:(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+)?"
    r"(?:(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?)"
    r"(?:,?\s+(\d{4}))?"
    r"(?:\s*(?:at|by)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*([A-Z]{2,4}T))?)?",
    re.I,
)
_NUMERIC = re.compile(r"\b(?:by|before|until|due|deadline(?: is|:)?|expires?(?: on)?)\s+(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", re.I)
_WEEKDAY = re.compile(r"\b(?:by|before|until|due|no later than)\s+(?:end of\s+(?:day\s+)?)?(?:this\s+|next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
_EOD = re.compile(r"\b(?:by|before)\s+(?:the\s+)?end of (?:the\s+)?(day|today|tonight|week|month)\b", re.I)
_TOMORROW = re.compile(r"\b(?:by|before|due)\s+tomorrow\b", re.I)


def _end_of_day(d: datetime) -> datetime:
    return d.replace(hour=23, minute=59, second=0, microsecond=0)


def parse_deadline(text: str, reference: datetime | None = None) -> datetime | None:
    """Return the deadline as a naive local datetime, or None if nothing is stated."""
    ref = reference or datetime.now()
    t = text or ""

    m = _REL.search(t)
    if m:
        n_raw, unit = m.group(1).lower(), m.group(2).lower()
        n = int(n_raw) if n_raw.isdigit() else NUM_WORDS.get(n_raw)
        if n:
            if unit.startswith("hour"):
                return ref + timedelta(hours=n)
            if unit.startswith("business"):
                d = ref
                added = 0
                while added < n:
                    d += timedelta(days=1)
                    if d.weekday() < 5:
                        added += 1
                return _end_of_day(d)
            if unit.startswith("day"):
                return _end_of_day(ref + timedelta(days=n))
            if unit.startswith("week"):
                return _end_of_day(ref + timedelta(weeks=n))

    m = _BY_DATE.search(t)
    if m:
        day = int(m.group(2) or m.group(5))
        mon = MONTHS.get((m.group(3) or m.group(4)).lower()[:3])
        year = int(m.group(6)) if m.group(6) else ref.year
        if mon:
            try:
                d = datetime(year, mon, day)
            except ValueError:
                d = None
            if d:
                if not m.group(6) and d < ref - timedelta(days=2):
                    d = d.replace(year=year + 1)
                if m.group(7):
                    hour = int(m.group(7)) % 12 + (12 if (m.group(9) or "").lower() == "pm" else 0)
                    return d.replace(hour=hour, minute=int(m.group(8) or 0))
                return _end_of_day(d)

    m = _NUMERIC.search(t)
    if m:
        mon, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else ref.year
        if year < 100:
            year += 2000
        try:
            d = datetime(year, mon, day)
            if not m.group(3) and d < ref - timedelta(days=2):
                d = d.replace(year=year + 1)
            return _end_of_day(d)
        except ValueError:
            pass

    m = _TOMORROW.search(t)
    if m:
        return _end_of_day(ref + timedelta(days=1))

    m = _WEEKDAY.search(t)
    if m:
        wd = WEEKDAYS[m.group(1).lower()]
        delta = (wd - ref.weekday()) % 7
        if delta == 0:
            delta = 7
        return _end_of_day(ref + timedelta(days=delta))

    m = _EOD.search(t)
    if m:
        what = m.group(1).lower()
        if what in ("day", "today", "tonight"):
            return _end_of_day(ref)
        if what == "week":
            return _end_of_day(ref + timedelta(days=(4 - ref.weekday()) % 7))
        if what == "month":
            nxt = (ref.replace(day=28) + timedelta(days=4)).replace(day=1)
            return _end_of_day(nxt - timedelta(days=1))
    return None


def humanize_deadline(deadline: str | datetime | None, now: datetime | None = None) -> str:
    if not deadline:
        return ""
    d = datetime.fromisoformat(deadline) if isinstance(deadline, str) else deadline
    now = now or datetime.now()
    delta = d - now
    hours = delta.total_seconds() / 3600
    if hours < 0:
        return "overdue"
    if hours < 1:
        return "due within the hour"
    if hours < 36:
        return f"due in {int(round(hours))} hours"
    return f"due in {int(delta.days)} days"
