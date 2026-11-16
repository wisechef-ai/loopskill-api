"""Shared CLI plumbing for the ``app/crons/`` jobs.

Two things every scheduled job here needs, and needs IDENTICALLY, because the
exit code is the only thing a cron monitor can read:

``positive_int``
    A CLI integer that refuses values below its minimum. ``--days 0`` /
    ``--threshold 0`` / ``--hours -5`` must not silently mean "everything" or
    "nothing": a zero-threshold clustering pass mints ``pending`` patch
    candidates out of single-report noise, and a zero-day retention window
    deletes a live idempotency table.

``Parser``
    An ``ArgumentParser`` whose *usage* errors exit ``3`` with a JSON line
    instead of argparse's default ``2``. These jobs own exit ``2`` for
    "REFUSED on a retention floor / invalid parameter" — a documented,
    grep-able state. Letting a typo'd flag (``--days abc``) also exit ``2``
    makes a typo indistinguishable from a deliberate unsafe
    request, and argparse prints a usage block to stderr instead of the JSON
    line every other path emits.

Exit-code contract shared by the cron jobs: ``0`` ran, ``1`` crashed, ``2``
refused (unsafe window / invalid parameter), ``3`` usage error, and ``4`` is
RESERVED for "another run still holds the lock" — no job may ever return it.

WHY 4 IS RESERVED — the ``-E 4`` in every ``CRONTAB_LINE``
    Measured on this fleet's util-linux 2.39.3: the obvious
    ``flock -n LOCK cmd >> LOG 2>&1`` exits **1 and writes zero bytes** when a
    live run holds the lock. ``flock`` is silent on ``EWOULDBLOCK``, so a stalled
    pass silences its own job with no trace at all — the same silent-failure class
    this card was opened to end. Adding a bare ``|| echo skipped`` would be worse,
    because the job's own crash (exit 1) would then also print "skipped": a second
    false attestation. So the line runs ONE ``flock -n -E 4`` and prints the marker
    only when ``$rc`` is exactly 4, then ``exit $rc``. One acquisition (a probe +
    run pair is a TOCTOU window that logs nothing when the probe wins and the real
    call loses), one reserved code, and 0/1/2/3 still mean what they always meant.
    Pinned behaviourally in ``tests/test_crons_wiring.py`` under a genuinely held
    lock, with a positive control proving the lock was held.
"""

from __future__ import annotations

import argparse
import json
import sys


class Parser(argparse.ArgumentParser):
    """``ArgumentParser`` that keeps exit 2 free for the floor guard."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(json.dumps({"error": f"usage error: {message}"}, sort_keys=True), flush=True)
        raise SystemExit(3)


def positive_int(name: str, value: int, *, minimum: int = 1) -> int:
    """Validate a parsed CLI integer.

    Returns the value when valid; raises ``SystemExit(2)`` with a JSON line on
    stdout when not, so the "JSON on stdout for every path" contract holds.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        print(
            json.dumps(
                {"error": f"--{name} must be an integer >= {minimum} (got {value!r})"},
                sort_keys=True,
            ),
            flush=True,
        )
        raise SystemExit(2)
    return value
