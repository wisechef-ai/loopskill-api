"""mesh_0408 W4 — schedule → expected-firing-interval derivation.

The convergence gate's whole claim is that its staleness deadline comes from
the loop's OWN schedule rather than a constant. That claim is only as good as
this parser, so these tests pin the exact intervals the deadline arithmetic
depends on, and — just as importantly — pin that garbage RAISES instead of
returning a plausible-looking number. A parser that quietly returned, say,
3600.0 for an expression it did not understand would hand every un-judgeable
loop a green verdict, which is precisely the defect W4 removes.
"""

from __future__ import annotations

import pytest

from app.services.schedule_interval import UnparseableSchedule, parse_schedule_interval

HOUR = 3600.0
DAY = 86400.0
WEEK = 7 * DAY


class TestCronExpressions:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("* * * * *", 60.0),  # every minute
            ("*/3 * * * *", 180.0),  # THE beacon — the number the gate turns on
            ("*/5 * * * *", 300.0),
            ("*/15 * * * *", 900.0),
            ("0 * * * *", HOUR),  # hourly
            ("30 * * * *", HOUR),  # hourly, offset — same RATE
            ("0 */6 * * *", 6 * HOUR),
            ("0 9 * * *", DAY),  # daily at 09:00
            ("17 3 * * *", DAY),
            ("0 9 * * 1", WEEK),  # weekly, Mondays
            ("0 9 * * mon", WEEK),  # names accepted
            ("0 9 * * MON", WEEK),
            ("0 9 * * 1,4", WEEK / 2),  # twice a week
            ("0 9 * * 1-5", WEEK / 5),  # weekdays
            ("0 0 1 * *", DAY * 365.2425 / 12),  # monthly
            ("0 0 1 1 *", DAY * 365.2425),  # yearly
            ("0 0 1 jan *", DAY * 365.2425),
        ],
    )
    def test_interval(self, expr, expected):
        assert parse_schedule_interval(expr) == pytest.approx(expected, rel=1e-9)

    def test_sunday_is_accepted_as_both_0_and_7(self):
        """Vixie cron allows either; treating 7 as an eighth day would make a
        weekly loop look like it fires 8/7 as often and shorten its deadline."""
        assert parse_schedule_interval("0 9 * * 0") == pytest.approx(WEEK)
        assert parse_schedule_interval("0 9 * * 7") == pytest.approx(WEEK)
        assert parse_schedule_interval("0 9 * * 0,7") == pytest.approx(WEEK)

    def test_star_dow_is_seven_days_not_eight(self):
        assert parse_schedule_interval("0 9 * * *") == pytest.approx(DAY)
        assert parse_schedule_interval("0 9 * * 0-6") == pytest.approx(DAY)

    def test_restricted_dom_and_dow_are_ORed_not_ANDed(self):
        """Cron's documented quirk: with BOTH restricted, a day matching
        EITHER fires. ANDing them would badly over-estimate the interval and
        make a frequent loop un-alarmable."""
        both = parse_schedule_interval("0 9 1 * 1")  # 1st of month OR Mondays
        dow_only = parse_schedule_interval("0 9 * * 1")  # Mondays alone
        assert both < dow_only  # the union fires MORE often, so the gap is smaller


class TestShorthands:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("@hourly", HOUR),
            ("@daily", DAY),
            ("@midnight", DAY),
            ("@weekly", WEEK),
            ("@monthly", DAY * 365.2425 / 12),
            ("@yearly", DAY * 365.2425),
            ("@annually", DAY * 365.2425),
            ("30m", 1800.0),
            ("90s", 90.0),
            ("2h", 2 * HOUR),
            ("1d", DAY),
            ("every 2h", 2 * HOUR),
            ("every 30 minutes", 1800.0),
            ("EVERY 15 MIN", 900.0),
            ("@every 45s", 45.0),
        ],
    )
    def test_interval(self, expr, expected):
        assert parse_schedule_interval(expr) == pytest.approx(expected, rel=1e-9)


class TestUnparseableRaisesRatherThanGuessing:
    """Every case here MUST raise. A returned float — any float — would be a
    silent green for a loop nobody can actually judge."""

    @pytest.mark.parametrize(
        "expr",
        [
            None,
            "",
            "   ",
            "every other thursday-ish",
            "soon",
            "* * * *",  # 4 fields
            "* * * * * *",  # 6 fields (seconds-resolution cron — not supported)
            "60 * * * *",  # minute out of range
            "* 25 * * *",  # hour out of range
            "0 0 32 * *",  # day-of-month out of range
            "0 0 * 13 *",  # month out of range
            "0 0 * * 8",  # day-of-week out of range
            "*/0 * * * *",  # zero step
            "*/-1 * * * *",  # negative step
            "*/x * * * *",  # non-numeric step
            "5-1 * * * *",  # descending range
            "0,, * * * *",  # empty list element
            "0m",  # zero-length interval
            "5 parsecs",  # unknown unit
        ],
    )
    def test_raises(self, expr):
        with pytest.raises(UnparseableSchedule):
            parse_schedule_interval(expr)

    def test_the_exception_is_a_valueerror_subclass(self):
        """So a caller that forgets the specific type still can't mistake it
        for a successful parse."""
        assert issubclass(UnparseableSchedule, ValueError)
