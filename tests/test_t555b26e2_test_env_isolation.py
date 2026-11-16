"""t_555b26e2 — the suite must not inherit ambient infrastructure state.

Two defects, one class: the test run silently changed behaviour depending on
what happened to be running on the host, so the ``random-order`` job's red/green
was a coin flip that could not be replayed from its own published seed.

1. ``WR_REDIS_URL`` defaults to ``redis://localhost:6379/0``. ``RateLimitMiddleware``
   keys its sliding window by client IP, and every ``TestClient`` presents
   127.0.0.1 — so on any box with Redis up, the entire ~8,350-test suite shares
   ONE 60-req/min bucket. Which tests fall past request #60 inside a 60s window is
   a function of run order and wall clock. Measured A/B on wisechef-agents at CI
   seed 3716405558 (serial): Redis reachable -> 30 failures (9 known host-sandbox
   + 21 rate-limit casualties); Redis unreachable -> exactly the 9. CI has no
   Redis, hence the unreproducible divergence. ``conftest.py`` now pins the URL
   blank; these tests fail if that ever regresses.

2. ``app/middleware/api_key.py`` carried a second, drifted copy of ``get_redis()``
   that ``app/last_used_tracker.py`` imports and calls at MODULE IMPORT time. It
   lacked the blank-URL guard its twin in ``app/middleware/__init__.py`` has, so
   the documented zero-config self-host shape (``docker-compose.yml`` ships
   ``WR_REDIS_URL: ""``) raised ``ValueError: Redis URL must specify one of the
   following schemes`` at import. The same blank URL the suite now pins.

Tests 1-3 are behavioural; 4-5 are structural, because a forgotten guard is
exactly the kind of thing a route-level test cannot catch.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.config import settings

REPO_ROOT = Path(__file__).resolve().parents[1]
# Above the middleware's 60/min window: if two app instances ever share a bucket,
# the tail of this loop is a 429 instead of a 200.
_REQUESTS_PER_APP = 40
_PROBE_PATH = "/api/marketing/claims"


def test_settings_redis_url_is_blank_under_pytest():
    """The suite pins WR_REDIS_URL blank; a host Redis must not be reachable."""
    assert not (settings.REDIS_URL or "").strip(), (
        "conftest.py must keep pinning WR_REDIS_URL blank. A live REDIS_URL makes "
        "RateLimitMiddleware share one per-IP bucket across the whole process, so "
        "test outcomes depend on run order and how fast the suite runs (t_555b26e2)."
    )


def test_both_get_redis_entry_points_report_no_redis():
    """There is one Redis client builder and it degrades to None here."""
    from app.middleware import get_redis as package_get_redis
    from app.middleware.api_key import get_redis as api_key_get_redis

    assert package_get_redis() is None
    assert api_key_get_redis() is None, (
        "app.middleware.api_key.get_redis must delegate to app.middleware.get_redis; "
        "a private copy there is what crashed the blank-URL zero-config path."
    )


def test_rate_limit_buckets_are_per_app_instance_not_process_global():
    """80 anonymous requests from one IP, split across two apps, must all pass.

    The middleware allows 60/min per bucket. Two independent app instances means
    two buckets, so 40+40 is legal. The moment the bucket becomes process-global
    (ambient Redis), the 61st request onward is a 429 — which is precisely the
    failure that made the random-order job a lottery on Redis-bearing hosts.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    for _ in range(2):
        client = TestClient(create_app())
        for i in range(_REQUESTS_PER_APP):
            resp = client.get(_PROBE_PATH)
            assert resp.status_code != 429, (
                f"rate-limited on anonymous request {i + 1} against {_PROBE_PATH}; "
                "the limiter's bucket is being shared beyond one app instance "
                "(t_555b26e2 — ambient Redis?)."
            )


def test_api_key_module_has_no_private_redis_builder():
    """Structural backstop: the drifted duplicate must not come back.

    A behavioural test can be re-broken by re-adding the copy; this one cannot.

    Parsed with AST, not string matching: a naive ``"redis.from_url(" not in
    src`` is satisfied by prose that merely *mentions* the call — and the
    docstring explaining why the copy was deleted does exactly that, so a
    substring check fails on the fix it is supposed to protect.
    """
    import ast

    src = (REPO_ROOT / "app" / "middleware" / "api_key.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    builds_client = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "from_url"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "redis"
    ]
    assert not builds_client, (
        f"app/middleware/api_key.py builds its own Redis client at line(s) "
        f"{[n.lineno for n in builds_client]} — delegate to "
        "app.middleware.get_redis (t_555b26e2: the private copy missed the "
        "blank-URL guard and raised ValueError at import time on the "
        "docker-compose.yml zero-config shape)."
    )
    imports_redis = [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Import) and any(a.name == "redis" for a in node.names))
        or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "redis")
    ]
    assert not imports_redis, (
        "app/middleware/api_key.py should not depend on the redis package directly; "
        "the single client builder lives in app/middleware/__init__.py"
    )


@pytest.mark.parametrize("redis_url", ["", "   "])
def test_last_used_tracker_imports_under_blank_redis_url(redis_url: str):
    """RED proof, in a subprocess: the docker-compose.yml zero-config shape.

    ``docker-compose.yml`` ships ``WR_REDIS_URL: ""`` and
    ``app/last_used_tracker.py`` calls ``get_redis()`` at module import time.
    Before the fix this raised ValueError and took down every request path that
    touches the last-used tracker.
    """
    code = "import app.last_used_tracker as m\nassert m.tracker is not None\nprint('ok')\n"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **{k: v for k, v in __import__("os").environ.items() if k.startswith("PATH")},
            "WR_DATABASE_URL": "sqlite:///:memory:",
            "DATABASE_URL": "sqlite:///:memory:",
            "WR_REDIS_URL": redis_url,
        },
    )
    assert proc.returncode == 0, (
        f"import app.last_used_tracker failed with WR_REDIS_URL={redis_url!r}:\n{proc.stderr[-2000:]}"
    )
    assert "ValueError" not in proc.stderr
