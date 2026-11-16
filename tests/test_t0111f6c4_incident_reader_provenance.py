"""t_0111f6c4 — the three non-public readers of `incident_reports` must agree
with the transparency metric on what a "skill error" is.

``IncidentReport.report_type`` (t_a0607d92 / PR #474) split one table into two
populations that had always been physically mixed:

  * ``TYPE_SKILL_ERROR`` — an agent reporting that *a marketplace skill* it ran
    misbehaved. POST /api/v1/skill-error, MCP loopskill_report_skill_error.
  * ``TYPE_INCIDENT``   — a crash report of LoopSkill's own auto-improve tooling.
    POST /api/feedback/incident. Names a skill_id because the column is NOT NULL,
    not because the skill did anything.

The public metric was filtered there; the auto-improve lane and the graph edge
still counted the union. Why that matters, per reader:

  * clustering counts the union -> a crash report can tip ``distinct_agents``
    past the threshold and mint a PatchCandidate, i.e. a request to patch a
    creator's skill derived from LoopSkill's own stack trace.
  * the drafter pulls the 3 most-recent rows for the signature with no provenance
    filter -> the prompt is built from crash text, and parse_draft only checks
    that the reply LOOKS like a diff + test, so a wrong patch sails to canary.
  * failed_after_edges serves the public no-auth GET /api/graph/related.

Also pinned here, because they are the parts a later edit quietly loses:

  * clustering and the drafter read through ONE shared predicate, so they cannot
    drift apart (test_..._share_one_predicate, test_no_cron_hand_writes...).
  * unclassified rows fall to the crash side, never the skill-error side.
  * dashboard_routes contains no IncidentReport read at all — its old comment
    invited exactly the badge this test forbids.

Measured on prod 2026-11-16, for honesty about impact: incident_reports held 11
rows, all report_type='skill_error', zero crash reports ever filed, and
patch_candidates held 0 rows — the clustering and drafter systemd timers are not
installed on any host (deploy/*.timer ship but no unit is enabled on hq or
agents, and their ExecStart points at a nonexistent /srv/recipes-api). So this
closes a latent defect on a currently-dark lane; it changes no number visible
today.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.crons.incident_clustering import find_clusters, run_once
from app.crons.patch_drafter import _top_reports, draft_patch
from app.models import IncidentReport, InstallEvent, PatchCandidate, Skill, SkillVersion

SIG_A = "a" * 64
CRASH_MARKER = "CRASH-ONLY-MARKER-loopskill-tooling-died"
SKILL_MARKER = "SKILL-ONLY-MARKER-creator-skill-misbehaved"


def _mk_skill(db, slug):
    s = Skill(id=uuid4(), slug=slug, title=slug, is_public=True)
    db.add(s)
    db.flush()
    return s


def _mk_version(db, skill, semver="1.0.0"):
    v = SkillVersion(id=uuid4(), skill_id=skill.id, semver=semver, tarball_path=f"/x/{semver}.tar.gz")
    db.add(v)
    db.flush()
    return v


def _mk_row(
    db,
    skill,
    *,
    report_type,
    sig=SIG_A,
    agent="agent-1",
    occurred_at=None,
    command="run",
    stack="frame1\nframe2",
):
    r = IncidentReport(
        id=uuid4(),
        skill_id=skill.id,
        error_signature=sig,
        env_fingerprint={"os": "linux"},
        agent_fp_anon=agent,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        command=command,
        exit_code=1,
        stack_trace_top=stack,
        report_type=report_type,
    )
    db.add(r)
    db.flush()
    return r


def _skill_error(db, skill, **kw):
    kw.setdefault("report_type", IncidentReport.TYPE_SKILL_ERROR)
    return _mk_row(db, skill, **kw)


def _crash(db, skill, **kw):
    kw.setdefault("report_type", IncidentReport.TYPE_INCIDENT)
    return _mk_row(db, skill, **kw)


def _code_only_source(module) -> str:
    """Module source with comments and docstrings stripped, via the AST.

    A plain substring scan of the raw file is too blunt for these two crons: both
    now carry docstrings that *discuss* provenance and therefore name report_type
    on purpose. What must not exist is report_type in executable code.
    """
    path = inspect.getsourcefile(module)
    assert path is not None, f"no source file for {module}"
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    docstring_nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, docstring_nodes):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            # Replace rather than delete: a docstring-only body must stay valid.
            body[0] = ast.Expr(value=ast.Constant(value="<docstring stripped>"))
    return ast.unparse(tree)


# ── the canonical predicate itself ─────────────────────────────────────


def test_predicate_is_equality_on_the_skill_error_constant():
    """The shared filter must compare report_type to TYPE_SKILL_ERROR exactly.

    Everything below hangs off this one expression; if it is pointed at the wrong
    constant the whole file's guarantees are decoration.
    """
    expr = IncidentReport.skill_error_rows()
    assert str(expr.left.name) == "report_type"
    assert expr.right.value == "skill_error" == IncidentReport.TYPE_SKILL_ERROR


def test_unclassified_rows_default_to_the_crash_side():
    """Inherited decision: a row that forgot to classify lands on 'incident'.

    Under-counting skill errors is the survivable direction, so the server_default
    must never be the skill-error value.
    """
    col = IncidentReport.__table__.c["report_type"]
    assert col.server_default.arg == IncidentReport.TYPE_INCIDENT
    assert col.nullable is False


def test_row_without_report_type_is_not_clustered(db_session):
    """A writer that omits report_type inherits the default and must be excluded."""
    skill = _mk_skill(db_session, "unclassified-target")
    for agent in ("a1", "a2", "a3"):
        r = IncidentReport(
            id=uuid4(),
            skill_id=skill.id,
            error_signature=SIG_A,
            env_fingerprint={},
            agent_fp_anon=agent,
            occurred_at=datetime.now(timezone.utc),
        )
        db_session.add(r)
    db_session.flush()
    # Sanity: the default really did apply, else this test proves nothing.
    assert {x.report_type for x in db_session.query(IncidentReport)} == {IncidentReport.TYPE_INCIDENT}
    assert find_clusters(db_session) == []


# ── reader 1: incident_clustering ──────────────────────────────────────


def test_crash_reports_never_seed_a_patch_candidate(db_session):
    """3 distinct agents reporting a crash must NOT produce a candidate.

    This is the card's stated risk verbatim: without the filter these three rows
    satisfy COUNT(*)>=3 AND COUNT(DISTINCT agent_fp_anon)>=3 and the auto-improve
    lane drafts a patch for a "bug" that was its own tooling dying.
    """
    skill = _mk_skill(db_session, "crash-only")
    for agent in ("a1", "a2", "a3"):
        _crash(db_session, skill, agent=agent)

    assert find_clusters(db_session) == []
    assert run_once(db_session) == 0
    assert db_session.query(PatchCandidate).count() == 0


def test_skill_errors_still_cluster_control(db_session):
    """Mirror image of the test above — the filter must not break the real rail."""
    skill = _mk_skill(db_session, "skill-error-target")
    for agent in ("a1", "a2", "a3"):
        _skill_error(db_session, skill, agent=agent)

    clusters = find_clusters(db_session)
    assert len(clusters) == 1
    assert clusters[0].cluster_count == 3
    assert clusters[0].distinct_agents == 3
    assert run_once(db_session) == 1


def test_crash_row_cannot_tip_a_cluster_over_the_threshold(db_session):
    """The quiet version of the bug: 2 real skill errors + 1 unrelated crash.

    Both halves of the threshold are met only because of the crash row, so the
    candidate looks like a fleet-confirmed skill bug and is neither.
    """
    skill = _mk_skill(db_session, "mixed-threshold")
    _skill_error(db_session, skill, agent="a1")
    _skill_error(db_session, skill, agent="a2")
    _crash(db_session, skill, agent="a3")

    assert find_clusters(db_session) == []
    assert db_session.query(PatchCandidate).count() == 0


def test_cluster_metrics_exclude_crash_rows(db_session):
    """A qualifying cluster must report skill-error counts, not union counts.

    cluster_count / distinct_agents are copied onto the PatchCandidate and are
    what a human triaging the queue reads as "how many agents hit this".
    """
    skill = _mk_skill(db_session, "mixed-metrics")
    for agent in ("a1", "a2", "a3"):
        _skill_error(db_session, skill, agent=agent)
    for agent in ("a4", "a5"):
        _crash(db_session, skill, agent=agent)

    clusters = find_clusters(db_session)
    assert len(clusters) == 1
    assert clusters[0].cluster_count == 3, "crash rows leaked into cluster_count"
    assert clusters[0].distinct_agents == 3, "crash agents leaked into distinct_agents"


# ── reader 2: patch_drafter._top_reports ───────────────────────────────


def test_top_reports_excludes_crash_rows(db_session):
    skill = _mk_skill(db_session, "drafter-filter")
    keep = _skill_error(db_session, skill, agent="a1")
    _crash(db_session, skill, agent="a2")
    _crash(db_session, skill, agent="a3")

    got = _top_reports(db_session, skill.id, SIG_A)
    assert [r.id for r in got] == [keep.id]


def test_draft_prompt_never_contains_crash_report_text(db_session, tmp_path):
    """The contamination test that matters — what the model is actually shown.

    The crash row is the NEWEST, so an unfiltered `_top_reports` (order_by
    occurred_at desc, limit 3) puts it first and _build_prompt prints its
    command/exit_code/stack_trace_top into the prompt. The model is then asked
    for a unified diff against the creator's skill with LoopSkill's own crash as
    the only evidence, and parse_draft would wave through any reply that merely
    looks like a diff plus a test.
    """
    skill = _mk_skill(db_session, "prompt-contamination")
    _mk_version(db_session, skill, "2.0.0")
    now = datetime.now(timezone.utc)
    _skill_error(
        db_session, skill, agent="a1", occurred_at=now - timedelta(hours=2),
        command=SKILL_MARKER, stack=f"{SKILL_MARKER} frame",
    )
    _crash(
        db_session, skill, agent="a2", occurred_at=now,
        command=CRASH_MARKER, stack=f"{CRASH_MARKER} frame",
    )
    cand = PatchCandidate(
        id=uuid4(), skill_id=skill.id, error_signature=SIG_A,
        cluster_count=3, distinct_agents=3, status="pending",
    )
    db_session.add(cand)
    db_session.flush()

    captured: dict[str, str] = {}

    def fake_llm(prompt: str) -> str:
        captured["prompt"] = prompt
        return "```diff\n--- a/x.py\n+++ b/x.py\n+x\n```\n```python\ndef test_r(): pass\n```"

    result = draft_patch(cand, db=db_session, llm_call=fake_llm, proposal_dir=tmp_path)
    assert result.runnable
    prompt = captured["prompt"]
    assert SKILL_MARKER in prompt, "legitimate skill-error evidence went missing"
    assert CRASH_MARKER not in prompt, "crash-report text was fed to the patch prompt"


def test_drafting_from_crash_only_candidate_raises_honestly(db_session, tmp_path):
    """A crash-only candidate has no drafting evidence; the error must say so.

    Guards against a future edit 'fixing' this by quietly falling back to the
    union when the filtered query comes back empty.
    """
    skill = _mk_skill(db_session, "crash-only-draft")
    _mk_version(db_session, skill)
    _crash(db_session, skill, agent="a1")
    cand = PatchCandidate(
        id=uuid4(), skill_id=skill.id, error_signature=SIG_A,
        cluster_count=3, distinct_agents=3, status="pending",
    )
    db_session.add(cand)
    db_session.flush()

    with pytest.raises(ValueError, match="no incident reports") as exc:
        draft_patch(cand, db=db_session, llm_call=lambda p: "x", proposal_dir=tmp_path)
    assert "skill-error provenance" in str(exc.value)


# ── the two crons must not drift apart ─────────────────────────────────


def test_clustering_and_drafter_read_through_one_predicate(db_session, monkeypatch):
    """Both crons must obtain the filter from the SAME shared source.

    Two hand-written ``report_type == 'skill_error'`` literals behave identically
    today and diverge the moment one is edited — which is precisely how these
    readers drifted from the transparency metric in the first place. Wrapping the
    classmethod and requiring BOTH call sites to hit it turns that intent into a
    gate: replace either with a literal and its counter stays 0.
    """
    calls = {"n": 0}
    original = IncidentReport.skill_error_rows.__func__

    def counting_cls(cls):
        calls["n"] += 1
        return original(cls)

    monkeypatch.setattr(IncidentReport, "skill_error_rows", classmethod(counting_cls))

    skill = _mk_skill(db_session, "coupling")
    for agent in ("a1", "a2", "a3"):
        _skill_error(db_session, skill, agent=agent)

    find_clusters(db_session)
    after_clustering = calls["n"]
    _top_reports(db_session, skill.id, SIG_A)

    assert after_clustering >= 1, "incident_clustering no longer uses the shared predicate"
    assert calls["n"] == after_clustering + 1, "patch_drafter._top_reports no longer uses the shared predicate"


@pytest.mark.parametrize("mod_name", ["app.crons.incident_clustering", "app.crons.patch_drafter"])
def test_no_cron_hand_writes_a_report_type_comparison(mod_name):
    """Structural backstop to the one above: no report_type in executable code.

    A behavioural test can be forgotten around a regression; a source scan cannot.
    Scanned code-only (docstrings/comments stripped) because both crons now discuss
    provenance in prose and naming report_type there is the point. If this fails,
    either the shared predicate was replaced by a hand-rolled comparison, or
    someone added a second, independent provenance filter to a cron — which is the
    drift this card exists to prevent.
    """
    import importlib

    src = _code_only_source(importlib.import_module(mod_name))
    assert "report_type" not in src, (
        f"{mod_name} references report_type in code; use IncidentReport.skill_error_rows() "
        "so this reader cannot drift from the transparency metric"
    )
    assert "skill_error_rows()" in src, f"{mod_name} lost its provenance filter entirely"


# ── reader 3: the dashboard badge the card believed existed ────────────


def test_dashboard_routes_contains_no_incident_report_read():
    """Pinned because the card's premise #3 was wrong, and wrong premises repeat.

    The card asked for a TYPE_SKILL_ERROR filter on "the badge at
    app/dashboard_routes.py:124". There is no badge: voice_counts has exactly two
    keys and the only thing at line 124 was a COMMENT naming IncidentReport as
    where skill errors live — and it listed POST /api/feedback/incident, a crash
    rail, among them. That comment was an invitation to implement the badge by
    counting the whole table. Comments are not in the AST, so this scan allows the
    corrected prose and forbids only real code that reads the table.
    """
    import app.dashboard_routes as dashboard

    dash_path = inspect.getsourcefile(dashboard)
    assert dash_path is not None, "no source file for dashboard_routes"
    tree = ast.parse(Path(dash_path).read_text(encoding="utf-8"))
    offenders = [
        f"Name {node.id!r} at line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "IncidentReport"
    ] + [
        f"string containing 'incident_reports' at line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "incident_reports" in node.value
    ] + [
        f"call to skill_error_rows at line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "skill_error_rows"
    ]
    assert offenders == [], f"dashboard_routes gained an incident_reports read: {offenders}"
    assert not hasattr(dashboard, "IncidentReport"), "dashboard_routes must not import IncidentReport"


# ── bonus reader: the public graph edge (found here, not on the card) ──


def _seed_graph_pair(db, *, preceder_offset):
    preceder = _mk_skill(db, "preceder-t0111")
    victim = _mk_skill(db, "victim-t0111")
    t0 = datetime.now(timezone.utc)
    db.add(
        InstallEvent(
            id=uuid4(),
            skill_id=preceder.id,
            skill_slug=preceder.slug,
            created_at=t0 + preceder_offset,
        )
    )
    db.flush()
    return victim, t0


def test_failed_after_edges_ignores_crash_reports_entirely(db_session):
    """Real-schema shape (models.IncidentReport), not tests/test_graph_extension's.

    GET /api/graph/related is public and unauthenticated, so this is a second
    public surface claiming something about skills from the union. Against the
    real schema this function takes the skill_id JOIN branch, where report_type
    exists — unlike the slug-shaped table the existing graph tests drop in, which
    has no report_type column and therefore exercises the unfiltered branch.
    """
    from app.graph_extension import failed_after_edges

    victim, t0 = _seed_graph_pair(db_session, preceder_offset=timedelta(minutes=-2))
    _crash(db_session, victim, agent="a1", occurred_at=t0)

    assert db_session.query(IncidentReport).count() == 1, "seed did not land"
    assert failed_after_edges(db_session, "victim-t0111") == []


def test_failed_after_edges_counts_only_skill_errors(db_session):
    """Mixed population, deliberately asymmetric: 2 skill errors vs 1 crash.

    Equal counts would not discriminate — one of each gives evidence_count 1
    whether the filter selects skill errors or, inverted, crash reports. With 2
    vs 1 the assertion pins the DIRECTION of the filter, not merely its presence
    (a missing filter reads 3, an inverted one reads 1, the correct filter 2).
    """
    from app.graph_extension import failed_after_edges

    victim, t0 = _seed_graph_pair(db_session, preceder_offset=timedelta(minutes=-2))
    _skill_error(db_session, victim, agent="a1", occurred_at=t0)
    _skill_error(db_session, victim, agent="a2", occurred_at=t0)
    _crash(db_session, victim, agent="a3", occurred_at=t0)

    edges = failed_after_edges(db_session, "victim-t0111")
    assert len(edges) == 1
    assert edges[0]["skill_slug"] == "preceder-t0111"
    assert edges[0]["evidence_count"] == 2, (
        "expected exactly the 2 skill-error rows as evidence; 3 means the crash row leaked in, "
        "1 means the provenance filter is inverted"
    )
    assert edges[0]["weight"] == pytest.approx(1.0)


def test_graph_failed_after_uses_the_skill_error_constant():
    """The raw-SQL branch must bind TYPE_SKILL_ERROR, never a hand-typed literal.

    This reader cannot call the ORM predicate (its SQL is string-built against
    two possible table shapes), so the thing worth pinning is that it still draws
    the value from the model constant and gates on column existence.
    """
    from app import graph_extension

    gx_path = inspect.getsourcefile(graph_extension)
    assert gx_path is not None, "no source file for graph_extension"
    src = Path(gx_path).read_text(encoding="utf-8")
    assert "IncidentReport.TYPE_SKILL_ERROR" in src
    assert "_column_exists(db, \"incident_reports\", \"report_type\")" in src, (
        "the provenance filter must stay gated on column existence — the "
        "slug-shaped table in tests/test_graph_extension.py has no report_type"
    )
