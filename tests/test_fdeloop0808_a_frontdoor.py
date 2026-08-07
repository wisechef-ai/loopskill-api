"""fdeloop_0808 Phase A — front door + demand capture.

Four defect classes, one test module. Each class was reproduced live on prod
(2026-08-08) before a line of the fix was written; the docstrings carry the
reproduction so a future reader can tell a regression from a redesign.

1. ``_get_latest_version_and_tarball`` blindly takes ``versions[0]``. When the
   newest version's artifact is dead (13 rows on prod carry a ``tarball_path``
   under the retired ``/storage/skills/`` root), the whole skill 404s even
   though an older version resolves perfectly. ``resolution_status`` was added
   by converge_0208 P3 for exactly this and is read by NOTHING on the read
   path. Live repro: ``GET /api/skills/loopskill/files`` -> 404 "Tarball file
   missing for 'loopskill@1.0.0'".

2. Two tier policies disagree. ``authz.tier_rank_allows_install`` floors an
   unknown/NULL skill tier to free (rank 1) — a NULL-tier skill is INSTALLABLE
   by anyone. ``skill_files_routes`` treats the same NULL as paid and 403s.
   Live repro: ``GET /api/skills/agentic-os/file?path=SKILL.md`` -> 403 while
   ``/install`` allowed it. One row, two answers. Fix the POLICY, not the row.

3. ``MissingSkillQuery`` is written only from ``skill_routes.search_skills``
   — the first-party path over 55 curated skills. A zero-result search across
   the ~91k federated catalog (``/api/skills/external``, the path the portal's
   library + browse pages actually call, and ``/api/skills/metasearch``, the
   agent-facing one) records nothing. The majority of the demand signal is
   discarded.

4. Nothing stops a published SKILL.md referencing a slug that does not exist.
   Live repro: ``hundred-million-offers`` says "see obviously-awesome" and
   "see predictable-revenue"; both 404.
"""

from __future__ import annotations

import datetime as _dt
import uuid

import pytest

from app.models import MissingSkillQuery, Skill, SkillVersion


# ── helpers ──────────────────────────────────────────────────────────────


def _mk_skill(db, slug: str, *, tier=None, is_public=True, readme=""):
    s = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug.replace("-", " ").title(),
        description=f"{slug} description",
        tier=tier,
        is_public=is_public,
        is_archived=False,
        readme=readme,
    )
    db.add(s)
    db.flush()
    return s


def _mk_version(db, skill, semver, tarball_path, *, resolution_status="ok", created_at=None):
    v = SkillVersion(
        id=uuid.uuid4(),
        skill_id=skill.id,
        semver=semver,
        tarball_path=str(tarball_path),
        resolution_status=resolution_status,
        created_at=created_at or _dt.datetime.now(_dt.timezone.utc),
    )
    db.add(v)
    db.flush()
    return v


def _write_tarball(tmp_path, slug: str, semver: str) -> str:
    """Write a real 2-file .tar.gz so the manifest route has something to read."""
    import io
    import tarfile

    d = tmp_path / slug
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{semver}.tar.gz"
    with tarfile.open(p, "w:gz") as tf:
        for name, body in (("SKILL.md", f"# {slug}\n"), ("README.md", "readme\n")):
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return str(p)


# ── 1. dead-artifact fallback ────────────────────────────────────────────


class TestDeadArtifactFallback:
    """A skill with a dead NEWEST version must serve its newest RESOLVABLE one."""

    def test_newest_version_dead_falls_back_to_resolvable_older(self, db_session, tmp_path):
        """RED: versions[0] is dead -> 404, even though 1.0.1 has real bytes.

        This is the loopskill/llm-wiki-hermes shape exactly: a newer row created
        later points at the retired /storage root, an older row has live bytes.
        """
        from app.skill_files_routes import _get_latest_version_and_tarball

        s = _mk_skill(db_session, "dead-head-skill", tier="free")
        live = _write_tarball(tmp_path, "dead-head-skill", "1.0.1")
        older = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=2)
        _mk_version(db_session, s, "1.0.1", live, created_at=older)
        # newest by created_at, artifact absent — the defect row
        _mk_version(db_session, s, "2.1.0", "/storage/skills/dead-head-skill-2.1.0.tar.gz")
        db_session.flush()
        db_session.refresh(s)

        version, path = _get_latest_version_and_tarball(s)

        assert version.semver == "1.0.1", "must fall back to the newest RESOLVABLE version"
        assert path == live

    def test_version_marked_unresolvable_is_skipped_even_if_a_file_appears(
        self, db_session, tmp_path
    ):
        """``resolution_status='unresolvable'`` is an operator verdict and outranks
        an incidental file at that path — otherwise a stray byte-blob silently
        resurrects a version an operator declared dead."""
        from app.skill_files_routes import _get_latest_version_and_tarball

        s = _mk_skill(db_session, "condemned-skill", tier="free")
        good = _write_tarball(tmp_path, "condemned-skill", "1.0.0")
        condemned = _write_tarball(tmp_path, "condemned-skill", "2.0.0")
        older = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=2)
        _mk_version(db_session, s, "1.0.0", good, created_at=older)
        _mk_version(db_session, s, "2.0.0", condemned, resolution_status="unresolvable")
        db_session.flush()
        db_session.refresh(s)

        version, path = _get_latest_version_and_tarball(s)

        assert version.semver == "1.0.0"
        assert path == good

    def test_all_versions_dead_still_404s(self, db_session):
        """The fallback must not invent success. Nothing resolvable -> 404, and
        the message names the skill so an operator can act on it."""
        from fastapi import HTTPException

        from app.skill_files_routes import _get_latest_version_and_tarball

        s = _mk_skill(db_session, "fully-dead-skill", tier="free")
        _mk_version(db_session, s, "1.0.0", "/storage/skills/fully-dead-skill-1.0.0.tar.gz")
        db_session.flush()
        db_session.refresh(s)

        with pytest.raises(HTTPException) as exc:
            _get_latest_version_and_tarball(s)
        assert exc.value.status_code == 404
        assert "fully-dead-skill" in str(exc.value.detail)


# ── 2. one tier policy, not two ──────────────────────────────────────────


class TestTierPolicyIsSingleSourced:
    """File access and install authz must agree about a NULL tier."""

    def test_null_tier_is_free_for_file_access_matching_install_authz(self):
        """RED: ``(skill.tier or '').lower() == 'free'`` is False for NULL, so
        file access 403s a skill that ``tier_rank_allows_install`` lets an
        anonymous caller install. Live repro: agentic-os.
        """
        from app.authz import tier_rank_allows_install
        from app.skill_files_routes import skill_is_free_to_read

        assert tier_rank_allows_install(None, None) is True, "install path: NULL floors to free"
        assert skill_is_free_to_read(None) is True, "file path MUST agree with install path"

    @pytest.mark.parametrize(
        "tier,expected",
        [
            ("free", True),
            ("FREE", True),
            ("  free  ", True),
            (None, True),
            ("", True),
            ("pro", False),
            ("pro_plus", False),
            ("cook", False),  # legacy alias -> pro
            ("operator", False),  # legacy alias -> pro_plus
        ],
    )
    def test_free_predicate_matrix(self, tier, expected):
        from app.skill_files_routes import skill_is_free_to_read

        assert skill_is_free_to_read(tier) is expected

    def test_predicate_agrees_with_install_authz_across_the_matrix(self):
        """The two surfaces are only single-sourced if they agree everywhere,
        not just on NULL. An anonymous (free) caller's install verdict IS the
        read verdict."""
        from app.authz import tier_rank_allows_install
        from app.skill_files_routes import skill_is_free_to_read

        for tier in (None, "", "free", "FREE", "pro", "pro_plus", "cook", "operator", "bogus"):
            assert skill_is_free_to_read(tier) is tier_rank_allows_install(None, tier), (
                f"tier={tier!r}: file-access and install-authz disagree"
            )


# ── 3. demand capture on the federated paths ─────────────────────────────


class TestFederatedDemandCapture:
    """A zero-result search on ANY path is a demand signal."""

    def test_helper_records_a_zero_result_query(self, db_session):
        from app.services.demand_capture import record_missing_skill_query

        record_missing_skill_query(db_session, "mitsubishi melsec plc")

        row = db_session.query(MissingSkillQuery).one()
        assert row.query == "mitsubishi melsec plc"
        assert row.count == 1
        assert row.day == _dt.date.today()

    def test_repeat_same_day_increments_rather_than_duplicating(self, db_session):
        from app.services.demand_capture import record_missing_skill_query

        for _ in range(3):
            record_missing_skill_query(db_session, "copywriting")

        rows = db_session.query(MissingSkillQuery).all()
        assert len(rows) == 1, "one row per (lower(query), day) — no row explosion"
        assert rows[0].count == 3

    def test_case_and_whitespace_variants_collapse_to_one_row(self, db_session):
        """``lower(query)`` is the unique index; the helper must normalise so the
        brief counts demand, not typography."""
        from app.services.demand_capture import record_missing_skill_query

        for q in ("Copywriting", "copywriting", "  COPYWRITING  "):
            record_missing_skill_query(db_session, q)

        rows = db_session.query(MissingSkillQuery).all()
        assert len(rows) == 1
        assert rows[0].count == 3

    def test_blank_query_is_not_recorded(self, db_session):
        """An empty search is a browse, not a demand signal."""
        from app.services.demand_capture import record_missing_skill_query

        for q in ("", "   ", None):
            record_missing_skill_query(db_session, q)
        assert db_session.query(MissingSkillQuery).count() == 0

    def test_a_db_failure_never_propagates(self, db_session, monkeypatch):
        """Demand capture is fire-and-forget: it must never break a search."""
        from app.services import demand_capture

        def boom(*_a, **_k):
            raise RuntimeError("db is on fire")

        monkeypatch.setattr(demand_capture, "_upsert", boom)
        demand_capture.record_missing_skill_query(db_session, "anything")  # must not raise

    def test_external_route_records_zero_result_federated_query(self, db_session, monkeypatch):
        """THE Phase-A gate: a zero-result FEDERATED query writes a row.

        ``/api/skills/external`` is what the portal's library + browse pages
        call. Before this fix, grep of the federation modules for
        MissingSkillQuery returned zero hits.
        """
        from tests._app_factory import build_test_app
        from fastapi.testclient import TestClient

        app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
        client = TestClient(app)

        r = client.get(
            "/api/skills/external",
            params={"sources": "hermes-hub", "q": "zzz-no-such-skill-anywhere-zzz"},
        )
        assert r.status_code == 200
        assert r.json().get("external") == []

        row = (
            db_session.query(MissingSkillQuery)
            .filter(MissingSkillQuery.query == "zzz-no-such-skill-anywhere-zzz")
            .one_or_none()
        )
        assert row is not None, "zero-result federated search recorded NO demand signal"
        assert row.count == 1


# ── 4. the reference gate ────────────────────────────────────────────────


class TestSkillReferenceGate:
    """A published SKILL.md may not point at a slug that does not exist."""

    def test_extracts_see_slug_references(self):
        from app.services.skill_refs import extract_skill_references

        text = (
            "For product positioning, see obviously-awesome. "
            "For outbound sales, see predictable-revenue."
        )
        assert extract_skill_references(text) == {"obviously-awesome", "predictable-revenue"}

    def test_extracts_markdown_skill_links(self):
        from app.services.skill_refs import extract_skill_references

        text = "Pair it with [the mom test](/skills/mom-test) before you build."
        assert "mom-test" in extract_skill_references(text)

    def test_ignores_prose_that_merely_contains_the_word_see(self):
        """``see the docs`` is prose, not a reference. A gate that fires on prose
        gets disabled within a week."""
        from app.services.skill_refs import extract_skill_references

        text = "See the docs for details, and see https://example.com/thing too."
        assert extract_skill_references(text) == set()

    def test_dangling_reference_is_reported(self):
        from app.services.skill_refs import find_dangling_references

        published = {"hundred-million-offers", "mom-test"}
        readmes = {
            "hundred-million-offers": "For positioning, see obviously-awesome. Also see mom-test.",
        }
        dangling = find_dangling_references(readmes, published)
        assert dangling == {"hundred-million-offers": {"obviously-awesome"}}

    def test_clean_corpus_reports_nothing(self):
        from app.services.skill_refs import find_dangling_references

        published = {"a-skill", "b-skill"}
        readmes = {"a-skill": "Then see b-skill for the next step."}
        assert find_dangling_references(readmes, published) == {}

    def test_self_reference_is_not_dangling(self):
        from app.services.skill_refs import find_dangling_references

        published = {"a-skill"}
        readmes = {"a-skill": "As described above, see a-skill."}
        assert find_dangling_references(readmes, published) == {}


# ── 5. the gate actually gates ───────────────────────────────────────────


class TestReferenceGateFailsCI:
    """Proving the CI gate is wired, not merely written.

    A gate that exists but never returns non-zero is decoration. These drive the
    real ``main()`` against a seeded corpus and assert the exit code, which is
    exactly what CI reads.
    """

    def test_clean_corpus_exits_zero(self, db_session, monkeypatch, capsys):
        import scripts.audit_skill_references as gate

        _mk_skill(db_session, "alpha-skill", readme="Then see beta-skill for the next step.")
        _mk_skill(db_session, "beta-skill", readme="No references here.")
        db_session.flush()
        monkeypatch.setattr(gate, "SessionLocal", lambda: db_session)

        assert gate.main([]) == 0
        assert "No dangling skill references." in capsys.readouterr().out

    def test_adding_a_dangling_reference_exits_nonzero(self, db_session, monkeypatch, capsys):
        """THE gate: `see nonexistent-skill` must fail the build."""
        import scripts.audit_skill_references as gate

        _mk_skill(db_session, "alpha-skill", readme="For more, see nonexistent-skill.")
        db_session.flush()
        monkeypatch.setattr(gate, "SessionLocal", lambda: db_session)

        assert gate.main([]) == 1, "a dangling reference MUST fail CI"
        out = capsys.readouterr().out
        assert "alpha-skill -> nonexistent-skill" in out

    def test_unpublished_target_is_dangling(self, db_session, monkeypatch):
        """A private/archived skill is unreachable by a reader, so pointing at
        it is as broken as pointing at nothing."""
        import scripts.audit_skill_references as gate

        _mk_skill(db_session, "alpha-skill", readme="see hidden-skill for details")
        _mk_skill(db_session, "hidden-skill", is_public=False, readme="")
        db_session.flush()
        monkeypatch.setattr(gate, "SessionLocal", lambda: db_session)

        assert gate.main([]) == 1

    def test_json_mode_reports_the_same_verdict(self, db_session, monkeypatch, capsys):
        import json as _json

        import scripts.audit_skill_references as gate

        _mk_skill(db_session, "alpha-skill", readme="see nonexistent-skill")
        db_session.flush()
        monkeypatch.setattr(gate, "SessionLocal", lambda: db_session)

        rc = gate.main(["--json"])
        payload = _json.loads(capsys.readouterr().out)

        assert rc == 1
        assert payload["dangling_count"] == 1
        assert payload["dangling"]["alpha-skill"] == ["nonexistent-skill"]


# ── 6. the schema the upsert depends on ──────────────────────────────────


class TestDemandIndexIsDeclaredOnTheModel:
    """The functional unique index must live in ``Base.metadata``, not only in
    the migration.

    Found by the postgres CI matrix on 2026-08-08 and reproduced locally
    against postgres:17. ``missing_skill_queries`` declared its columns but not
    its index; the index existed only in topshelf_2605_h. Tests build their
    schema with ``Base.metadata.create_all``, so the table they got had NO
    functional index — and ``ON CONFLICT (lower(query), day)`` cannot infer an
    index that does not exist. Every upsert therefore matched nothing and wrote
    zero rows.

    It was invisible on SQLite (that branch does SELECT-then-write) and
    invisible in production (the migration DOES create the index there). Only
    the postgres test leg could see it — which is exactly the gap the
    mesh0408 T0-A dual-engine matrix was added to close.
    """

    def test_model_declares_the_functional_unique_index(self):
        idx = {i.name: i for i in MissingSkillQuery.__table__.indexes}
        assert "uq_missing_skill_queries_query_day" in idx, (
            "the upsert's ON CONFLICT target is not declared on the model — "
            "create_all() will build a table the upsert cannot use"
        )
        assert idx["uq_missing_skill_queries_query_day"].unique is True

    def test_index_is_on_lower_query_not_raw_query(self):
        """A plain ``(query, day)`` index would let 'SEO' and 'seo' become two
        rows, splitting the count the demand brief ranks on."""
        idx = next(
            i
            for i in MissingSkillQuery.__table__.indexes
            if i.name == "uq_missing_skill_queries_query_day"
        )
        rendered = str(idx.expressions[0]).lower()
        assert "lower(" in rendered, f"index must be on lower(query), got {rendered!r}"

    def test_index_matches_the_migration_definition(self):
        """Model and migration must agree, or prod and tests diverge silently.

        Guards the specific direction that bit us: the migration is the source
        of truth for prod, the model is the source of truth for tests, and they
        drifted.
        """
        import pathlib
        import re

        mig = (
            pathlib.Path(__file__).resolve().parent.parent
            / "alembic"
            / "versions"
            / "topshelf_2605_h_missing_skill_queries.py"
        ).read_text()

        # The postgres branch of the migration.
        m = re.search(
            r"CREATE UNIQUE INDEX (\w+)\s*\n\s*ON missing_skill_queries \(lower\(query\), day\)",
            mig,
        )
        assert m, "migration no longer creates the functional index this upsert needs"
        assert m.group(1) == "uq_missing_skill_queries_query_day"
