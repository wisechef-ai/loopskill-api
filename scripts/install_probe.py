#!/usr/bin/env python3
"""install_probe.py — deterministic install-readiness probe (all hosted artifacts).

Adam, 2026-08-21: "not all artifacts are ready to be installed" on LoopSkill.
This is the gate that answers the question directly, per-artifact, by
actually WALKING the real install path a stranger uses — not by trusting a
catalog row or a 200 from an endpoint that returns a signed URL to nothing.

Mirrors ``scripts/bundle_validate.py`` / ``scripts/personality_validate.py``
conventions exactly: deterministic gates (zero LLM), the same ``_get()``
transport shim, the same 429-aware backoff-then-WARN discipline, the same
exit contract, and the same ``--json`` machine-readable output shape.

Three artifact kinds are hosted; each has a DIFFERENT real install path, so
each gets its own gate ladder. A catalog row existing is not installability —
that was exactly the failure class bundle_validate.py (phantom bundles) and
personality_validate.py (dangling skill refs) were built to catch. This
script closes the last gap: does the SKILL TARBALL ITSELF actually resolve,
download, and unpack into something an agent can run.

## Skills (the full 6-gate install ladder — the "real install path"):
  G1 catalog-listing   slug appears in GET /api/skills/search (page_size=100)
  G2 detail-200        GET /api/skills/{slug} == 200
  G3 install-resolve   GET /api/skills/install?slug={slug} == 200 (anonymous;
                       this is the exact portal "install command" + MCP
                       loopskill_install path for a free-tier skill — a
                       non-free skill that still needs a real key is
                       reported as SKIP, not FAIL, since anon 401 there is
                       the CORRECT gate, not a defect)
  G4 tarball-fetch     the signed tarball_url resolves 200, body is
                       non-empty, and is a valid gzip stream
  G5 manifest-parses   SKILL.md is present in the tarball AND (if present)
                       skill.toml parses as valid TOML
  G6 refs-exist        every skill slug the SKILL.md frontmatter's
                       `related_skills:` list declares resolves via
                       GET /api/skills/{slug} in the SAME catalog listing
                       (a dangling related-skill ref is a broken promise,
                       same class fdeloop_0808 built audit_skill_references
                       for — this is that gate replayed over the tarball
                       body instead of the DB readme, so it catches drift
                       between the DB and the SHIPPED artifact too)

## Personalities (no tarball — the real install path is the detail fetch
## itself; `loopskill pull personality <slug>` IS `GET /api/personalities/
## {slug}`):
  G1 catalog-listing   slug appears in GET /api/personalities
  G2 detail-200        GET /api/personalities/{slug} == 200
  G3 pull-resolve      system_prompt is present and non-empty (the actual
                       payload `pull` hands the caller — an empty prompt is
                       an uninstallable personality even though the row and
                       the 200 both look fine)
  G4 refs-exist        every skill slug in config.recommended_skills /
                       config.member_skills resolves via GET /api/skills/{s}

## Bundles (the real install path is the public well-known index — the
## EXACT surface a portal "install" button / bundle-aware CLI consumes):
  G1 catalog-listing   slug appears in GET /api/bundles/discover
  G2 install-index-200 GET /api/bundles/public/{slug}/.well-known/skills/
                       index.json == 200
  G3 members-nonzero   the install index lists >= 1 member
  G4 refs-exist        every LOCAL member slug resolves via GET /api/skills/
                       {slug}; federated (`ext:`) members resolve via
                       GET /api/federation/filter (same resolution logic as
                       bundle_validate.py, replayed here for the install-path
                       lens rather than the phantom-membership lens)

Exit contract (cron-safe, matches bundle_validate.py / personality_validate.py):
  0 = every artifact's real install path resolved end-to-end (warnings allowed)
  1 = at least one artifact FAILED (or a catalog itself is unreachable — never
      report OK on total failure)
  2 = usage / infra error (bad args, DNS dead, non-JSON)

Usage:
  python scripts/install_probe.py                       # probe everything
  python scripts/install_probe.py --skills-only --json
  python scripts/install_probe.py --slug super-memory --slug loopskill
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_BASE = "https://app.loopskill.io"
_TIMEOUT = 20
_DOWNLOAD_TIMEOUT = 30
_PROBE_COOLDOWN_S = 20  # anon rate-limit backoff between probes (matches sibling scripts)

# YAML frontmatter `related_skills:` block extractor — SKILL.md is YAML
# frontmatter + markdown body, not a full YAML doc, so a light regex over the
# fenced `---...---` header is sufficient and avoids a new YAML dependency.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_RELATED_LIST_RE = re.compile(r"related_skills:\s*\n((?:\s*-\s*\S+\s*\n?)+)")
_LIST_ITEM_RE = re.compile(r"-\s*([a-z0-9][a-z0-9_-]*)")


@dataclass
class RefCheck:
    slug: str
    ok: bool
    reason: str = ""


@dataclass
class ArtifactReport:
    kind: str  # "skill" | "personality" | "bundle"
    slug: str
    refs: list[RefCheck] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: str = ""  # non-empty => this artifact was intentionally not fully walked

    @property
    def passed(self) -> bool:
        return not self.failures


def _get(url: str) -> tuple[int, object]:
    """Transport shim — identical shape to bundle_validate.py / personality_validate.py."""
    req = urllib.request.Request(url, headers={"User-Agent": "install-probe/1.0"})
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


def _get_bytes(url: str) -> tuple[int, bytes]:
    """Byte-preserving GET for tarball downloads (no JSON decode / no truncation)."""
    req = urllib.request.Request(url, headers={"User-Agent": "install-probe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001 — Rationale: report ANY transport failure as code 0 shape
        return 0, str(e).encode("utf-8", "replace")


def _get_with_backoff(url: str) -> tuple[int, object]:
    code, data = _get(url)
    if code == 429:
        time.sleep(_PROBE_COOLDOWN_S)
        code, data = _get(url)
    return code, data


def _get_bytes_with_backoff(url: str) -> tuple[int, bytes]:
    code, data = _get_bytes(url)
    if code == 429:
        time.sleep(_PROBE_COOLDOWN_S)
        code, data = _get_bytes(url)
    return code, data


# ── Skills ───────────────────────────────────────────────────────────────


def _discover_skills(base: str) -> list[dict]:
    """Page through the full public catalog (100/page max; server-enforced).

    total_skills is 57 today so this is a single page in practice, but paging
    for real (rather than trusting one page) means the gate stays correct as
    the catalog grows past the page cap without a silent truncation. Stops
    when a page returns fewer than the requested page_size (the standard
    "last page" signal) rather than trusting a `total` field that may be
    absent — this also makes the loop safe against a test double that
    always echoes the same fixed result set with no `total` key.
    """
    page_size = 100
    out: list[dict] = []
    page = 1
    while True:
        code, data = _get(f"{base}/api/skills/search?page_size={page_size}&page={page}")
        if code != 200 or not isinstance(data, dict):
            print(f"FATAL: /api/skills/search unreachable (code={code})", file=sys.stderr)
            raise SystemExit(2)
        results = data.get("results") or []
        out.extend(results)
        if len(results) < page_size:
            break
        page += 1
    return out


def _extract_related_skills(skill_md: str) -> list[str]:
    """Pull the `related_skills:` YAML list out of SKILL.md frontmatter, if any."""
    fm = _FRONTMATTER_RE.match(skill_md)
    if not fm:
        return []
    m = _RELATED_LIST_RE.search(fm.group(1))
    if not m:
        return []
    return _LIST_ITEM_RE.findall(m.group(1))


def _validate_skill(base: str, slug: str, catalog_slugs: set[str]) -> ArtifactReport:
    rep = ArtifactReport(kind="skill", slug=slug)

    if slug not in catalog_slugs:
        rep.failures.append(f"G1: '{slug}' not present in GET /api/skills/search listing")
        return rep

    code, detail = _get_with_backoff(f"{base}/api/skills/{slug}")
    if code == 429:
        rep.warnings.append("G2: detail probe rate-limited (unverified this run)")
        return rep
    if code != 200 or not isinstance(detail, dict):
        rep.failures.append(f"G2: detail endpoint returned {code} (expected 200)")
        return rep

    tier = (detail.get("tier") or "").lower()
    is_public = bool(detail.get("is_public", True))
    if not is_public:
        rep.failures.append("G2: skill is not public but appears in the public catalog listing")
        return rep

    code, install = _get_with_backoff(f"{base}/api/skills/install?slug={slug}")
    if code == 429:
        rep.warnings.append("G3: install-resolve probe rate-limited (unverified this run)")
        return rep
    if code == 401 and tier and tier != "free":
        # Correct behaviour: a genuinely non-free tier requires a real key.
        # This is the gate working as designed, not a defect — record it as
        # an intentionally-not-walked artifact rather than a failure. An
        # UNSET tier (empty string) is NOT this case — a skill with no tier
        # at all should still install anonymously (it reads as "free" on the
        # public catalog), so a 401 there is a real defect, not a gate.
        rep.skipped = f"G3: tier='{tier}' requires auth for anon install (expected 401)"
        return rep
    if code != 200 or not isinstance(install, dict):
        if tier == "free" or tier == "":
            reason = (
                f"G3: install endpoint returned {code} for a free/uncategorized-tier skill (expected 200)"
            )
            if tier == "":
                reason += (
                    " — tier is unset/None, so this skill CANNOT install anonymously despite appearing free"
                )
            rep.failures.append(reason)
        else:
            rep.warnings.append(f"G3: install endpoint returned {code} (tier={tier!r})")
        return rep

    tarball_url = install.get("tarball_url")
    if not tarball_url:
        rep.failures.append("G3: install response has no tarball_url")
        return rep

    code, body = _get_bytes_with_backoff(tarball_url)
    if code == 429:
        rep.warnings.append("G4: tarball fetch rate-limited (unverified this run)")
        return rep
    if code != 200:
        rep.failures.append(f"G4: tarball download returned {code}")
        return rep
    if not body:
        rep.failures.append("G4: tarball download returned an EMPTY body")
        return rep
    # Rationale: gzip.BadGzipFile / tarfile errors both indicate a corrupt
    # artifact; a valid-gzip check plus tarfile.open (which requires valid
    # gzip framing under "r:gz") together satisfy the "valid gzip" gate.
    try:
        gzip.decompress(body)
    except gzip.BadGzipFile as e:
        rep.failures.append(f"G4: tarball is not valid gzip: {e}")
        return rep
    try:
        tf = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
        names = tf.getnames()
    except (tarfile.TarError, EOFError, OSError) as e:
        rep.failures.append(f"G4: tarball is not a valid gzip/tar stream: {e}")
        return rep

    if "SKILL.md" not in names:
        rep.failures.append(f"G5: no SKILL.md in tarball (found: {names[:8]})")
        return rep

    skill_md_member = tf.extractfile("SKILL.md")
    if skill_md_member is None:
        rep.failures.append("G5: SKILL.md entry is not a regular file (directory/symlink?)")
        return rep
    skill_md = skill_md_member.read().decode("utf-8", "replace")

    if "skill.toml" in names:
        toml_member = tf.extractfile("skill.toml")
        toml_bytes = toml_member.read() if toml_member is not None else b""
        try:
            tomllib.loads(toml_bytes.decode("utf-8", "replace"))
        except tomllib.TOMLDecodeError as e:
            rep.failures.append(f"G5: skill.toml does not parse: {e}")
            return rep

    related = _extract_related_skills(skill_md)
    for r in related:
        if r == slug:
            continue  # self-reference is prose, not navigation (matches skill_refs.py convention)
        ok = r in catalog_slugs
        rep.refs.append(RefCheck(r, ok, "" if ok else "not in public catalog"))
        if not ok:
            rep.failures.append(f"G6: related_skills entry '{r}' does not resolve in the catalog")

    return rep


# ── Personalities ────────────────────────────────────────────────────────


def _discover_personalities(base: str) -> list[dict]:
    code, data = _get(f"{base}/api/personalities?limit=200")
    if code != 200 or not isinstance(data, list):
        print(f"FATAL: /api/personalities unreachable (code={code})", file=sys.stderr)
        raise SystemExit(2)
    return data


def _validate_personality(
    base: str, slug: str, catalog_slugs: set[str], skill_slugs: set[str]
) -> ArtifactReport:
    rep = ArtifactReport(kind="personality", slug=slug)

    if slug not in catalog_slugs:
        rep.failures.append(f"G1: '{slug}' not present in GET /api/personalities listing")
        return rep

    code, detail = _get_with_backoff(f"{base}/api/personalities/{slug}")
    if code == 429:
        rep.warnings.append("G2: detail probe rate-limited (unverified this run)")
        return rep
    if code != 200 or not isinstance(detail, dict):
        rep.failures.append(f"G2: detail endpoint returned {code} (expected 200)")
        return rep

    system_prompt = (detail.get("system_prompt") or "").strip()
    if not system_prompt:
        rep.failures.append("G3: system_prompt is empty — `pull` would install an empty persona")
        return rep

    config = detail.get("config") if isinstance(detail.get("config"), dict) else {}
    refs = config.get("recommended_skills") or config.get("member_skills") or []
    if not isinstance(refs, list):
        refs = []
    for r in refs:
        ok = r in skill_slugs
        if not ok:
            code2, sdata = _get_with_backoff(f"{base}/api/skills/{r}")
            if code2 == 429:
                rep.warnings.append(f"G4: referenced skill '{r}' probe rate-limited (unverified this run)")
                rep.refs.append(RefCheck(r, True, "rate-limited"))
                continue
            ok = code2 == 200 and isinstance(sdata, dict)
        rep.refs.append(RefCheck(r, ok, "" if ok else "does not resolve"))
        if not ok:
            rep.failures.append(f"G4: referenced skill '{r}' does not resolve")

    return rep


# ── Bundles ──────────────────────────────────────────────────────────────


def _discover_bundles(base: str) -> list[dict]:
    code, data = _get(f"{base}/api/bundles/discover")
    if code != 200 or not isinstance(data, dict):
        print(f"FATAL: /api/bundles/discover unreachable (code={code})", file=sys.stderr)
        raise SystemExit(2)
    return data.get("bundles") or data.get("results") or data.get("cookbooks") or []


def _validate_bundle(base: str, slug: str, catalog_slugs: set[str], bundle_slugs: set[str]) -> ArtifactReport:
    rep = ArtifactReport(kind="bundle", slug=slug)

    if slug not in bundle_slugs:
        rep.failures.append(f"G1: '{slug}' not present in GET /api/bundles/discover listing")
        return rep

    url = f"{base}/api/bundles/public/{slug}/.well-known/skills/index.json"
    code, data = _get_with_backoff(url)
    if code == 429:
        rep.warnings.append("G2: install-index probe rate-limited (unverified this run)")
        return rep
    if code != 200 or not isinstance(data, dict):
        rep.failures.append(f"G2: install index returned {code} (expected 200)")
        return rep

    members = []
    for entry in data.get("skills") or []:
        m = entry.get("slug") or entry.get("skill_slug") or entry.get("name") or entry.get("dir_name")
        if m:
            members.append(m)

    if not members:
        rep.failures.append("G3: install index lists ZERO members")
        return rep

    for m in members:
        if m.startswith("ext:"):
            parts = m.split(":")
            tail = parts[-1] if len(parts) >= 3 else (parts[1] if len(parts) == 2 else m)
            candidates = [tail.split("--")[-1], tail.replace("--", "-"), tail]
            found = False
            for q in dict.fromkeys(candidates):
                code2, fdata = _get_with_backoff(f"{base}/api/federation/filter?q={q}&limit=5")
                if code2 == 200 and isinstance(fdata, dict):
                    normalized = tail.replace("--", "-")
                    want = f"{parts[1]}-{normalized}" if len(parts) >= 3 else normalized
                    for row in fdata.get("results") or []:
                        if (row.get("slug") or "") == want or (row.get("federated_slug") or "") == want:
                            found = True
                            break
                if found:
                    break
            rep.refs.append(RefCheck(m, found, "" if found else "federation 0 hits"))
            if not found:
                rep.failures.append(f"G4: federated member '{m}' does not resolve in the federation index")
            continue
        ok = m in catalog_slugs
        if not ok:
            code2, sdata = _get_with_backoff(f"{base}/api/skills/{m}")
            ok = code2 == 200 and isinstance(sdata, dict)
        rep.refs.append(RefCheck(m, ok, "" if ok else "does not resolve"))
        if not ok:
            rep.failures.append(f"G4: member '{m}' does not resolve")

    return rep


def _print_text(reports: list[ArtifactReport]) -> None:
    for r in reports:
        if r.skipped:
            status = "SKIP"
        else:
            status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.kind}:{r.slug}")
        if r.skipped:
            print(f"    \u21b3 {r.skipped}")
        for f_ in r.failures:
            print(f"    \u2717 {f_}")
        for w in r.warnings:
            print(f"    \u26a0 {w}")


def _print_json(base: str, reports: list[ArtifactReport]) -> None:
    failed = [r for r in reports if not r.passed and not r.skipped]
    skipped = [r for r in reports if r.skipped]
    print(
        json.dumps(
            {
                "base": base,
                "checked": len(reports),
                "failed": len(failed),
                "skipped": len(skipped),
                "reports": [
                    {
                        "kind": r.kind,
                        "slug": r.slug,
                        "passed": r.passed,
                        "skipped": r.skipped,
                        "failures": r.failures,
                        "warnings": r.warnings,
                        "refs": [{"slug": ref.slug, "ok": ref.ok, "reason": ref.reason} for ref in r.refs],
                    }
                    for r in reports
                ],
            },
            indent=2,
        )
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic install-readiness probe (all hosted artifacts)")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--slug", action="append", default=[], help="restrict to specific slug(s), any kind")
    ap.add_argument("--skills-only", action="store_true")
    ap.add_argument("--personalities-only", action="store_true")
    ap.add_argument("--bundles-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    only_one = args.skills_only or args.personalities_only or args.bundles_only
    do_skills = args.skills_only or not only_one
    do_personalities = args.personalities_only or not only_one
    do_bundles = args.bundles_only or not only_one

    reports: list[ArtifactReport] = []

    skill_rows = _discover_skills(args.base)
    catalog_slugs = {r["slug"] for r in skill_rows if r.get("slug")}

    if do_skills:
        target_slugs = [s for s in args.slug if s in catalog_slugs] if args.slug else sorted(catalog_slugs)
        for i, slug in enumerate(target_slugs):
            if i:
                time.sleep(0.3)
            reports.append(_validate_skill(args.base, slug, catalog_slugs))

    if do_personalities:
        p_rows = _discover_personalities(args.base)
        p_slugs = {r["slug"] for r in p_rows if r.get("slug")}
        target_slugs = [s for s in args.slug if s in p_slugs] if args.slug else sorted(p_slugs)
        for i, slug in enumerate(target_slugs):
            if i:
                time.sleep(0.3)
            reports.append(_validate_personality(args.base, slug, p_slugs, catalog_slugs))

    if do_bundles:
        b_rows = _discover_bundles(args.base)
        b_slugs = {r["slug"] for r in b_rows if r.get("slug")}
        target_slugs = [s for s in args.slug if s in b_slugs] if args.slug else sorted(b_slugs)
        for i, slug in enumerate(target_slugs):
            if i:
                time.sleep(_PROBE_COOLDOWN_S if i else 0)
            reports.append(_validate_bundle(args.base, slug, catalog_slugs, b_slugs))

    if not reports:
        print("no artifacts discovered to probe", file=sys.stderr)
        return 2

    if args.json:
        _print_json(args.base, reports)
    else:
        _print_text(reports)

    failed = [r for r in reports if not r.passed and not r.skipped]
    skipped = [r for r in reports if r.skipped]
    print(
        f"\n{len(reports) - len(failed) - len(skipped)}/{len(reports)} artifacts installable "
        f"({len(skipped)} correctly-gated skip(s), {len(failed)} FAIL(s))"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
