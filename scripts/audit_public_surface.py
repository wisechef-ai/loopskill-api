#!/usr/bin/env python3
"""audit_public_surface.py — the publish gate, made executable.

`hub.md` §5 (the claims register) says: *nothing goes into marketing copy, a
landing page, a README or an X post unless it has a row there with a verified
date and a probe that proves it.* Until now that rule lived only in a vault
document, so nothing stopped a false claim being written back into a file this
repo publishes to the world.

This script enforces the ❌ rows — the claims that are **known false today** —
against the surfaces this repository actually serves to strangers:

  * ``README.md``                 — the GitHub front door
  * ``docs/SELF_HOST.md``         — the self-host guide
  * ``docs/recipes-skill/SKILL.md`` — served verbatim at
    ``https://app.loopskill.io/skill`` by ``app/skill_serve_routes.py``

Each rule below names the §5 row it enforces and the live evidence that makes
the claim false. A rule is deleted from here only when a probe upgrades its §5
row to ✅ — not when the copy gets softened, because **a hedge is still a
claim**.

Rules are deliberately narrow and sentence-scoped. A copy gate that fires on
adjacent-but-true language teaches writers to route around it, which is worse
than no gate; the RED proofs in
``tests/test_m4_public_surface_truth.py`` pin both halves — each rule fires on
the real reintroduction shape, and stays silent on the true sentences that live
next to it today.

Exit codes:
  0 — clean (no false claims on any audited surface)
  1 — violations found

Usage:
  python3 scripts/audit_public_surface.py             # scan from repo root
  python3 scripts/audit_public_surface.py --root DIR
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# The surfaces this repo publishes. Internal design notes under docs/design/,
# docs/deviations/ etc. are engineering history, not product copy, and are out
# of scope on purpose — widening this list is a decision, not a default.
PUBLIC_SURFACES: tuple[str, ...] = (
    "README.md",
    "docs/SELF_HOST.md",
    "docs/recipes-skill/SKILL.md",
)


@dataclass(frozen=True)
class Rule:
    """One ❌ claim from hub §5, expressed as a sentence-scoped matcher."""

    id: str
    pattern: str
    claim: str
    why: str
    # A sentence that matches `pattern` is exonerated when it matches `unless`
    # AND does not match `unless_absent`. This is how a rule stays narrow
    # without becoming a no-op: the ❌ claim is "a BUNDLE lands on a fleet
    # member", and composite LOOPS genuinely do that (converge_0208 P4 proved
    # the placement chain), so the loop wording must stay sayable — but a
    # sentence that names a bundle too has not been rescued by saying "loop"
    # somewhere in the same list.
    unless: str = ""
    unless_absent: str = ""

    @property
    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE)

    def fires_on(self, fragment: str) -> bool:
        if not self.regex.search(fragment):
            return False
        if self.unless and re.search(self.unless, fragment, re.IGNORECASE):
            if not (self.unless_absent and re.search(self.unless_absent, fragment, re.IGNORECASE)):
                return False
        return True


RULES: tuple[Rule, ...] = (
    Rule(
        id="bundle-deployment",
        # "deploy a bundle" / "bundles you deploy" — either order, close together
        # so the two words are genuinely verb-and-object rather than co-occurring
        # in unrelated clauses of a long sentence.
        pattern=r"\bdeploy\w*\b[^.\n]{0,30}?\bbundles?\b|\bbundles?\b[^.\n]{0,30}?\bdeploy\w*\b",
        claim='hub §5 ❌ "Deploy to a fleet member"',
        why=(
            "bundle_deployments = 0 and app/bundle_deployment_routes.py:326,348 "
            "returns a permanent fake {'status': 'applying'} — the bundle apply/jobs "
            "surface has no terminal state and never had one. Composite LOOPS do "
            "deploy (real placement chain, proven by converge_0208 P4); bundles "
            "do not."
        ),
        unless=r"\bloops?\b",
        unless_absent=r"\bbundles?\b|\bcookbooks?\b",
    ),
    Rule(
        id="fleet-push",
        # The same ❌ row seen from the other side: a markdown table cell drops
        # its subject ("**bundle** | A curated set you deploy + sync to a whole
        # fleet"), so the bundle noun is not in the fragment at all — the tell
        # is the *target*.
        pattern=(
            r"\b(deploy|push|ship|roll\s*out)\w*\b[^.\n]{0,40}?"
            r"\b(to|onto|across|into)\b[^.\n]{0,25}?"
            r"\b(fleet|fleets|members?|client agents?|every agent|all agents?)\b"
        ),
        claim='hub §5 ❌ "Deploy to a fleet member"',
        why=(
            "Nothing is pushed. LoopSkill is pull-based (hub §1): the control plane "
            "cannot push and cannot execute. An agent converges by polling; for "
            "bundles there is no terminal apply state at all."
        ),
        unless=r"\bloops?\b|\bplacements?\b",
        unless_absent=r"\bbundles?\b|\bcookbooks?\b",
    ),
    Rule(
        id="roi-metric",
        pattern=r"cost[ _-]per[ _-]accepted|per accepted change|\bcost per change\b",
        claim="hub §4 D-019 — the ROI metric is HIDDEN, in any form",
        why=(
            "D-019 removed cost_per_accepted_change from dashboard_routes.py and the "
            "portal fleet-map tile (#174, portal #39). It has never been corroborated "
            "server-side. Re-surfacing it in any wording re-opens the decision."
        ),
    ),
    Rule(
        id="fast-sync",
        pattern=(
            r"\b(fast|instant|instantly|immediate|immediately|real[- ]?time|realtime)\b"
            r"[^.\n]{0,30}?\bsync\w*\b"
            r"|\bsync\w*\b[^.\n]{0,30}?\b(is |are )?"
            r"(instant|instantly|immediate|immediately|real[- ]?time|realtime)\b"
        ),
        claim='hub §5 ⚠️ "Fast sync" — still false',
        why=(
            "Convergence is a 30-minute poll (docs/SELF_HOST.md step 5). Nothing is "
            "pushed and nothing is real-time. Say the interval instead of an adjective."
        ),
    ),
    Rule(
        id="loops-on-any-host",
        pattern=r"\bloops?\b[^.\n]{0,60}?\b(any|every|all)\b[^.\n]{0,25}?\b(agent|host|vendor|client)s?\b",
        claim="hub §5 — the loop path is Hermes-only",
        why=(
            "app/loop_apply.py writes the Hermes scheduler's ~/.hermes/cron/jobs.json "
            "and nothing else speaks that format. scripts/install-loop-apply.sh refuses "
            "Codex/Claude/OpenCode hosts rather than installing a cron that can never "
            "converge. The SKILL path is cross-vendor; the LOOP path is not."
        ),
    ),
    Rule(
        id="automatic-telemetry",
        pattern=(
            r"\btelemetry\b[^.\n]{0,40}?\bautomatic\w*\b"
            r"|\bautomatic\w*\b[^.\n]{0,40}?\btelemetry\b"
        ),
        claim="hub §5 — loop telemetry is NOT automatic",
        why=(
            "A loop run is recorded only if the loop's own prompt calls "
            "scripts/loopskill-emit-run.sh. Nothing else observes a fire. That is the "
            "structural reason loop_runs sat at 1 for a year (/tmp/ISSUES-p4.md)."
        ),
    ),
)

# Sentence splitter: split on a period/!/? that ends a sentence, plus on the
# markdown table-cell boundary `|`, because a table row packs several
# independent claims onto one physical line and must not leak context between
# them.
_SPLIT = re.compile(r"(?<=[.!?])\s+|\s*\|\s*|\n")


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: Rule
    text: str


def scan_text(text: str, rel_path: str) -> list[Violation]:
    """Scan one document, reporting at most one violation per (line, rule)."""
    out: list[Violation] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for fragment in _SPLIT.split(line):
            fragment = fragment.strip()
            if not fragment:
                continue
            for rule in RULES:
                if rule.fires_on(fragment):
                    out.append(Violation(rel_path, lineno, rule, fragment))
                    break
    return out


def scan(repo_root: Path, surfaces: tuple[str, ...] = PUBLIC_SURFACES) -> list[Violation]:
    """Scan every declared public surface. A missing surface is a violation."""
    out: list[Violation] = []
    for rel in surfaces:
        path = repo_root / rel
        if not path.is_file():
            # A surface that vanished cannot be audited, and silence must never
            # look like health.
            out.append(
                Violation(
                    rel,
                    0,
                    Rule(
                        id="missing-surface",
                        pattern="",
                        claim="declared public surface is missing",
                        why=(
                            "PUBLIC_SURFACES names a file this repo publishes. If it "
                            "moved, move the entry; do not let it drop out of the audit."
                        ),
                    ),
                    "",
                )
            )
            continue
        out.extend(scan_text(path.read_text(encoding="utf-8"), rel))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=None, help="repo root (default: this script's parent)")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    violations = scan(root)

    if not violations:
        print(f"audit_public_surface: OK — {len(PUBLIC_SURFACES)} surface(s) clean")
        return 0

    print(f"audit_public_surface: {len(violations)} false claim(s) on a public surface\n")
    for v in violations:
        print(f"  {v.path}:{v.line}  [{v.rule.id}]")
        if v.text:
            print(f"      {v.text}")
        print(f"      violates: {v.rule.claim}")
        print(f"      why:      {v.rule.why}\n")
    print("Delete the claim. Do not soften it — a hedge is still a claim.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
