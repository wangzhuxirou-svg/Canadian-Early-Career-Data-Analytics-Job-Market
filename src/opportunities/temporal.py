"""Evidence-based local clock parsing; never guess a time or a DST offset."""

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

RANGE = re.compile(r"(?<![\d:])(?P<a>\d{1,2}(?::\d{2})?)\s*(?P<ap>[ap]\.?m\.?)?\s*(?:[-–—]|to)\s*(?P<b>\d{1,2}(?::\d{2})?)\s*(?P<bp>[ap]\.?m\.?)?(?!\w)", re.I)
SINGLE = re.compile(r"(?<![\d:])(?P<a>\d{1,2}(?::\d{2})?)\s*(?P<ap>[ap]\.?m\.?)(?!\w)|(?:at|@|time:)\s*(?P<clock>\d{1,2}:\d{2})\s*(?P<cp>[ap]\.?m\.?)?(?!\d)", re.I)
NOON = re.compile(r"\b(?:(?:at|by)\s+|time:\s*)noon\b", re.I)
ZONE_TOKEN = re.compile(
    r"\b(?:UTC|GMT)(?:\s*[+-]\d{1,2}(?::?\d{2})?)?(?!\w)|"
    r"\b(?:[ECMP][SD]?T|[A-Z]{2,5}[SD]T|CET|CEST|BST|IST|HST|AKST|AKDT|"
    r"JST|KST|SGT|WET|WEST|EET|EEST|AST|ADT|NST|NDT|Eastern|Central|Mountain|Pacific)\b|"
    r"\b[A-Z_]+/[A-Z_]+\b", re.I
)


def clock_zone(text, zone="America/Toronto"):
    matches = {match.group().casefold() for match in ZONE_TOKEN.finditer(text)}
    zones = {"utc": timezone.utc, "gmt": timezone.utc, "et": ZoneInfo("America/Toronto"),
             "eastern": ZoneInfo("America/Toronto"),
             "est": timezone(timedelta(hours=-5)), "edt": timezone(timedelta(hours=-4))}
    if matches - zones.keys() or len({zones[key] for key in matches}) > 1:
        raise ValueError("Explicit timezone needs confirmation")
    return zones[next(iter(matches))] if matches else ZoneInfo(zone)


def _clock(value, meridiem=None):
    hour, _, minute = value.partition(":")
    hour, minute = int(hour), int(minute or 0)
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError("Invalid 12-hour clock")
        hour = hour % 12 + (12 if meridiem[0].lower() == "p" else 0)
    return time(hour, minute)


def _utc(day, clock, zone):
    local = datetime.combine(datetime.fromisoformat(day).date(), clock, zone)
    # Ambiguous/nonexistent wall times need an operator, not a guessed offset.
    if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
        raise ValueError("Ambiguous or nonexistent local clock time")
    utc = local.astimezone(timezone.utc)
    if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValueError("Nonexistent local clock time")
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def clock_range(line: str, day: str, zone="America/Toronto"):
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
        source_zone = clock_zone(line, zone)
        start, end = _utc(day, _clock(a, ap), source_zone), _utc(day, _clock(b, bp), source_zone)
        if end <= start:
            raise ValueError("End clock must follow start; overnight ranges need confirmation")
        return start, end
    return None


def deadline_clock(line: str, day: str, zone="America/Toronto"):
    if NOON.search(line):
        return _utc(day, time(12), clock_zone(line, zone))
    match = SINGLE.search(line)
    if not match:
        return None
    clock = _clock(match.group("clock") or match.group("a"), match.group("ap") or match.group("cp"))
    return _utc(day, clock, clock_zone(line, zone))


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


def expiration_reason(opportunity: dict, now: datetime | None = None) -> str | None:
    """Shared D6/D7 expiry policy; date-only values last through Toronto midnight."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo("America/Toronto")).date().isoformat()
    for key in ("deadline", "end", "start"):
        if key == "start" and (opportunity.get("end_date") or opportunity.get("end_at")):
            continue
        exact = opportunity.get(f"{key}_at")
        day = opportunity.get(f"{key}_date")
        if exact:
            instant = datetime.fromisoformat(exact.replace("Z", "+00:00"))
            if key != "start":
                if instant <= now:
                    return f"expired_{key}"
                continue
            day = instant.astimezone(ZoneInfo("America/Toronto")).date().isoformat()
        if day and day < today:
            return f"expired_{key}"
    return None
