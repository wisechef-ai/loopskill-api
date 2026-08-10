"""bundles_0811 Phase P3.9 — federation is a live lookup, honestly labelled.

Covers the four honesty gaps closed by this phase:

1. **The deep-link surfacing decision (a).** Deep-link-only results (no
   resolvable install instruction) now surface in metasearch instead of being
   silently dropped — clearly labelled ``view_origin`` / "not installable".
   The card contract tests live in ``test_metasearch_card_contract.py``
   (``test_github_oss_deployable_but_unresolvable_falls_back_to_view_origin``);
   this file adds the reindex/admin-surface honesty pieces P3.9 also requires.

2. **``well-known`` renders as by-design, not failing** — already encoded as
   ``STRUCTURALLY_EMPTY_SOURCES``; this file pins that the ``/api/skills/external``
   admin surface actually SURFACES that flag per-source, and that clawhub's
   real ``last_error`` (e.g. "clawhub page 220: status=503") is surfaced
   honestly rather than hidden behind a bare count.

3. **The silent-auth-vanished detector.** A token-gated source (github-oss) at
   ``indexed_count=0`` is indistinguishable from a healthy empty walk unless
   the token's presence is checked separately — this is exactly the failure
   mode that hid the missing ``GITHUB_TOKEN`` for days on prod. Pins
   ``federation_live.token_gated_source_missing_auth`` and the reindex
   walker's honest ``last_error`` write when it fires.

4. **No public surface claims an owned catalog larger than what we serve** —
   documented via the module-docstring/self-host-doc edits (not independently
   testable here; see PR body for the grep evidence).
"""

from __future__ import annotations

import app.services.federation_live as fl


# ── silent-auth-vanished detector (P3.9 item 5) ───────────────────────────────


class TestTokenGatedAuthDetector:
    def test_github_oss_is_token_gated(self):
        assert "github-oss" in fl.TOKEN_GATED_SOURCES

    def test_well_known_is_not_token_gated(self):
        """well-known is STRUCTURALLY empty (no catalog), not auth-gated — the
        two failure classes must stay distinct; conflating them would make the
        admin surface lie about why a source is at zero."""
        assert "well-known" not in fl.TOKEN_GATED_SOURCES

    def test_missing_token_detected(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        assert fl.token_gated_source_missing_auth("github-oss") is True

    def test_present_token_not_flagged(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "x-test-token")
        assert fl.token_gated_source_missing_auth("github-oss") is False

    def test_non_gated_source_never_flagged(self, monkeypatch):
        """A source outside TOKEN_GATED_SOURCES must never be flagged, token or
        not — this predicate is only meaningful for the registered sources."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        assert fl.token_gated_source_missing_auth("well-known") is False
        assert fl.token_gated_source_missing_auth("clawhub") is False

    def test_gh_token_alias_also_satisfies(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "x-test-token")
        assert fl.token_gated_source_missing_auth("github-oss") is False


# ── reindex walker: honest last_error on silent-zero (P3.9 item 5) ───────────


class TestReindexSilentAuthHonesty:
    def test_github_oss_zero_without_token_records_last_error(self, monkeypatch):
        """The adapter's own graceful-empty degrade never raises, so the
        reindex walker's except-block never fires and last_error would
        otherwise stay NULL — the exact ambiguity that hid the missing token
        for days. The walker must now write an explicit, honest last_error."""
        import scripts.federation_reindex as fr
        from app.services import federation_cache as fcache

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setitem(fl.LIVE_FETCH, "github-oss", lambda q: [])

        writes: list[dict] = []

        def _rec(db, source, **kw):
            writes.append({"source": source, **kw})
            return None

        monkeypatch.setattr(fcache, "write_source_cache", _rec)

        report = fr.reindex_source(db=None, source_id="github-oss")
        assert report["status"] == "ok"
        assert report["indexed"] == 0
        assert "error" in report and "GITHUB_TOKEN" in report["error"]
        assert writes and writes[0]["indexed_count"] == 0
        assert writes[0]["last_error"] is not None
        assert "GITHUB_TOKEN" in writes[0]["last_error"]

    def test_github_oss_zero_with_token_present_records_no_error(self, monkeypatch):
        """A token-present github-oss walk that genuinely finds nothing (a real
        empty result for this query) must NOT be flagged as silent-auth-missing
        — the detector only fires when the token itself is absent."""
        import scripts.federation_reindex as fr
        from app.services import federation_cache as fcache

        monkeypatch.setenv("GITHUB_TOKEN", "x-test-token")
        monkeypatch.setitem(fl.LIVE_FETCH, "github-oss", lambda q: [])

        writes: list[dict] = []

        def _rec(db, source, **kw):
            writes.append({"source": source, **kw})
            return None

        monkeypatch.setattr(fcache, "write_source_cache", _rec)

        report = fr.reindex_source(db=None, source_id="github-oss")
        assert report["status"] == "ok"
        assert "error" not in report
        assert writes[0]["last_error"] is None

    def test_github_oss_nonzero_never_flagged(self, monkeypatch):
        """Any successful walk with real rows must never carry the
        auth-vanished last_error regardless of token state — the predicate
        only applies to the ambiguous indexed=0 case."""
        import scripts.federation_reindex as fr
        from app.services import federation_cache as fcache

        monkeypatch.setenv("GITHUB_TOKEN", "x-test-token")
        monkeypatch.setitem(
            fl.LIVE_FETCH,
            "github-oss",
            lambda q: [
                {
                    "full_name": "acme/cool-skill",
                    "name": "cool-skill",
                    "license": {"spdx_id": "MIT"},
                    "html_url": "https://github.com/acme/cool-skill",
                    "description": "d",
                }
            ],
        )

        writes: list[dict] = []

        def _rec(db, source, **kw):
            writes.append({"source": source, **kw})
            return None

        monkeypatch.setattr(fcache, "write_source_cache", _rec)

        report = fr.reindex_source(db=None, source_id="github-oss")
        assert report["indexed"] == 1
        assert "error" not in report
        assert writes[0]["last_error"] is None


# ── admin surface: structurally-empty-by-design + honest last_error ──────────


class TestAdminSurfaceHonesty:
    def test_well_known_reported_structurally_empty(self, client, monkeypatch):
        """`/api/skills/external` must tell the admin surface that well-known's
        zero is BY DESIGN, not a failing walk — pulled from the static
        STRUCTURALLY_EMPTY_SOURCES registry, never guessed from the count."""
        r = client.get("/api/skills/external")
        assert r.status_code == 200
        body = r.json()
        assert body["per_source"]["well-known"]["structurally_empty"] is True

    def test_non_empty_source_not_flagged_structurally_empty(self, client):
        r = client.get("/api/skills/external")
        body = r.json()
        assert body["per_source"]["github-oss"]["structurally_empty"] is False
        assert body["per_source"]["hermes-hub"]["structurally_empty"] is False

    def test_clawhub_last_error_surfaced_from_cache(self, client, db_session):
        """A source's real last_error (e.g. clawhub's page-503 partial failure)
        must be readable from the admin surface, not hidden behind a bare
        indexed count — P3.9 item 3."""
        from app.services import federation_cache as fcache

        fcache.write_source_cache(
            db_session,
            "clawhub",
            indexed_count=42892,
            installable_count=0,
            last_error="clawhub page 220: status=503",
        )
        r = client.get("/api/skills/external")
        body = r.json()
        assert body["per_source"]["clawhub"]["last_error"] == "clawhub page 220: status=503"
        # the count still reports honestly despite the partial failure
        assert body["per_source"]["clawhub"]["indexed"] == 42892

    def test_source_with_no_error_reports_null(self, client, db_session):
        from app.services import federation_cache as fcache

        fcache.write_source_cache(db_session, "hermes-hub", indexed_count=100, installable_count=100)
        r = client.get("/api/skills/external")
        body = r.json()
        assert body["per_source"]["hermes-hub"]["last_error"] is None
