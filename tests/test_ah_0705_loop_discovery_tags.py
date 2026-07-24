"""atomic-habits 2026-07-05 rank-8 REVENUE-CATALOG: loop discovery tags.

Pins the contract that every starter loop carries a non-empty, slug-keyed
discovery `tags` list AND that the list is emitted onto the LoopVersion
manifest (F-API-14: manifest carries category + tags + tier). Zero-run loops
were invisible in browse because only `category` was faceted; tags widen
discovery. Data-only — no schema change. The Verifier.tags column + the
list_verifiers tag-filter query are next-cycle wiring (see
~/.hermes/atomic-habits/executed/2026-07-05.json next_cycle_deferred).
"""

from __future__ import annotations

from scripts.seed_starter_catalog import (
    LOOP_TAGS_BY_SLUG,
    STARTER_LOOPS,
    _loop_manifest_toml,
)


def test_every_starter_loop_has_tags() -> None:
    """No starter loop may ship without at least one discovery tag."""
    missing = [s["slug"] for s in STARTER_LOOPS if not LOOP_TAGS_BY_SLUG.get(s["slug"])]
    assert not missing, f"loops with no discovery tags: {missing}"


def test_manifest_emits_category_and_tags() -> None:
    """The [loop] manifest section carries category + a tags TOML array."""
    for spec in STARTER_LOOPS:
        manifest = _loop_manifest_toml(spec)
        assert "tags = [" in manifest, f"{spec['slug']}: manifest missing tags array"
        assert (
            f'category = "{spec.get("category", "")}"' in manifest
        ), f"{spec['slug']}: manifest missing category line"
        for tag in LOOP_TAGS_BY_SLUG.get(spec["slug"], []):
            assert (
                f'"{tag}"' in manifest
            ), f"{spec['slug']}: tag {tag!r} not surfaced on manifest"


def test_tags_are_lowercase_hyphenated_facets() -> None:
    """Tags are browse-filter facets: lowercase, hyphen-separated, non-empty."""
    for slug, tags in LOOP_TAGS_BY_SLUG.items():
        assert tags, f"{slug}: empty tag list"
        for tag in tags:
            assert tag == tag.lower(), f"{slug}: tag {tag!r} not lowercase"
            assert " " not in tag, f"{slug}: tag {tag!r} contains a space"
            assert tag.strip(), f"{slug}: blank tag"


def test_seed_refreshes_stale_manifest_on_existing_loop(db_session) -> None:
    """A re-run pushes a changed manifest onto an already-seeded loop.

    Guards the "code shipped but data not migrated" trap: without the
    content-diff refresh in _seed_loops, tag edits only reach fresh clones and
    the 9 live loops keep their old tag-less manifest forever.
    """
    from app.models import LoopVersion

    from scripts.seed_starter_catalog import _seed_loops

    # First seed → creates loops + v1.0.0 versions with the current manifest.
    _seed_loops(db_session)
    db_session.flush()

    spec = STARTER_LOOPS[1]  # pr-review-loop
    loop_slug = spec["slug"]
    from app.models import Loop

    loop = db_session.query(Loop).filter(Loop.slug == loop_slug).first()
    version = (
        db_session.query(LoopVersion)
        .filter(LoopVersion.loop_id == loop.id, LoopVersion.semver == "1.0.0")
        .first()
    )
    assert version is not None
    # Simulate a stale (pre-tags) manifest already in the DB.
    version.manifest = "[loop]\nslug = \"pr-review-loop\"\n"
    db_session.flush()

    # Re-seed → should refresh the stale manifest in place (no new version row).
    _seed_loops(db_session)
    db_session.flush()

    versions = (
        db_session.query(LoopVersion)
        .filter(LoopVersion.loop_id == loop.id, LoopVersion.semver == "1.0.0")
        .all()
    )
    assert len(versions) == 1, "refresh must not create a duplicate version row"
    assert "tags = [" in versions[0].manifest, "stale manifest was not refreshed with tags"
    for tag in LOOP_TAGS_BY_SLUG[loop_slug]:
        assert f'"{tag}"' in versions[0].manifest
