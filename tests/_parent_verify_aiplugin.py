"""Parent-side independent verification of the ai-plugin.json manifest.

Not a re-run of the child's tests — a check of the one claim I care about:
the manifest must not advertise a URL that 404s (the "documented but broken"
defect class this gap-closure exists to kill), and it must serve with no
credential at all.

Run: pytest tests/_parent_verify_aiplugin.py -q -s
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import create_app

PATH = "/.well-known/ai-plugin.json"


def _client(db_session):
    app = create_app()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    return app, TestClient(app)


def _urls(o, out=None):
    out = [] if out is None else out
    if isinstance(o, str) and ("http" in o or o.startswith("/")):
        out.append(o)
    elif isinstance(o, dict):
        for v in o.values():
            _urls(v, out)
    elif isinstance(o, list):
        for v in o:
            _urls(v, out)
    return out


def test_manifest_advertises_nothing_that_404s(db_session):
    app, c = _client(db_session)
    r = c.get(PATH)
    assert r.status_code == 200, r.text
    d = r.json()
    print("\nSTATUS:", r.status_code)
    print(
        "HEADERS:",
        {
            k: v
            for k, v in r.headers.items()
            if k.lower() in ("cache-control", "etag", "content-type")
        },
    )
    print(json.dumps(d, indent=1)[:1500])

    # Strip whatever origin this environment resolved to (tests default to
    # loopskill.io, prod is app.loopskill.io) so the probe works in BOTH.
    # An earlier version of this check hardcoded "app.loopskill.io", matched
    # nothing, silently probed ZERO urls and reported green — a false pass in
    # the verifier itself. Assert we actually probed something.
    from urllib.parse import urlparse

    bad, probed = [], []
    print("\n=== advertised URLs probed for real ===")
    for u in sorted(set(_urls(d))):
        path = urlparse(u).path if u.startswith("http") else u
        if not path.startswith("/") or path == "/":
            continue
        resp = c.get(path)
        # The promise being pinned is "nothing advertised here is a DEAD link"
        # — i.e. no 404/405-route-missing. A 401 is a LIVE route correctly
        # demanding a credential (/api/mcp/http is key-gated by design, and
        # the manifest's own auth block tells an agent how to get that key),
        # so it must not fail this check. Only route-absent statuses do.
        dead = resp.status_code in (404, 405) or (
            resp.status_code == 200
            and isinstance(resp.headers.get("content-type"), str)
            and "html" in resp.headers["content-type"]
            and "not found" in resp.text.lower()[:400]
        )
        probed.append(path)
        print(f"  {'DEAD' if dead else 'live'} {resp.status_code} {path}")
        if dead:
            bad.append((path, resp.status_code))

    assert probed, "verifier probed ZERO urls — the extractor is broken, not the manifest"
    print(f"  ({len(probed)} url(s) probed)")
    assert not bad, f"manifest advertises URL(s) that do not resolve: {bad}"
