"""M4 (autopilot_0308) — the truth pass, made a standing gate.

hub §5 binds every public claim to a probe and a verified date. Until M4 that
rule was enforced by a human reading a vault page, which is exactly the kind of
discipline that decays silently — the same failure shape as the 27-day
convergence outage `converge_0208` fixed.

Four contracts are pinned here, each one a defect that was live on
2026-08-03 and each one re-provable:

1.  **No ❌ claim on a repo-owned public surface.** ``scripts/audit_public_surface.py``
    enforces the false rows of hub §5 against README.md, docs/SELF_HOST.md and
    docs/recipes-skill/SKILL.md (the last is served verbatim at
    ``https://app.loopskill.io/skill``). Both halves are pinned: each rule fires
    on the real reintroduction shape, AND stays silent on the true sentence that
    lives next to it — a copy gate that cries wolf gets routed around.

2.  **docs/SELF_HOST.md is runnable as written.** Step 1 exported
    ``DATABASE_URL``, but ``Settings`` carries ``env_prefix="WR_"`` and
    ``alembic/env.py`` reads ``WR_DATABASE_URL`` — so a stranger following the
    guide verbatim ran ``alembic upgrade head`` against the hardcoded
    ``postgresql://wisechef@localhost/wiserecipes`` default in ``alembic.ini``.
    The guide's first command silently targeted the wrong database.

3.  **The two loop limitations are stated wherever loops are documented.**
    Telemetry exists only if the loop's own prompt calls
    ``loopskill-emit-run.sh``, and the loop path is Hermes-only. Both are the
    reason ``loop_runs`` sat at 1 for a year; a doc that omits either is
    selling a loop that reports nothing.

4.  **Counts in the README match the code they describe.** The README claimed 9
    starter loops; ``scripts/seed_starter_catalog.py`` seeds 10. A number
    nobody re-derives is a number that drifts.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_PATH = REPO_ROOT / "scripts" / "audit_public_surface.py"
SELF_HOST = REPO_ROOT / "docs" / "SELF_HOST.md"
README = REPO_ROOT / "README.md"
SEED = REPO_ROOT / "scripts" / "seed_starter_catalog.py"


def _load_audit():
    spec = importlib.util.spec_from_file_location("audit_public_surface", AUDIT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_public_surface"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit():
    return _load_audit()


# ── 1. the ❌ claims ────────────────────────────────────────────────────────


class TestLiveSurfacesCarryNoFalseClaim:
    def test_every_public_surface_is_clean(self, audit):
        violations = audit.scan(REPO_ROOT)
        assert violations == [], "false claim(s) on a published surface:\n" + "\n".join(
            f"  {v.path}:{v.line} [{v.rule.id}] {v.text}" for v in violations
        )

    def test_every_declared_surface_exists(self, audit):
        """A surface that moved must move in the audit too, not drop out of it."""
        for rel in audit.PUBLIC_SURFACES:
            assert (REPO_ROOT / rel).is_file(), f"declared public surface missing: {rel}"

    def test_missing_surface_is_a_violation_not_a_pass(self, audit):
        """Silence must never look like health."""
        violations = audit.scan(REPO_ROOT, surfaces=("docs/NO_SUCH_SURFACE.md",))
        assert [v.rule.id for v in violations] == ["missing-surface"]


class TestEachRuleFiresOnReintroduction:
    """The gate must not be a no-op. One synthetic reintroduction per rule."""

    @pytest.mark.parametrize(
        ("rule_id", "sentence"),
        [
            ("bundle-deployment", "Compose a bundle and deploy the bundle in one pass."),
            ("fleet-push", "Push your curated set to a whole fleet at once."),
            ("roi-metric", "Your agents report a cost per accepted change of $0.42."),
            ("fast-sync", "Fast sync keeps every machine current."),
            ("loops-on-any-host", "Your loops run on any agent you own."),
            ("automatic-telemetry", "Loop telemetry is collected automatically."),
        ],
    )
    def test_rule_fires(self, audit, rule_id, sentence):
        hits = audit.scan_text(sentence, "synthetic.md")
        assert [h.rule.id for h in hits] == [rule_id], (
            f"rule {rule_id!r} did not fire on its own reintroduction shape: {sentence!r} "
            f"(got {[h.rule.id for h in hits]})"
        )


class TestEachRuleStaysSilentOnTheTruth:
    """The true sentence next door must stay sayable, or writers route around."""

    @pytest.mark.parametrize(
        "sentence",
        [
            # Composite loops genuinely deploy onto a member — converge_0208 P4
            # proved the placement chain end to end.
            "Deploy a composite loop to a fleet member and it runs on schedule.",
            "Place the loop on a member; the next apply materializes its cron.",
            # The interval, stated instead of an adjective.
            "The agent syncs on a 30-minute poll.",
            # The SKILL path really is cross-vendor.
            "Skills install the same way into any MCP-capable agent.",
            # Telemetry, described honestly. The first version of this gate
            # flagged the exact disclosure M4 requires — a rule that punishes
            # the honest sentence trains people to write the dishonest one.
            "Telemetry lands only when the loop's prompt calls the emitter.",
            "Telemetry is NOT automatic.",
            "Loop telemetry is never collected automatically.",
            "Sync is not instant — the agent polls every 30 minutes.",
            "Loops do not run on every agent; the scheduled path is Hermes-only.",
            # A tailored fork attaching into a bundle is a real, working path.
            "Deploy a tailored fork's latest version into one of your bundles.",
        ],
    )
    def test_true_sentence_is_not_flagged(self, audit, sentence):
        hits = audit.scan_text(sentence, "synthetic.md")
        assert hits == [], (
            f"a TRUE sentence was flagged by {[h.rule.id for h in hits]}: {sentence!r}"
        )


# ── 2. SELF_HOST.md is runnable as written ─────────────────────────────────

# Env vars a reader is told to export that are NOT pydantic Settings fields and
# so are correctly un-prefixed. Each is read by name somewhere in the tree.
_NON_SETTINGS_ENV = {
    "RECIPES_API_KEY",  # agent-side CLI key — app/reconcile_cli.py
    "LOOPSKILL_MEMBER_KEY",  # collector key — scripts/loopskill-collect-reports.py
}

_EXPORT_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{2,})=", re.MULTILINE)


def _settings_field_names() -> set[str]:
    from app.config import Settings

    return set(Settings.model_fields)


class TestSelfHostGuideIsRunnable:
    def test_every_exported_setting_carries_the_wr_prefix(self):
        """A Settings field exported without WR_ is read by nothing at all.

        ``Settings.model_config`` sets ``env_prefix="WR_"`` and
        ``alembic/env.py`` reads ``WR_DATABASE_URL``. Exporting the bare name
        does not fail loudly — it falls through to the default in
        ``alembic.ini``, so the stranger's very first command migrates a
        database they never named.
        """
        fields = _settings_field_names()
        text = SELF_HOST.read_text(encoding="utf-8")
        bare = sorted(
            {
                name
                for name in _EXPORT_RE.findall(text)
                if name in fields and name not in _NON_SETTINGS_ENV
            }
        )
        assert bare == [], (
            "docs/SELF_HOST.md exports Settings field(s) without the WR_ prefix, so the "
            f"app and alembic never see them: {bare}"
        )

    def test_settings_a_documented_step_depends_on_are_exported(self):
        """A step that cannot be completed is worse than a step that is missing.

        Step 2 mints the owner key from a signed-in session, and the only sign-in
        route is GitHub OAuth — which needs ``WR_GITHUB_CLIENT_ID`` and
        ``WR_GITHUB_CLIENT_SECRET``. Neither appeared anywhere in the guide, so a
        stranger following it verbatim reached step 2 and stopped.
        """
        text = SELF_HOST.read_text(encoding="utf-8")
        if "auth/github" not in text:
            pytest.skip("the guide no longer routes sign-in through GitHub OAuth")
        exported = set(_EXPORT_RE.findall(text))
        missing = sorted(
            {"WR_GITHUB_CLIENT_ID", "WR_GITHUB_CLIENT_SECRET"} - exported
        )
        assert missing == [], (
            "docs/SELF_HOST.md tells the reader to sign in via GitHub OAuth but never "
            f"has them set: {missing}"
        )

    def test_the_guide_says_where_the_api_key_comes_from(self):
        """Every step that needs a key must name which key and its source."""
        text = SELF_HOST.read_text(encoding="utf-8")
        assert "WR_API_KEY" in text, "the guide exports WR_API_KEY but never says what to do with it"
        # The first curl that uses a key must be preceded by a sentence tying
        # that key back to an env var the reader has already set.
        first_key_use = text.index("x-api-key:")
        preamble = text[:first_key_use]
        assert "WR_API_KEY" in preamble, (
            "docs/SELF_HOST.md uses `x-api-key:` before telling the reader which key "
            "that is or where it comes from"
        )

    def test_the_guide_creates_the_bundle_it_later_subscribes_to(self):
        """`--cookbook <bundle-uuid>` is unobtainable if no step makes a bundle.

        The sprint's one-sentence test is *a stranger enrolls an agent, receives
        a bundle, and watches a loop run*. A guide that starts from a bundle
        UUID the reader has no way to get fails that test at step 2.
        """
        text = SELF_HOST.read_text(encoding="utf-8")
        assert "/api/bundles" in text and re.search(
            r"POST\s+https?://[^\s]*?/api/bundles\b|curl -X POST[^\n]*?/api/bundles\b", text
        ), (
            "docs/SELF_HOST.md subscribes a fleet to `<bundle-uuid>` but never shows the "
            "reader how to create a bundle and get that UUID"
        )


# ── 3. the loop limitations ────────────────────────────────────────────────


class TestLoopLimitationsAreStated:
    @pytest.mark.parametrize("doc", [SELF_HOST, README])
    def test_telemetry_limitation_is_stated(self, doc):
        text = doc.read_text(encoding="utf-8")
        if "loop" not in text.lower():
            pytest.skip(f"{doc.name} does not discuss loops")
        assert "loopskill-emit-run.sh" in text, (
            f"{doc.name} discusses loops but never says telemetry exists ONLY when the "
            "loop's own prompt calls loopskill-emit-run.sh — the structural reason "
            "loop_runs sat at 1 for a year"
        )

    @pytest.mark.parametrize("doc", [SELF_HOST, README])
    def test_hermes_only_limitation_is_stated(self, doc):
        text = doc.read_text(encoding="utf-8")
        if "loop" not in text.lower():
            pytest.skip(f"{doc.name} does not discuss loops")
        assert re.search(r"Hermes[^\n]{0,40}only|only[^\n]{0,40}Hermes", text, re.IGNORECASE), (
            f"{doc.name} discusses loops but never states that the loop path (cron "
            "materialization) is Hermes-only — app/loop_apply.py writes "
            "~/.hermes/cron/jobs.json and nothing else speaks that format"
        )


# ── 4. counts match the code ───────────────────────────────────────────────


class TestReadmeCountsMatchTheCode:
    def test_starter_loop_count(self):
        seeded = len(re.findall(r'^\s+"slug": "[a-z0-9-]+-loop",\s*$', SEED.read_text("utf-8"), re.M))
        assert seeded > 0, "could not count seeded loops — the regex drifted from the seed file"
        claimed = re.search(r"\*\*(\d+) vetted loops\*\*", README.read_text("utf-8"))
        assert claimed is not None, "README no longer states a starter-loop count"
        assert int(claimed.group(1)) == seeded, (
            f"README claims {claimed.group(1)} vetted loops; "
            f"scripts/seed_starter_catalog.py seeds {seeded}"
        )
