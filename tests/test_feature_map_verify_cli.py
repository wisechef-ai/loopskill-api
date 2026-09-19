"""Contract tests for feature-map.yaml and the deterministic verify CLI."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]


def _feature_map() -> dict:
    return yaml.safe_load((ROOT / "feature-map.yaml").read_text(encoding="utf-8"))


def test_every_feature_map_path_exists_in_production_route_table():
    route_paths = {route.path for route in create_app().routes}
    for feature_id, feature in _feature_map()["features"].items():
        assert feature["path"] in route_paths, f"{feature_id}: missing route {feature['path']}"


def test_feature_map_and_cli_flow_ids_have_exact_parity():
    from cli.verify import FLOW_RUNNERS

    mapped = {feature["verify"] for feature in _feature_map()["features"].values()}
    assert mapped == set(FLOW_RUNNERS)


def test_run_all_succeeds_on_seeded_temp_database(tmp_path):
    from cli.verify import main

    db = tmp_path / "verify.db"
    assert main(["--db", str(db), "seed"]) == 0
    assert main(["--db", str(db), "run", "all"]) == 0


def test_check_returns_nonzero_when_an_invariant_is_broken(tmp_path, monkeypatch):
    from cli import verify

    db = tmp_path / "verify.db"
    assert verify.main(["--db", str(db), "seed"]) == 0

    def broken_invariant(_result):
        raise verify.VerificationFailure("deliberately broken invariant")

    monkeypatch.setitem(verify.INVARIANT_CHECKS, "health", (broken_invariant,))
    assert verify.main(["--db", str(db), "check"]) != 0


def test_verify_harness_never_leaks_env_into_the_process(tmp_path):
    """The harness must not leave WR_* overrides behind.

    A leaked WR_COOKIES_SECURE=false makes every later Settings() under a
    non-sqlite DATABASE_URL refuse to boot — invisible in the sqlite lane,
    fatal in the postgres lane. Pinned so the failure mode cannot return.
    """
    import os

    from cli import verify

    before = {key: os.environ.get(key) for key in verify._VERIFY_ENV_KEYS}
    db = tmp_path / "verify.db"
    assert verify.main(["--db", str(db), "seed"]) == 0
    assert verify.main(["--db", str(db), "run", "health"]) == 0
    after = {key: os.environ.get(key) for key in verify._VERIFY_ENV_KEYS}
    assert after == before
