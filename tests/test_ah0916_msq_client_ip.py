"""ah_0916 — client_ip persistence on missing_skill_queries.

THE GAP THESE TESTS CLOSE

``record_missing_skill_query()`` has accepted ``client_ip`` since
coldstart_0609/A, and both live callers (skill_routes, metasearch_routes)
already pass it — but it was only ever fed to ``is_probe_request()`` and then
discarded. ``is_probe`` closes the *known*-fleet half of attribution (known
api-key owners, fixed loopback IPs). It cannot close the ANONYMOUS half: a row
with no ``user_id`` and no ``api_key_id`` has nothing left to correlate on, so
the reach feeder must bucket the whole anonymous set as UNATTRIBUTABLE (82 rows
as of 2026-09-16) — the same blindness that let 485 fleet echo signals pass as
external demand before the 2026-09-09 echo-removal fix.

These tests pin the four things that make the column an instrument rather than
decoration:
  1. an anonymous miss PERSISTS the IP it came from (the whole point);
  2. a caller with no request context still works and stores NULL — "unknown"
     must stay distinguishable from "known to be X", so no '' / '0.0.0.0'
     backfill sneaks in;
  3. first-writer-wins on repeat: the row is a per-(query, day) COUNTER, so a
     second hit increments ``count`` and does NOT overwrite the first IP.
     Overwriting would silently redefine the column as "most recent searcher",
     a different and useless statistic — and the Postgres ON CONFLICT DO UPDATE
     omits client_ip precisely so this holds on BOTH dialects;
  4. the column exists on the mapped table with the same String(64)/nullable
     shape as telemetry_events.client_ip.

Test 3 is the one that would catch a future "helpful" DO UPDATE SET client_ip.
"""

from __future__ import annotations

import uuid

from sqlalchemy import inspect

from app.models import MissingSkillQuery
from app.services.demand_capture import record_missing_skill_query

_IP_A = "203.0.113.77"  # TEST-NET-3, never a real fleet host
_IP_B = "198.51.100.9"  # TEST-NET-2


def _rows(db, q):
    return db.query(MissingSkillQuery).filter(MissingSkillQuery.query == q).all()


def test_anonymous_miss_persists_client_ip(db_session):
    """The headline: a signed-out stranger's miss is now attributable."""
    q = "ah0916-anon-" + uuid.uuid4().hex[:8]

    assert record_missing_skill_query(db_session, q, client_ip=_IP_A) is True

    rows = _rows(db_session, q)
    assert len(rows) == 1
    assert rows[0].client_ip == _IP_A
    # Still anonymous in every other respect — that is exactly why the IP matters.
    assert rows[0].user_id is None


def test_missing_request_context_stores_null_not_placeholder(db_session):
    """No IP available => NULL. Never '' or a sentinel.

    A backfilled placeholder would manufacture provenance we do not have and
    make 'unknown origin' indistinguishable from 'known to be this origin',
    which is the precise failure the column exists to fix.
    """
    q = "ah0916-noctx-" + uuid.uuid4().hex[:8]

    assert record_missing_skill_query(db_session, q) is True

    rows = _rows(db_session, q)
    assert len(rows) == 1
    assert rows[0].client_ip is None


def test_repeat_hit_increments_count_and_keeps_first_ip(db_session):
    """First-writer-wins. Guards against a future DO UPDATE SET client_ip.

    The row is a per-(query, day) counter, so the second searcher's IP does not
    describe it any better than the first's. Both dialect branches must agree.
    """
    q = "ah0916-repeat-" + uuid.uuid4().hex[:8]

    record_missing_skill_query(db_session, q, client_ip=_IP_A)
    record_missing_skill_query(db_session, q, client_ip=_IP_B)

    rows = _rows(db_session, q)
    assert len(rows) == 1, "same query+day must stay ONE row"
    assert rows[0].count == 2, "the second hit must still be counted"
    assert rows[0].client_ip == _IP_A, "first writer wins; IP must not be overwritten"


def test_column_shape_matches_sibling_client_ip_columns(db_session):
    """String(64), nullable — same shape as telemetry_events/install_events."""
    cols = {c["name"]: c for c in inspect(db_session.get_bind()).get_columns("missing_skill_queries")}

    assert "client_ip" in cols, "migration ah0916_msq_client_ip did not apply"
    assert cols["client_ip"]["nullable"] is True
    # Length is asserted via the ORM so the check is dialect-independent
    # (SQLite reports VARCHAR(64), Postgres VARCHAR(64) — but type objects differ).
    assert MissingSkillQuery.__table__.c.client_ip.type.length == 64
