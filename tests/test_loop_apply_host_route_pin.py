"""A host-local route pin (provider/model/base_url) survives loop-apply.

Live bug, Chef 2026-11-22: ``loopskill/cron-watchdog`` was repinned from the exhausted zai
quota to the local model. The ``*/30`` loop-apply cron rewrote ``provider: None`` and the
manifest model at 13:00:02, before the job's 13:00 fire, so every run went back to the
quota wall. ``apply_assignments`` itself never writes a non-null provider, so a non-empty
``provider`` on a managed job can only be an operator pin and is the host's to keep.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.loop_apply import apply_assignments


def _assignment(*, model="glm-5.3-flash", prompt="Scan the fleet.", epoch=1):
    return {
        "loop_key": "cron-watchdog",
        "placement_id": "p1",
        "epoch": epoch,
        "status": "assigned",
        "manifest": {
            "loop_id": "cron-watchdog",
            "schedule": "*/30 * * * *",
            "prompt": prompt,
            "skills": [],
            "enabled": True,
            "deliver": "local",
            "model": model,
        },
    }


def _job(jobs: Path) -> dict:
    return json.loads(jobs.read_text())["jobs"][0]


def _pin(jobs: Path, **fields) -> None:
    data = json.loads(jobs.read_text())
    data["jobs"][0].update(fields)
    jobs.write_text(json.dumps(data))


def _applied(tmp_path: Path) -> Path:
    jobs = tmp_path / "jobs.json"
    apply_assignments([_assignment()], jobs)
    return jobs


class TestHostRoutePin:
    def test_pinned_job_reapply_is_a_noop(self, tmp_path: Path):
        jobs = _applied(tmp_path)
        _pin(jobs, provider="local-llm", model="local-llm")
        before = jobs.read_text()

        result = apply_assignments([_assignment()], jobs)

        assert not result.changed, "the manifest model must not count as drift on a pinned job"
        assert jobs.read_text() == before

    def test_pin_survives_a_real_update_and_the_manifest_still_wins_elsewhere(self, tmp_path: Path):
        jobs = _applied(tmp_path)
        _pin(jobs, provider="local-llm", model="local-llm", base_url="http://hercules:8000/v1")

        result = apply_assignments([_assignment(prompt="Scan harder.", epoch=2)], jobs)

        assert result.updated == ["cron-watchdog"]
        job = _job(jobs)
        assert job["prompt"] == "Scan harder."  # manifest still owns the prompt
        assert job["loopskill"]["epoch"] == 2
        assert (job["provider"], job["model"], job["base_url"]) == (
            "local-llm",
            "local-llm",
            "http://hercules:8000/v1",
        )

    def test_pin_survives_a_manifest_model_change(self, tmp_path: Path):
        jobs = _applied(tmp_path)
        _pin(jobs, provider="local-llm", model="local-llm")

        result = apply_assignments([_assignment(model="glm-5.3", epoch=2)], jobs)

        assert not result.changed
        assert _job(jobs)["model"] == "local-llm"

    def test_unpinned_job_still_tracks_the_manifest_model(self, tmp_path: Path):
        jobs = _applied(tmp_path)

        result = apply_assignments([_assignment(model="glm-5.3", epoch=2)], jobs)

        assert result.updated == ["cron-watchdog"]
        job = _job(jobs)
        assert (job["provider"], job["model"], job["base_url"]) == (None, "glm-5.3", None)

    def test_blank_provider_is_not_a_pin(self, tmp_path: Path):
        jobs = _applied(tmp_path)
        for blank in ("", "   "):
            _pin(jobs, provider=blank, model="stale-model")

            result = apply_assignments([_assignment()], jobs)

            assert result.updated == ["cron-watchdog"]
            job = _job(jobs)
            assert (job["provider"], job["model"]) == (None, "glm-5.3-flash")

    def test_non_string_provider_is_not_a_pin(self, tmp_path: Path):
        jobs = _applied(tmp_path)
        _pin(jobs, provider=["local-llm"], model="stale-model")

        result = apply_assignments([_assignment()], jobs)

        assert result.updated == ["cron-watchdog"]
        assert _job(jobs)["provider"] is None

    def test_pin_with_null_model_keeps_the_pin_and_no_model(self, tmp_path: Path):
        jobs = _applied(tmp_path)
        _pin(jobs, provider="local-llm", model=None)

        result = apply_assignments([_assignment()], jobs)

        assert not result.changed
        assert _job(jobs)["model"] is None
