"""Date and time tool. No network, no model: the answer comes from the system clock.

Tag: [TIME: now | in <place> | days until <date or holiday> | days since <date>]
Answers "what time is it", "what's the date", "what day is it", "time in Tokyo",
"how many days until Christmas / 2026-12-25 / March 3". Standard library only.
"""
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CITY_ZONES = {
    "utc": "UTC", "gmt": "UTC", "new york": "America/New_York", "toronto": "America/Toronto",
    "montreal": "America/Toronto", "ottawa": "America/Toronto", "vancouver": "America/Vancouver",
    "chicago": "America/Chicago", "denver": "America/Denver", "los angeles": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "mexico city": "America/Mexico_City",
    "sao paulo": "America/Sao_Paulo", "london": "Europe/London", "dublin": "Europe/Dublin",
    "paris": "Europe/Paris", "berlin": "Europe/Berlin", "madrid": "Europe/Madrid",
    "rome": "Europe/Rome", "amsterdam": "Europe/Amsterdam", "zurich": "Europe/Zurich",
    "stockholm": "Europe/Stockholm", "athens": "Europe/Athens", "moscow": "Europe/Moscow",
    "istanbul": "Europe/Istanbul", "dubai": "Asia/Dubai", "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata", "singapore": "Asia/Singapore", "hong kong": "Asia/Hong_Kong",
    "shanghai": "Asia/Shanghai", "beijing": "Asia/Shanghai", "tokyo": "Asia/Tokyo",
    "seoul": "Asia/Seoul", "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "auckland": "Pacific/Auckland", "johannesburg": "Africa/Johannesburg", "cairo": "Africa/Cairo",
}
HOLIDAYS = {"christmas": (12, 25), "christmas eve": (12, 24), "new year": (1, 1),
            "new years": (1, 1), "new year's": (1, 1), "new year's eve": (12, 31),
            "halloween": (10, 31), "valentine's day": (2, 14), "valentines day": (2, 14),
            "canada day": (7, 1), "independence day": (7, 4), "bastille day": (7, 14)}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))


def _fmt_time(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def _fmt_day(d):
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B %Y')}"


def _find_zone(text):
    low = text.lower()
    for name in sorted(CITY_ZONES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            return name.title(), ZoneInfo(CITY_ZONES[name])
    m = re.search(r"\b([A-Za-z]+/[A-Za-z_]+)\b", text)
    if m:
        try:
            return m.group(1), ZoneInfo(m.group(1))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return None


def _find_date(text, today):
    """Return (label, date) for an explicit date or a named holiday, else None."""
    low = text.lower()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return _fmt_day(d), d
        except ValueError:
            return None
    for pat in (rf"\b({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b",
                rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH_RE})\.?(?:,?\s+(\d{{4}}))?\b"):
        m = re.search(pat, low)
        if m:
            a, b, y = m.groups()
            month, day = (MONTHS[a], int(b)) if a.isalpha() else (MONTHS[b], int(a))
            return _resolve(month, day, int(y) if y else None, today)
    for name in sorted(HOLIDAYS, key=len, reverse=True):
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            month, day = HOLIDAYS[name]
            label, d = _resolve(month, day, None, today)
            return name.title(), d
    return None


def _resolve(month, day, year, today):
    try:
        d = date(year or today.year, month, day)
        if year is None and d < today:               # no year given: the next occurrence
            d = date(today.year + 1, month, day)
    except ValueError:
        return None
    return _fmt_day(d), d


def run(arg, ctx):
    """[TIME: ...] -> (spoken answer, {"query": arg}). Direct: no model pass needed."""
    text = (arg or "now").strip()
    now = datetime.now().astimezone()
    today = now.date()
    parts = []

    zone = _find_zone(text)
    if zone and not re.search(r"\b(?:days?|until|till|since)\b", text, re.I):
        label, tz = zone
        there = now.astimezone(tz)
        parts.append(f"In {label} it is {_fmt_time(there)} on {_fmt_day(there.date())} ({there.tzname()})")
        parts.append(f"Here it is {_fmt_time(now)} ({now.tzname()})")
    else:
        hit = _find_date(text, today) if re.search(r"\b(?:until|till|since|to|from|days?|away|left)\b", text, re.I) else None
        if hit:
            label, d = hit
            if label != _fmt_day(d):
                label = f"{label} ({_fmt_day(d)})"
            delta = (d - today).days
            if delta == 0:
                parts.append(f"{label} is today")
            elif delta > 0:
                parts.append(f"{label} is {delta} day{'s' if delta != 1 else ''} away")
            else:
                parts.append(f"{label} was {-delta} day{'s' if delta != -1 else ''} ago")
        else:
            parts.append(f"It is {_fmt_time(now)} on {_fmt_day(today)} ({now.tzname()})")
    return ", sir. ".join(parts[:2]) + ", sir." if len(parts) > 1 else parts[0] + ", sir.", {"query": text}


def now_line():
    """One line for the model's prompt, so every answer knows the date AND time."""
    now = datetime.now().astimezone()
    return f"{_fmt_day(now.date())}, {_fmt_time(now)} {now.tzname()}"


TAGS = {"TIME": {
    "direct": True,                      # answered from the clock; no second model pass
    "default_arg": "now",
    "route": re.compile(
        r"^(?=.*\b(?:what(?:'s|\s+is)?\s+(?:the\s+)?(?:current\s+)?(?:time|date)(?!\s+of\b)"
        r"|what\s+time\s+is\s+it|what\s+day\s+is\s+(?:it|today)|what(?:'s|\s+is)\s+today"
        r"|today'?s\s+date|current\s+(?:time|date)|how\s+many\s+days|days\s+(?:until|till|since|left|to)"
        r"|how\s+long\s+(?:until|till|since))\b)(?P<arg>.*)$", re.I | re.S),
    "intent": re.compile(r"\b(time|date|day|today|clock|days|until|till)\b", re.I),
    "label": "TIME",
    "run": run,
}}
