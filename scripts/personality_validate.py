#!/usr/bin/env python3
"""personality_validate.py — deterministic personality quality-gate validator
(persona_factory_0821, mirrors scripts/bundle_validate.py PR #267 conventions).

LoopSkill hosts a second runnable catalog type alongside skills/bundles: a
*personality* (`app/models.py::Personality`) is a packaged agent identity —
system prompt + structured config — a user pulls onto their own agent
(`GET /api/personalities/{slug}`, MCP `loopskill_get_personality` /
`loopskill_search_personalities`). Today there are exactly 2, hand-authored
directly into `scripts/seed_starter_catalog.py::STARTER_PERSONALITIES` (no UI
publish flow has shipped a third). Before scaling the catalog, this is the
gate that keeps every new persona grounded: no empty fields, no copy-pasted
boilerplate description, no dangling "recommended skill" reference, no slug
collision. It works over the LIVE public API (read-only, anonymous) for
already-published personalities, AND over a local JSON candidate file for a
NOT-YET-PUBLISHED batch (the exact shape this repo needs before anyone runs
a bulk seed/publish script) — same gates, same script, so a candidate batch
can be proven clean before a single row is written.

Gates (all deterministic, zero LLM):
  G1 required-fields    every required field present AND non-empty
                         (live: slug, title, description, category,
                          system_prompt; candidate: slug, name, role,
                          target_user, member_skills, why)
  G2 honest-description  description >= MIN_DESC_LEN chars, matches no
                         placeholder/boilerplate pattern, and is not
                         byte-identical (after whitespace/case normalization)
                         to another personality's description in the same
                         batch (the copy-paste-persona signature)
  G3 skill-existence     every referenced skill slug (live: config.
                         recommended_skills / config.member_skills; candidate:
                         member_skills) resolves via GET /api/skills/{slug},
                         is public, and is not archived. Candidates additionally
                         require >= 3 member_skills (a persona with < 3 grounded
                         skills is a stub, not a role).
  G4 slug-uniqueness     slug matches the publish schema pattern
                         (^[a-z0-9][a-z0-9_-]{0,63}$) AND is unique within the
                         batch being validated

Exit contract (cron-safe, matches bundle_validate.py / connector_walk.py):
  0 = everything in the batch passed (warnings allowed)
  1 = at least one record FAILED (or the catalog itself is unreachable — never
      report OK on total failure)
  2 = usage / infra error (bad args, DNS dead, non-JSON, unreadable file)

Usage:
  # Validate the 2 live public personalities
  python scripts/personality_validate.py --all-public

  # Validate specific live slugs
  python scripts/personality_validate.py --slug research-analyst --slug focused-dev-agent

  # Validate a NOT-YET-PUBLISHED candidate batch (member_skills checked live)
  python scripts/personality_validate.py --candidates docs/personality-first-batch-candidates.json --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE = "https://app.loopskill.io"
MIN_DESC_LEN = 40
MIN_MEMBER_SKILLS = 3
_TIMEOUT = 20
_PROBE_COOLDOWN_S = 20  # anon rate-limit backoff between probes

# Phrases that flag a copy-pasted / never-written / AI-filler description.
# WARN-vs-FAIL split: these are hard FAILs (unlike bundle_validate's G5,
# which is a soft noun-overlap heuristic) because a placeholder description
# is unambiguous, not a judgment call.
_PLACEHOLDER_RE = re.compile(
    r"\b(lorem ipsum|todo|tbd|coming soon|placeholder text|fill me in|"
    r"description here|change ?me|sample description|persona description|"
    r"insert description|xxx+)\b",
    re.IGNORECASE,
)

# Same publish-time constraint as PersonalityPublishIn.slug in app/schemas.py —
# validated here too so a bad candidate slug is caught before it ever reaches
# the publish endpoint.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

LIVE_REQUIRED_FIELDS = ("slug", "title", "description", "category", "system_prompt")
CANDIDATE_REQUIRED_FIELDS = ("slug", "name", "role", "target_user", "member_skills", "why")


@dataclass
class SkillRef:
    slug: str
    ok: bool
    reason: str = ""


@dataclass
class PersonalityReport:
    slug: str
    description: str = ""
    skills: list[SkillRef] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _get(url: str) -> tuple[int, object]:
    """Transport shim — identical shape to bundle_validate.py's _get()."""
    req = urllib.request.Request(url, headers={"User-Agent": "personality-validate/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read()
            ctype = resp.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:  # noqa: BLE001 — Rationale: report ANY transport failure as code 0 shape
        return 0, f"{type(e).__name__}: {e}"
    if "json" in ctype:
        try:
            return 200, json.loads(body)
        except json.JSONDecodeError as e:
            return -1, f"invalid JSON: {e}"
    return 200, body.decode("utf-8", "replace")


def _discover_public_personalities(base: str) -> list[dict]:
    code, data = _get(f"{base}/api/personalities?limit=200")
    if code != 200 or not isinstance(data, list):
        print(f"FATAL: /api/personalities unreachable (code={code})", file=sys.stderr)
        raise SystemExit(2)
    return data


def _get_personality_detail(base: str, slug: str) -> tuple[dict | None, str | None]:
    url = f"{base}/api/personalities/{slug}"
    code, data = _get(url)
    if code == 429:
        time.sleep(_PROBE_COOLDOWN_S)
        code, data = _get(url)
    if code == 429:
        return None, "RATE-LIMITED (probe-side; not counted as failed)"
    if code != 200 or not isinstance(data, dict):
        return None, f"personality detail unreachable (code={code}: {str(data)[:120]})"
    return data, None


def _check_slug(slug: str) -> list[str]:
    if not slug or not _SLUG_RE.match(slug):
        return [f"G4: slug {slug!r} fails schema pattern ^[a-z0-9][a-z0-9_-]{{0,63}}$"]
    return []


def _check_required_fields(record: dict, required: tuple[str, ...]) -> list[str]:
    failures = []
    for name in required:
        value = record.get(name)
        ok = len(value) > 0 if isinstance(value, list) else isinstance(value, str) and value.strip() != ""
        if not ok:
            failures.append(f"G1: required field '{name}' missing or empty")
    return failures


def _check_description_quality(desc: str) -> list[str]:
    failures = []
    desc = (desc or "").strip()
    if len(desc) < MIN_DESC_LEN:
        failures.append(f"G2: description too short ({len(desc)} chars < {MIN_DESC_LEN})")
    if _PLACEHOLDER_RE.search(desc):
        failures.append("G2: description contains placeholder/boilerplate text")
    return failures


def _check_skills_exist(base: str, skills: list[str]) -> tuple[list[SkillRef], list[str], list[str]]:
    refs: list[SkillRef] = []
    failures: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for s in skills:
        if s in seen:
            failures.append(f"G3: duplicate referenced skill '{s}'")
            refs.append(SkillRef(s, False, "duplicate"))
            continue
        seen.add(s)
        url = f"{base}/api/skills/{s}"
        code, sdata = _get(url)
        if code == 429:
            time.sleep(_PROBE_COOLDOWN_S)
            code, sdata = _get(url)
        if code == 429:
            warnings.append(f"G3: referenced skill '{s}' probe rate-limited (unverified this run)")
            refs.append(SkillRef(s, True, "rate-limited"))
            continue
        if code == 404 or not isinstance(sdata, dict):
            failures.append(f"G3: referenced skill '{s}' does not resolve (404)")
            refs.append(SkillRef(s, False, "404"))
            continue
        if code != 200:
            failures.append(f"G3: referenced skill '{s}' error http {code}")
            refs.append(SkillRef(s, False, f"http {code}"))
            continue
        ok = True
        if sdata.get("is_archived"):
            failures.append(f"G3: referenced skill '{s}' is ARCHIVED")
            ok = False
        if not sdata.get("is_public", True):
            failures.append(f"G3: referenced skill '{s}' is not public")
            ok = False
        refs.append(SkillRef(s, ok))
    return refs, failures, warnings


def _duplicate_description_pass(reports: list[PersonalityReport]) -> None:
    """G2 cross-batch: byte-identical (normalized) description = FAIL both."""
    seen: dict[str, str] = {}
    for rep in reports:
        norm = " ".join((rep.description or "").split()).lower()
        if not norm:
            continue
        if norm in seen:
            other = seen[norm]
            rep.failures.append(f"G2: description identical to '{other}' (copy-pasted persona)")
            for r2 in reports:
                if r2.slug == other:
                    r2.failures.append(f"G2: description identical to '{rep.slug}' (copy-pasted persona)")
        else:
            seen[norm] = rep.slug


def _slug_uniqueness_pass(reports: list[PersonalityReport]) -> None:
    counts: dict[str, int] = {}
    for rep in reports:
        counts[rep.slug] = counts.get(rep.slug, 0) + 1
    for rep in reports:
        if counts.get(rep.slug, 0) > 1:
            rep.failures.append(f"G4: slug '{rep.slug}' appears {counts[rep.slug]}x in this batch")


def validate_live(base: str, slug: str) -> PersonalityReport:
    rep = PersonalityReport(slug=slug)
    data, err = _get_personality_detail(base, slug)
    if err or data is None:
        if (err or "").startswith("RATE-LIMITED"):
            rep.warnings.append(f"G0: {err}")
        else:
            rep.failures.append(f"G0: {err or 'no data returned'}")
        return rep
    rep.description = data.get("description") or ""
    rep.failures += _check_slug(slug)
    rep.failures += _check_required_fields(data, LIVE_REQUIRED_FIELDS)
    rep.failures += _check_description_quality(rep.description)
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    skills = config.get("recommended_skills") or config.get("member_skills") or []
    if not isinstance(skills, list) or not skills:
        rep.warnings.append("G3: no recommended_skills declared in config (not verified)")
    else:
        refs, failures, warnings = _check_skills_exist(base, skills)
        rep.skills, rep.failures, rep.warnings = refs, rep.failures + failures, rep.warnings + warnings
    return rep


def validate_candidate(base: str, record: dict) -> PersonalityReport:
    slug = record.get("slug") or "<missing-slug>"
    rep = PersonalityReport(slug=slug)
    rep.description = record.get("role") or record.get("description") or ""
    rep.failures += _check_slug(slug)
    rep.failures += _check_required_fields(record, CANDIDATE_REQUIRED_FIELDS)
    rep.failures += _check_description_quality(rep.description)
    skills = record.get("member_skills") or []
    if not isinstance(skills, list) or len(skills) < MIN_MEMBER_SKILLS:
        got = len(skills) if isinstance(skills, list) else 0
        rep.failures.append(f"G3: candidate must declare >= {MIN_MEMBER_SKILLS} member_skills (got {got})")
    if isinstance(skills, list) and skills:
        refs, failures, warnings = _check_skills_exist(base, skills)
        rep.skills, rep.failures, rep.warnings = refs, rep.failures + failures, rep.warnings + warnings
    return rep


def _print_text(reports: list[PersonalityReport]) -> None:
    for r in reports:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.slug} ({len(r.skills)} referenced skill(s))")
        for f_ in r.failures:
            print(f"    \u2717 {f_}")
        for w in r.warnings:
            print(f"    \u26a0 {w}")


def _print_json(base: str, reports: list[PersonalityReport]) -> None:
    failed = [r for r in reports if not r.passed]
    print(
        json.dumps(
            {
                "base": base,
                "checked": len(reports),
                "failed": len(failed),
                "reports": [
                    {
                        "slug": r.slug,
                        "passed": r.passed,
                        "failures": r.failures,
                        "warnings": r.warnings,
                        "skills": [{"slug": s.slug, "ok": s.ok, "reason": s.reason} for s in r.skills],
                    }
                    for r in reports
                ],
            },
            indent=2,
        )
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic personality quality-gate validator")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--slug", action="append", default=[])
    ap.add_argument("--all-public", action="store_true")
    ap.add_argument(
        "--candidates",
        default=None,
        help="path to a JSON array of not-yet-published candidate records to validate",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    reports: list[PersonalityReport] = []

    if args.candidates:
        path = Path(args.candidates)
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"FATAL: cannot read candidates file {path}: {e}", file=sys.stderr)
            return 2
        if not isinstance(records, list) or not records:
            print("FATAL: candidates file must be a non-empty JSON array", file=sys.stderr)
            return 2
        for i, rec in enumerate(records):
            if i:
                time.sleep(0.2)
            reports.append(validate_candidate(args.base, rec))
    else:
        slugs = list(args.slug)
        if args.all_public or not slugs:
            rows = _discover_public_personalities(args.base)
            slugs = [r.get("slug") for r in rows if r.get("slug")]
        if not slugs:
            print("no public personalities discovered", file=sys.stderr)
            return 2
        for i, s in enumerate(slugs):
            if i:
                time.sleep(_PROBE_COOLDOWN_S)
            reports.append(validate_live(args.base, s))

    _slug_uniqueness_pass(reports)
    _duplicate_description_pass(reports)

    if args.json:
        _print_json(args.base, reports)
    else:
        _print_text(reports)

    failed = [r for r in reports if not r.passed]
    print(f"\n{len(reports) - len(failed)}/{len(reports)} personalities passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
