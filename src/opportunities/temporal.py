"""Evidence-based local clock parsing; never guess a time or a DST offset."""

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

RANGE = re.compile(r"(?<![\d:])(?P<a>\d{1,2}(?::\d{2})?)\s*(?P<ap>[ap]\.?m\.?)?\s*(?:[-–—]|to)\s*(?P<b>\d{1,2}(?::\d{2})?)\s*(?P<bp>[ap]\.?m\.?)?(?!\w)", re.I)
SINGLE = re.compile(r"(?<![\d:])(?P<a>\d{1,2}(?::\d{2})?)\s*(?P<ap>[ap]\.?m\.?)(?!\w)|(?:at|@|time:)\s*(?P<clock>\d{1,2}:\d{2})\s*(?P<cp>[ap]\.?m\.?)?(?!\d)", re.I)


def _clock(value, meridiem=None):
    hour, _, minute = value.partition(":")
    hour, minute = int(hour), int(minute or 0)
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError("Invalid 12-hour clock")
        hour = hour % 12 + (12 if meridiem[0].lower() == "p" else 0)
    return time(hour, minute)


def _utc(day, clock, zone):
    local = datetime.combine(datetime.fromisoformat(day).date(), clock, ZoneInfo(zone))
    # Ambiguous/nonexistent wall times need an operator, not a guessed offset.
    if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
        raise ValueError("Ambiguous or nonexistent local clock time")
    utc = local.astimezone(timezone.utc)
    if utc.astimezone(ZoneInfo(zone)).replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValueError("Nonexistent local clock time")
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def clock_range(line: str, day: str, zone="America/Toronto"):
    if re.search(r"\b(?:UTC|GMT|PST|PDT|CST|CDT|MST|MDT|CET|CEST|BST)\b", line):
        raise ValueError("Explicit non-Toronto timezone needs confirmation")
    for match in RANGE.finditer(line):
        a, b, ap, bp = (match.group(key) for key in ("a", "b", "ap", "bp"))
        if not ap and not bp and not (":" in a and ":" in b):
            continue
        if not ap and bp:
            ap = bp
            if _clock(a, ap) >= _clock(b, bp) and bp.lower().startswith("p"):
                ap = "am"
        if ap and not bp:
            bp = ap
        start, end = _utc(day, _clock(a, ap), zone), _utc(day, _clock(b, bp), zone)
        if end <= start:
            raise ValueError("End clock must follow start; overnight ranges need confirmation")
        return start, end
    return None


def deadline_clock(line: str, day: str, zone="America/Toronto"):
    if re.search(r"\b(?:at|by) noon\b", line, re.I):
        return _utc(day, time(12), zone)
    match = SINGLE.search(line)
    if not match:
        return None
    return _utc(day, _clock(match.group("clock") or match.group("a"), match.group("ap") or match.group("cp")), zone)


def relative_deadline(text: str, posted_at: str | None):
    if not posted_at:
        return None
    anchor = datetime.fromisoformat(posted_at.replace("Z", "+00:00")).astimezone(ZoneInfo("America/Toronto")).date()
    if re.search(r"\btomorrow\b", text, re.I):
        return (anchor + timedelta(days=1)).isoformat()
    if re.search(r"\b(?:today|tonight)\b", text, re.I):
        return anchor.isoformat()
    match = re.search(r"\bnext (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text, re.I)
    if match:
        weekday = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday").index(match.group(1).lower())
        delta = (weekday - anchor.weekday()) % 7 or 7
        return (anchor + timedelta(days=delta)).isoformat()
    match = re.search(r"\b(\d+) days? left\b", text, re.I)
    if match:
        return (anchor + timedelta(days=int(match.group(1)))).isoformat()
    return None
