import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ZONES = {
    "us eastern": "America/New_York",
    "eastern time": "America/New_York",
    "et": "America/New_York",
    "est": "UTC-05:00",
    "edt": "UTC-04:00",
    "us pacific": "America/Los_Angeles",
    "pacific time": "America/Los_Angeles",
    "pt": "America/Los_Angeles",
    "pst": "UTC-08:00",
    "pdt": "UTC-07:00",
    "cet": "UTC+01:00",
    "cest": "UTC+02:00",
    "gmt": "UTC",
    "utc": "UTC",
}
ZONE = re.compile(
    r"\b(?:UTC[+-](?:0?\d|1[0-4])(?::[0-5]\d)?|[A-Z][a-z_]+/[A-Z][\w/-]+|"
    + "|".join(ZONES)
    + r")\b",
    re.I,
)
STANDALONE_ZONE = re.compile(
    r"\b(?:CET|CEST|UTC|GMT|EST|EDT|PST|PDT|US Eastern|US Pacific|Eastern Time|Pacific Time)\b"
    r"|[A-Z][a-z_]+/[A-Z][\w/-]+"
)
RANGE = re.compile(
    r"\b(\d{1,2}(?::[0-5]\d)?)(?:\s*(am|pm))?\s*(?:[–—-]|to)\s*"
    r"(\d{1,2}(?::[0-5]\d)?)(?:\s*(am|pm))?\b",
    re.I,
)
CONTEXT = re.compile(
    r"working hours?|work hours?|time zones?|timezone|overlap|core hours|business hours|"
    r"\b(?:work|available)\s+(?:from|between|on|during)\b|\d\s*(?:am|pm)\b|"
    r"рабоч\w* час|часов\w* пояс|пересечен",
    re.I,
)
REQUIRED = re.compile(r"must|required|mandatory|core hours|обязательно|необходимо", re.I)
OVERLAP = re.compile(r"(?:at least|minimum(?: of)?|не менее)\s+(\d{1,2})\s*(?:hours?|час)", re.I)


@dataclass(frozen=True)
class WorkTime:
    evidence: str
    zone: str | None
    start: str | None
    end: str | None
    mandatory: bool
    minimum_overlap: int | None = None


def timezone_for(name):
    if name.startswith(("UTC+", "UTC-")):
        hours, _, minutes = name[4:].partition(":")
        offset = timedelta(hours=int(hours), minutes=int(minutes or 0))
        return timezone(offset if name[3] == "+" else -offset)
    return ZoneInfo(name)


def iso_clock(value, suffix):
    hour, _, minute = value.partition(":")
    number = int(hour)
    if suffix:
        if not 1 <= number <= 12:
            return None
        number = number % 12 + (12 if suffix.casefold() == "pm" else 0)
    return f"{number:02d}:{minute or '00'}" if number < 24 else None


def work_times(description):
    for line in re.split(r"\n|(?<=[.!?])\s+", description):
        window, match = RANGE.search(line), ZONE.search(line)
        if not (CONTEXT.search(line) or STANDALONE_ZONE.search(line) or window and match):
            continue
        start = end = zone = None
        if window:
            left, ampm, right, end_ampm = window.groups()
            start, end = iso_clock(left, ampm or end_ampm), iso_clock(right, end_ampm)
            if start and end and start > end and not ampm and end_ampm:
                start = iso_clock(left, "am" if end_ampm.casefold() == "pm" else "pm")
        if match:
            value = match.group()
            zone = ZONES.get(
                value.casefold(), value.upper() if value.casefold().startswith("utc") else value
            )
            try:
                timezone_for(zone)
            except (ValueError, ZoneInfoNotFoundError):
                zone = None
        if window or match or re.search(r"\d\s*(?:am|pm)\b|working hours?|core hours", line, re.I):
            overlap = OVERLAP.search(line)
            yield WorkTime(
                line.strip()[:500],
                zone,
                start,
                end,
                bool(REQUIRED.search(line) or overlap),
                int(overlap[1]) if overlap else None,
            )


def work_time(description):
    return max(
        work_times(description),
        key=lambda item: (
            item.mandatory,
            bool(item.zone and item.start and item.end),
            bool(item.zone),
        ),
        default=None,
    )


def local_window(day, start, end, zone):
    begins = datetime.combine(day, time.fromisoformat(start), timezone_for(zone))
    ends = datetime.combine(day, time.fromisoformat(end), timezone_for(zone))
    if ends <= begins:
        ends += timedelta(days=1)
    return begins.astimezone(UTC), ends.astimezone(UTC)


def time_conflicts(requirement, availability, day):
    if not (
        requirement
        and availability
        and requirement.mandatory
        and requirement.zone
        and requirement.start
        and requirement.end
    ):
        return False
    job_start, job_end = local_window(day, requirement.start, requirement.end, requirement.zone)
    expected = (
        timedelta(hours=requirement.minimum_overlap)
        if requirement.minimum_overlap
        else job_end - job_start
    )
    for shift in (-1, 0, 1):
        available_start, available_end = local_window(
            day + timedelta(days=shift),
            availability["start"],
            availability["end"],
            availability["zone"],
        )
        if min(job_end, available_end) - max(job_start, available_start) >= expected:
            return False
    return True


def display_work_time(description, availability, day: date):
    requirement = work_time(description)
    if requirement is None:
        return None
    result = {
        **asdict(requirement),
        "needs_review": not (requirement.zone and requirement.start and requirement.end),
    }
    if availability and not result["needs_review"]:
        start, end = local_window(day, requirement.start, requirement.end, requirement.zone)
        zone = timezone_for(availability["zone"])
        result.update(
            local_start=start.astimezone(zone).isoformat(),
            local_end=end.astimezone(zone).isoformat(),
        )
    return result
