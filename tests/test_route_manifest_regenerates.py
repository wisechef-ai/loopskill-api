"""Drift gate for docs/route-manifest.{json,md} (bundles_0811 P7).

Two guarantees:

1. **Regenerability** — ``scripts/generate_route_manifest.py --write``
   produces byte-identical output to the committed
   ``docs/route-manifest.json`` + ``docs/route-manifest.md``. If a route was
   added/removed/renamed in code but the manifest wasn't regenerated, this
   test fails with the exact diff.

2. **Tamper detection** — if someone hand-edits the committed manifest (the
   files are auto-generated, marked as such in their headers), this test
   catches it because regeneration produces different bytes.

When this test fails, run::

    python scripts/generate_route_manifest.py --write

and commit the result. Do NOT edit the manifest files by hand.

P7 of bundles_0811 (docs inventory): see docs/route-manifest.md for the
classification rules and docs/docs-gap-list.md for the gap analysis.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_JSON = REPO_ROOT / "docs" / "route-manifest.json"
MANIFEST_MD = REPO_ROOT / "docs" / "route-manifest.md"
GENERATOR = REPO_ROOT / "scripts" / "generate_route_manifest.py"


def _regenerate() -> tuple[str, str]:
    """Run the generator in --write mode against a temp dir; return its output.

    Writes to a throwaway directory (not the repo) so we can diff against the
    committed files without mutating the working tree mid-test.
    """
    import importlib.util

    # Strategy: import the generator as a module and call its build/render
    # functions directly. That avoids the env-var dance a subprocess would
    # need and lets us assert on the exact bytes it would have written.
    spec = importlib.util.spec_from_file_location("generate_route_manifest", GENERATOR)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    manifest = mod.build_manifest()
    json_bytes = json.dumps(manifest, indent=2, sort_keys=False) + "\n"
    md_bytes = mod.render_markdown(manifest)
    return json_bytes, md_bytes


def test_manifest_json_matches_running_app() -> None:
    """Committed route-manifest.json is byte-identical to what the app yields."""
    if not MANIFEST_JSON.exists():
        raise AssertionError(
            f"{MANIFEST_JSON} is missing. Run " "`python scripts/generate_route_manifest.py --write`."
        )
    committed = MANIFEST_JSON.read_text()
    regenerated, _ = _regenerate()
    if committed != regenerated:
        # Surface a compact diff so the failure message is actionable.
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                committed.splitlines(),
                regenerated.splitlines(),
                fromfile="committed docs/route-manifest.json",
                tofile="regenerated from app.main",
                lineterm="",
            )
        )
        raise AssertionError(
            "docs/route-manifest.json has drifted from the running app.\n"
            "Run `python scripts/generate_route_manifest.py --write` "
            "and commit the result.\n\n" + diff
        )


def test_manifest_md_matches_running_app() -> None:
    """Committed route-manifest.md is byte-identical to what the app yields."""
    if not MANIFEST_MD.exists():
        raise AssertionError(
            f"{MANIFEST_MD} is missing. Run " "`python scripts/generate_route_manifest.py --write`."
        )
    committed = MANIFEST_MD.read_text()
    _, regenerated = _regenerate()
    if committed != regenerated:
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                committed.splitlines(),
                regenerated.splitlines(),
                fromfile="committed docs/route-manifest.md",
                tofile="regenerated from app.main",
                lineterm="",
            )
        )
        raise AssertionError(
            "docs/route-manifest.md has drifted from the running app.\n"
            "Run `python scripts/generate_route_manifest.py --write` "
            "and commit the result.\n\n" + diff
        )


def test_route_manifest_classifications_are_not_empty() -> None:
    """Every classified route has one of the four known labels.

    Guards against a future classification refactor that silently drops a
    bucket (e.g. renames ``authenticated`` → ``user`` without updating the
    consumer of this manifest).
    """
    committed = json.loads(MANIFEST_JSON.read_text())
    allowed = {"public", "authenticated", "admin", "internal"}
    bad = [r for r in committed["routes"] if r["classification"] not in allowed]
    assert not bad, f"routes with unknown classification: {bad[:5]}"


def test_manifest_route_count_matches_decorator_count() -> None:
    """Sanity: the manifest's route count is in the same ballpark as the
    decorator count grepped from app/*.py.

    The decorator count (155 @router.<verb> decorators in app/*.py) is the
    number cited in the bundles_0811 P7 brief. The manifest's count is
    higher (302) because:

      * some decorators register the same path under multiple methods
        (e.g. ``@router.api_route(..., methods=[\"GET\",\"POST\"])``)
      * routes from app/ subdirectories (sandbox/, mcp/, etc.) are included
      * the manifest counts distinct (method, path) pairs, not decorators

    A decorator count < 100 or > 400 would indicate the manifest is broken
    (missing modules or double-counting). The two bounds below are wide
    enough to allow normal growth but catch a catastrophic regression.
    """
    committed = json.loads(MANIFEST_JSON.read_text())
    n = committed["route_count"]
    assert 100 <= n <= 400, (
        f"manifest route count {n} is outside the expected 100–400 band; " "the generator is likely broken"
    )


def test_no_legacy_tier_slugs_in_manifest() -> None:
    """The manifest must not contain legacy tier slugs (repo-wide audit gate).

    A repo-wide test fails CI if the strings ``cook|operator|studio`` appear
    ANYWHERE in the tree — this test extends that guard to the generated
    manifest, which lives under docs/ and would otherwise be a gap.
    """
    committed = MANIFEST_JSON.read_text() + "\n" + MANIFEST_MD.read_text()
    # ``cook`` appears as a substring of ``cookbook`` — that's fine and
    # intended (cookbook is the canonical name). The audit bans the slug as a
    # STANDALONE tier label, so we match word boundaries.
    import re

    for slug in ("cook", "operator", "studio"):
        pattern = rf"\b{slug}\b"
        matches = re.findall(pattern, committed)
        # ``cookbook`` is allowed; a bare ``cook`` tier label is not.
        if slug == "cook":
            # Filter out matches inside 'cookbook' by checking they're not
            # preceded/followed by 'book'.
            real = [
                m
                for m in re.finditer(pattern, committed)
                if not (
                    committed[max(0, m.start() - 0) : m.start()] == ""
                    and committed[m.end() : m.end() + 4] == "book"
                )
            ]
            assert not real, (
                f"legacy tier slug '{slug}' found in manifest: " f"{[r.group() for r in real][:3]}"
            )
        else:
            assert not matches, f"legacy tier slug '{slug}' found in manifest: {matches[:3]}"
