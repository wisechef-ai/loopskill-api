"""Guard: tests must not write ``WR_*`` env vars straight into ``os.environ``.

``app.config.Settings`` reads every ``WR_*`` var, and the root conftest pins
``WR_DATABASE_URL=sqlite`` + ``WR_COOKIES_SECURE=false`` so the issue-#11
production gate stays quiet. A test that does ``os.environ["WR_..."] = x``
never undoes it, so the value outlives the test and lands in whatever builds
the Settings singleton next on that xdist worker.

That is how ci-proof run 37240071513 (attempt 1) failed 5 unrelated tests on
gw0: tests/migrations/test_issue282_fed_hub_trgm.py set
``WR_DATABASE_URL=<postgres DSN>`` permanently, a later reload of app.config
emptied the Settings cache, and the next Settings() saw postgres +
COOKIES_SECURE=false and raised. The order depended on --dist loadfile
scheduling, so the rerun passed.

Use ``monkeypatch.setenv`` (undone at teardown) or pass an explicit ``env=``
dict to a subprocess instead.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# os.environ["WR_X"] = ...   /   os.environ['WR_X'] = ...   (not ==)
RAW_WRITE = re.compile(r"""os\.environ\[\s*["']WR_[A-Z0-9_]+["']\s*\]\s*=(?!=)""")
# os.environ.update({... "WR_X" ...}). setdefault is deliberately NOT matched:
# conftest.py uses it at import time for suite-wide defaults, which is the
# intended baseline every test should see, not a per-test leak.
RAW_UPDATE = re.compile(r"""os\.environ\.update\([^)]*["']WR_""")


def _offenders() -> list[str]:
    hits: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path == Path(__file__).resolve():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if RAW_WRITE.search(line) or RAW_UPDATE.search(line):
                hits.append(f"{path.relative_to(TESTS_DIR.parent)}:{lineno}: {line.strip()}")
    return hits


def test_no_test_writes_wr_env_vars_directly() -> None:
    """Every in-process WR_* env change in tests/ must be scoped."""
    offenders = _offenders()
    assert not offenders, (
        "Raw WR_* writes to os.environ leak into later tests on the same worker; "
        "use monkeypatch.setenv instead:\n" + "\n".join(offenders)
    )


def test_guard_pattern_catches_the_original_leak() -> None:
    """Pin the regex against the exact line that caused the flake."""
    assert RAW_WRITE.search('        os.environ["WR_DATABASE_URL"] = url')
    assert RAW_UPDATE.search('os.environ.update({"WR_COOKIES_SECURE": "false"})')
    assert not RAW_WRITE.search('assert os.environ["WR_DATABASE_URL"] == url')
    assert not RAW_WRITE.search('monkeypatch.setenv("WR_DATABASE_URL", url)')
