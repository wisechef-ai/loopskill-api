"""perclient_1110 — first-party page-view beacon ``POST /api/pv``.

app.loopskill.io has no access log (Caddy logs nothing), so the portal fires a
same-origin beacon and the API records ONE ``telemetry_events`` row per
qualified view (``event_type='page_view'``). Contract pinned here:

* anonymous (no x-api-key, no cookie) → 204, never 401;
* allowlisted path (``/use-cases/``, ``/blog/``) → 204 + one row, utm fields
  and the referrer HOST stored in the JSON payload;
* junk (non-allowlisted path, oversized / malformed body) → 204, NO row —
  never a 4xx/5xx, never an oracle;
* ``navigator.sendBeacon`` sends ``text/plain`` — accepted;
* bot User-Agent → ``ua_bot=true`` and ``class='unknown'``;
* fleet IP (fleet_exclusions) → ``class='fleet'`` via funnel_ledger.classify;
* per-IP limit → over-limit is a silent 204 with no row.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models import TelemetryEvent
from app.services.funnel_ledger import clear_fleet_exclusions_cache

STRANGER_IP = "203.0.113.7"
FLEET_IP = "198.51.100.42"
PAGE = "/use-cases/one-private-bundle-per-client/"
HUMAN_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point fleet exclusions at a tiny fixture config + reset the limiter."""
    cfg = tmp_path / "fleet_exclusions.yaml"
    cfg.write_text(f"emails: []\nips:\n  - {FLEET_IP}\napi_key_ids: []\n")
    monkeypatch.setattr(settings, "FUNNEL_FLEET_EXCLUSIONS_PATH", str(cfg), raising=False)
    clear_fleet_exclusions_cache()
    try:
        from app.services import pageview
    except ImportError:  # RED phase: module not written yet
        pageview = None
    if pageview:
        pageview.reset_rate_limiter()
    yield
    if pageview:
        pageview.reset_rate_limiter()
    clear_fleet_exclusions_cache()


def _client(db_session, monkeypatch, ip: str = STRANGER_IP) -> TestClient:
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, raise_server_exceptions=True, client=(ip, 50000))


def _rows(db_session) -> list[TelemetryEvent]:
    db_session.expire_all()
    return db_session.query(TelemetryEvent).filter(TelemetryEvent.event_type == "page_view").all()


def _payload(row: TelemetryEvent) -> dict:
    return json.loads(row.payload)


def test_route_is_mounted_in_real_create_app():
    from app.main import create_app

    assert "/api/pv" in {getattr(r, "path", None) for r in create_app().routes}


def test_allowlisted_path_writes_one_row_with_utm_and_ref_host(db_session, monkeypatch):
    c = _client(db_session, monkeypatch)
    body = {
        "path": PAGE,
        "utm_source": "x",
        "utm_medium": "social",
        "utm_campaign": "perclient_1110",
        "utm_content": "thread-1",
        "referrer": "https://t.co/abc?x=1",
    }
    r = c.post("/api/pv", json=body, headers={"user-agent": HUMAN_UA})
    assert r.status_code == 204
    assert r.content == b""

    rows = _rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.skill_slug is None
    assert row.client_ip == STRANGER_IP
    p = _payload(row)
    assert p["path"] == PAGE
    assert p["utm_source"] == "x"
    assert p["utm_medium"] == "social"
    assert p["utm_campaign"] == "perclient_1110"
    assert p["utm_content"] == "thread-1"
    assert p["ref_host"] == "t.co"
    assert "referrer" not in p  # only the host is stored, never the full URL
    assert p["ua_bot"] is False
    assert p["class"] == "stranger"


def test_anonymous_request_is_not_401(db_session, monkeypatch):
    """No x-api-key and no cookie: the middleware must let the beacon through."""
    c = _client(db_session, monkeypatch)
    r = c.post("/api/pv", json={"path": "/blog/hello"})
    assert r.status_code == 204
    assert len(_rows(db_session)) == 1


def test_non_allowlisted_path_is_204_and_writes_nothing(db_session, monkeypatch):
    c = _client(db_session, monkeypatch)
    for path in ("/pricing", "/", "/admin/use-cases/", "https://evil.example/use-cases/x", ""):
        r = c.post("/api/pv", json={"path": path})
        assert r.status_code == 204, path
    assert _rows(db_session) == []


def test_text_plain_body_from_sendbeacon_is_accepted(db_session, monkeypatch):
    c = _client(db_session, monkeypatch)
    r = c.post(
        "/api/pv",
        content=json.dumps({"path": PAGE, "utm_source": "hn"}),
        headers={"content-type": "text/plain;charset=UTF-8", "user-agent": HUMAN_UA},
    )
    assert r.status_code == 204
    rows = _rows(db_session)
    assert len(rows) == 1
    assert _payload(rows[0])["utm_source"] == "hn"


def test_oversized_body_is_204_and_writes_nothing(db_session, monkeypatch):
    c = _client(db_session, monkeypatch)
    big = json.dumps({"path": PAGE, "utm_content": "a" * 5000})
    r = c.post("/api/pv", content=big, headers={"content-type": "text/plain"})
    assert r.status_code == 204
    assert _rows(db_session) == []


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[1,2,3]", b'"just a string"', b"", b'{"path": 123}', b"\xff\xfe\x00"],
)
def test_malformed_body_is_204_and_writes_nothing(db_session, monkeypatch, raw):
    c = _client(db_session, monkeypatch)
    r = c.post("/api/pv", content=raw, headers={"content-type": "text/plain"})
    assert r.status_code == 204
    assert _rows(db_session) == []


def test_strings_are_truncated(db_session, monkeypatch):
    c = _client(db_session, monkeypatch)
    body = {
        "path": "/blog/" + "p" * 400,
        "utm_source": "s" * 300,
        "referrer": "https://" + ("h" * 60 + ".") * 3 + "example.com/x",
    }
    r = c.post("/api/pv", json=body)
    assert r.status_code == 204
    p = _payload(_rows(db_session)[0])
    assert len(p["path"]) == 200
    assert p["path"].startswith("/blog/")
    assert len(p["utm_source"]) == 64
    assert len(p["ref_host"]) <= 120


def test_query_string_is_not_stored_in_path(db_session, monkeypatch):
    c = _client(db_session, monkeypatch)
    c.post("/api/pv", json={"path": PAGE + "?email=a@b.c#frag"})
    assert _payload(_rows(db_session)[0])["path"] == PAGE


def test_bot_user_agent_is_flagged_and_class_unknown(db_session, monkeypatch):
    c = _client(db_session, monkeypatch)
    r = c.post(
        "/api/pv",
        json={"path": PAGE},
        headers={"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"},
    )
    assert r.status_code == 204
    p = _payload(_rows(db_session)[0])
    assert p["ua_bot"] is True
    assert p["class"] == "unknown"


@pytest.mark.parametrize(
    "ua", ["curl/8.5.0", "python-requests/2.32", "HeadlessChrome/120", "Go-http-client/1.1"]
)
def test_tooling_user_agents_are_bots(db_session, monkeypatch, ua):
    c = _client(db_session, monkeypatch)
    c.post("/api/pv", json={"path": PAGE}, headers={"user-agent": ua})
    p = _payload(_rows(db_session)[0])
    assert p["ua_bot"] is True
    assert p["class"] == "unknown"


def test_fleet_ip_classifies_fleet(db_session, monkeypatch):
    c = _client(db_session, monkeypatch, ip=FLEET_IP)
    r = c.post("/api/pv", json={"path": PAGE}, headers={"user-agent": HUMAN_UA})
    assert r.status_code == 204
    row = _rows(db_session)[0]
    assert row.client_ip == FLEET_IP
    assert _payload(row)["class"] == "fleet"


def test_over_rate_limit_is_silent_204_without_row(db_session, monkeypatch):
    from app.services import pageview

    monkeypatch.setattr(pageview, "RATE_LIMIT_MAX", 3)
    c = _client(db_session, monkeypatch)
    statuses = [c.post("/api/pv", json={"path": PAGE}).status_code for _ in range(5)]
    assert statuses == [204] * 5
    assert len(_rows(db_session)) == 3


def test_allowlist_is_a_module_constant():
    from app.services import pageview

    assert pageview.ALLOWED_PATH_PREFIXES == ("/use-cases/", "/blog/")


# ── review hardening (codex gpt-6.1-sol SHOULD-FIX on PR #402) ─────────────


def test_chunked_oversized_body_without_content_length_writes_nothing(db_session, monkeypatch):
    """A streamed body with no Content-Length is bounded by the reader, not the header."""
    c = _client(db_session, monkeypatch)
    big = json.dumps({"path": PAGE, "utm_content": "a" * 5000}).encode()

    def gen():
        yield big[:10]
        yield big[10:]

    r = c.post("/api/pv", content=gen(), headers={"content-type": "text/plain"})
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 204
    assert _rows(db_session) == []


def test_chunked_small_body_without_content_length_is_recorded(db_session, monkeypatch):
    c = _client(db_session, monkeypatch)
    body = json.dumps({"path": PAGE}).encode()

    def gen():
        yield body[:5]
        yield body[5:]

    r = c.post("/api/pv", content=gen(), headers={"content-type": "text/plain", "user-agent": HUMAN_UA})
    assert r.status_code == 204
    assert len(_rows(db_session)) == 1


def test_spoofed_forwarded_for_from_untrusted_peer_is_ignored(db_session, monkeypatch):
    """A direct (untrusted) caller cannot claim a fleet IP via X-Forwarded-For."""
    monkeypatch.setattr(settings, "TRUSTED_PROXY_CIDRS", ["127.0.0.1/32"], raising=False)
    c = _client(db_session, monkeypatch, ip=STRANGER_IP)
    r = c.post(
        "/api/pv",
        json={"path": PAGE},
        headers={"user-agent": HUMAN_UA, "x-forwarded-for": FLEET_IP, "cf-connecting-ip": FLEET_IP},
    )
    assert r.status_code == 204
    row = _rows(db_session)[0]
    assert row.client_ip == STRANGER_IP
    assert _payload(row)["class"] == "stranger"


def test_forwarded_for_from_trusted_proxy_is_honoured(db_session, monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_CIDRS", ["127.0.0.1/32"], raising=False)
    c = _client(db_session, monkeypatch, ip="127.0.0.1")
    r = c.post(
        "/api/pv", json={"path": PAGE}, headers={"user-agent": HUMAN_UA, "x-forwarded-for": STRANGER_IP}
    )
    assert r.status_code == 204
    assert _rows(db_session)[0].client_ip == STRANGER_IP


def test_db_write_failure_is_still_204(db_session, monkeypatch):
    """A failed commit must never surface as a 5xx (best-effort analytics)."""
    c = _client(db_session, monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db_session, "commit", boom)
    r = c.post("/api/pv", json={"path": PAGE}, headers={"user-agent": HUMAN_UA})
    assert r.status_code == 204
    assert r.content == b""
