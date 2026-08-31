"""bundles_0811 — a federated search row must carry an identifier you can ACT on.

THE DEFECT
----------
`GET /api/skills/external` returned rows with a bare `slug` and NO install ref,
so an anonymous caller had nothing to feed `GET /api/skills/install`. Measured
against prod 2026-08-11 across 6 queries: **180/180 rows had `install_ref: null`.**

Worse, the slug SHAPES disagree by source:

    search shows   mvanhorn--cli-printing-press--printing-press   (skills-sh live id)
    hub stores     skills-sh-mvanhorn-cli-printing-press-printing-press

Probed live: the hub-form slug installs (`kind=fetch`, real raw URL); the
search-shown slug 404s. A user pasting exactly what they were shown is told
"not found" for a row we demonstrably hold — a silent wrong answer.

WHAT THIS FIX DOES, AND WHAT IT DOES NOT
----------------------------------------
`ExternalSkill.to_dict()` is the single serializer for every federated search
row, so emitting `install_ref = f"{source}:{slug}"` there closes the "no
identifier at all" half everywhere at once. It reuses the exact contract
`services/metasearch.py` already builds and `install_routes` already accepts —
no new vocabulary.

It does NOT by itself make every skills-sh row resolvable, because those rows
come from a live skills.sh search whose ids are not the hub snapshot's slugs.
That reconciliation is a separate, larger piece of work (the hub indexer would
have to record the upstream id alongside its own slug). These tests pin what is
true now so the gap is visible and measurable rather than silent.
"""

from __future__ import annotations

from app.services.federation import ExternalSkill, InstallPath


def _row(source: str, slug: str, path: InstallPath = InstallPath.FETCH_ORIGIN) -> ExternalSkill:
    return ExternalSkill(
        slug=slug,
        title=slug,
        source=source,
        install_path=path,
        origin_url=f"https://example.invalid/{slug}",
    )


class TestEveryRowCarriesAnInstallRef:
    def test_install_ref_is_present(self):
        d = _row("hermes-hub", "official-security-1password").to_dict()
        assert d["install_ref"], "a search row with no install ref is unusable"

    def test_install_ref_uses_the_source_colon_slug_contract(self):
        """Same shape metasearch builds and install_routes accepts."""
        d = _row("hermes-hub", "official-security-1password").to_dict()
        assert d["install_ref"] == "hermes-hub:official-security-1password"

    def test_install_ref_is_emitted_for_every_source(self):
        for src in ("hermes-hub", "skills-sh", "github-oss", "clawhub", "lobehub", "browse-sh"):
            d = _row(src, "some-slug").to_dict()
            assert d["install_ref"] == f"{src}:some-slug", f"{src} row lacks a usable ref"

    def test_deep_link_rows_also_carry_a_ref(self):
        """A deep-link row still resolves — to an ORIGIN instruction, not bytes.

        Withholding the ref would make it look uninstallable rather than
        honestly origin-only.
        """
        d = _row("clawhub", "1password", InstallPath.DEEP_LINK).to_dict()
        assert d["install_ref"] == "clawhub:1password"

    def test_ref_prefix_is_one_the_install_route_accepts(self):
        """Guards the two ends drifting apart again."""
        from app.install_routes import _FEDERATED_SLUG_PREFIXES

        for src in ("hermes-hub", "skills-sh", "github-oss", "clawhub", "lobehub", "browse-sh"):
            prefix = _row(src, "x").to_dict()["install_ref"].split(":", 1)[0]
            assert prefix in _FEDERATED_SLUG_PREFIXES, (
                f"search emits '{prefix}:' but the install route would reject it"
            )

    def test_the_existing_contract_is_unchanged(self):
        """install_ref is ADDITIVE — no consumer loses a field."""
        d = _row("hermes-hub", "x").to_dict()
        for k in (
            "slug",
            "title",
            "source",
            "install_path",
            "origin_url",
            "license",
            "redistributable",
            "description",
            "namespace",
            "quality",
            "scan_status",
        ):
            assert k in d, f"regression: {k} disappeared from the search row"
