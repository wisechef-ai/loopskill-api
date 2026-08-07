"""mesh_0408 W4 — derive an *expected firing interval* from a loop's schedule.

Convergence has to answer "has this loop been silent for longer than it should
be?" without a hard-coded constant. A ``*/3 * * * *`` beacon that has not fired
in an hour is badly overdue; a weekly loop assigned two days ago is perfectly
fine. Both facts fall out of the loop's OWN schedule, so that is what this
module computes: ``LoopManifest.schedule`` (a 5-field cron expression, a
``@daily``-style nickname, or a ``30m`` / ``every 2h`` shorthand) in, expected
seconds between firings out.

**No new dependency.** ``croniter`` is not in ``requirements.txt`` and this
does not need a full occurrence generator — it needs the *mean* gap between
firings, which is exact arithmetic over the expanded cron field sets.

The estimate is the ARITHMETIC MEAN gap: seconds-per-year divided by
firings-per-year. For every regular schedule (``*/N``, a fixed daily time, a
weekday-of-week) that mean equals the true gap exactly. For a deliberately
lumpy schedule (``0 9,10 * * *`` — two firings an hour apart, then a 23-hour
gap) the mean sits between the two real gaps, so a staleness deadline built on
it fires late rather than early. That bias is the safe direction: this module
would rather stay quiet one extra period than cry wolf on a healthy loop.

Anything this module cannot parse raises :class:`UnparseableSchedule`. Callers
MUST NOT swallow it into a healthy default — an unknown schedule is an unknown
state, not a green one (see ``ConvergenceState.UNKNOWN_SCHEDULE``).
"""

from __future__ import annotations

import re

__all__ = ["UnparseableSchedule", "parse_schedule_interval"]


class UnparseableSchedule(ValueError):
    """Raised when a schedule expression yields no derivable interval.

    Deliberately an exception rather than a ``None`` return: a caller that
    forgets to handle it crashes loudly instead of silently rendering an
    un-judgeable loop as healthy.
    """


# Mean Gregorian year — the same constant the cron-field arithmetic below
# divides by, so `*/3 * * * *` comes back as exactly 180.0.
_SECONDS_PER_YEAR = 365.2425 * 86400.0
_DAYS_PER_MONTH = 365.2425 / 12.0  # 30.436875

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip
_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

# Cron nicknames, expanded to the equivalent 5-field expression.
_NICKNAMES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_DURATION_UNITS = {
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
    "w": 604800.0, "week": 604800.0, "weeks": 604800.0,
}  # fmt: skip

# "30m", "every 2h", "every 30 minutes", "@every 90s"
_DURATION_RE = re.compile(
    r"^(?:@?every\s+)?(\d+(?:\.\d+)?)\s*([a-z]+)$",
    re.IGNORECASE,
)


def _expand_field(expr: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    """Expand one cron field into the set of values it matches.

    Supports ``*``, ``*/N``, ``a``, ``a-b``, ``a-b/N``, ``a/N`` and
    comma-separated lists of those, with optional three-letter names for the
    month and day-of-week fields.
    """
    values: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            raise UnparseableSchedule(f"empty element in cron field {expr!r}")

        step = 1
        if "/" in part:
            base, _, step_txt = part.partition("/")
            try:
                step = int(step_txt)
            except ValueError:
                raise UnparseableSchedule(f"non-integer step in {part!r}") from None
            if step < 1:
                raise UnparseableSchedule(f"step must be >= 1 in {part!r}")
        else:
            base = part

        base = base.strip()
        if base in ("*", ""):
            start, end = lo, hi
        elif "-" in base:
            start_txt, _, end_txt = base.partition("-")
            start = _atom(start_txt, names)
            end = _atom(end_txt, names)
        else:
            start = _atom(base, names)
            # `5/15` means "from 5, every 15, up to the field maximum" — the
            # same reading Vixie cron and systemd give it.
            end = hi if step > 1 else start

        if not (lo <= start <= hi) or not (lo <= end <= hi):
            raise UnparseableSchedule(f"value out of range [{lo}-{hi}] in {part!r}")
        if end < start:
            # Wrapping ranges (e.g. `fri-mon`) are not portable cron; refuse
            # rather than guess at a firing count.
            raise UnparseableSchedule(f"descending range in {part!r}")

        values.update(range(start, end + 1, step))

    if not values:
        raise UnparseableSchedule(f"cron field {expr!r} matches nothing")
    return values


def _atom(txt: str, names: dict[str, int] | None) -> int:
    txt = txt.strip()
    if names is not None:
        named = names.get(txt.lower()[:3])
        if named is not None:
            return named
    try:
        return int(txt)
    except ValueError:
        raise UnparseableSchedule(f"not a number or known name: {txt!r}") from None


def _cron_interval(expr: str) -> float:
    fields = expr.split()
    if len(fields) != 5:
        raise UnparseableSchedule(f"expected 5 cron fields, got {len(fields)}: {expr!r}")
    minute_f, hour_f, dom_f, month_f, dow_f = fields

    minutes = _expand_field(minute_f, 0, 59)
    hours = _expand_field(hour_f, 0, 23)
    doms = _expand_field(dom_f, 1, 31)
    months = _expand_field(month_f, 1, 12, _MONTH_NAMES)
    dows = _expand_field(dow_f, 0, 7, _DOW_NAMES)
    dows = {0 if d == 7 else d for d in dows}  # cron accepts both 0 and 7 for Sunday

    # A field is "unrestricted" when it selects every legal value — that is
    # what decides whether cron's day-of-month / day-of-week OR rule applies.
    dom_all = len(doms) == 31
    dow_all = len(dows) == 7

    dom_days = min(float(len(doms)), _DAYS_PER_MONTH)
    dow_days = len(dows) * _DAYS_PER_MONTH / 7.0

    if dom_all and dow_all:
        days_per_month = _DAYS_PER_MONTH
    elif dow_all:
        days_per_month = dom_days
    elif dom_all:
        days_per_month = dow_days
    else:
        # Vixie cron ORs a restricted dom with a restricted dow, so the
        # matching days are the UNION — bounded by the length of the month.
        days_per_month = min(_DAYS_PER_MONTH, dom_days + dow_days)

    firings_per_year = len(minutes) * len(hours) * days_per_month * len(months)
    if firings_per_year <= 0:
        raise UnparseableSchedule(f"schedule never fires: {expr!r}")
    return _SECONDS_PER_YEAR / firings_per_year


def parse_schedule_interval(schedule: str | None) -> float:
    """Expected seconds between firings of ``schedule``.

    Accepts a 5-field cron expression, a cron nickname (``@daily``), or a
    duration shorthand (``30m``, ``every 2h``, ``@every 90s``) — the three
    forms ``LoopManifest.schedule`` documents.

    Raises:
        UnparseableSchedule: for None, blank, or any expression whose firing
            rate cannot be derived. **Never returns a fallback.**
    """
    if schedule is None:
        raise UnparseableSchedule("schedule is not declared")
    text = schedule.strip()
    if not text:
        raise UnparseableSchedule("schedule is empty")

    nickname = _NICKNAMES.get(text.lower())
    if nickname is not None:
        return _cron_interval(nickname)

    m = _DURATION_RE.match(text)
    if m is not None:
        unit = _DURATION_UNITS.get(m.group(2).lower())
        if unit is None:
            raise UnparseableSchedule(f"unknown duration unit in {schedule!r}")
        seconds = float(m.group(1)) * unit
        if seconds <= 0:
            raise UnparseableSchedule(f"non-positive interval in {schedule!r}")
        return seconds

    return _cron_interval(text)
