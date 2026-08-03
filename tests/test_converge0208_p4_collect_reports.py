"""converge_0208 P4 — ship the telemetry drain, `scripts/loopskill-collect-reports.py`.

`docs/SELF_HOST.md` told a self-hoster to "install the collector (copy from
scripts/…)". There was no collector in `scripts/`; the only copy lived on one
machine, hardcoded to that machine's secrets file
(`~/.hermes/secrets/loopskill_tori.json`).

The collector is the hop that turns a spool file into a `LoopRun` row. Without
it a fired loop calls `loopskill-emit-run.sh`, the record lands in the outbox —
and stays there forever. That is the other half of why `loop_runs` had one row.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = REPO_ROOT / "scripts" / "loopskill-collect-reports.py"
EMITTER = REPO_ROOT / "scripts" / "loopskill-emit-run.sh"


@pytest.fixture
def collector(tmp_path, monkeypatch):
    """Import the script as a module with every path pointed at tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOOPSKILL_OUTBOX", str(tmp_path / "outbox"))
    monkeypatch.setenv("LOOPSKILL_CRON_STATE", str(tmp_path / "jobs.json"))
    monkeypatch.setenv("LOOPSKILL_LOCKFILE", str(tmp_path / "lock.json"))
    monkeypatch.setenv("LOOPSKILL_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("LOOPSKILL_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.delenv("LOOPSKILL_MEMBER_KEY", raising=False)
    monkeypatch.delenv("RECIPES_API_KEY", raising=False)

    spec = importlib.util.spec_from_file_location("_p4_collector", COLLECTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules.pop("_p4_collector", None)
    return mod


def test_collector_ships_in_the_repo():
    assert COLLECTOR.is_file(), "SELF_HOST.md tells self-hosters to copy this from scripts/"


class TestKeyResolution:
    def test_prefers_the_explicit_member_key_env(self, collector, monkeypatch):
        monkeypatch.setenv("LOOPSKILL_MEMBER_KEY", "rec_live_member")
        assert collector.member_key() == "rec_live_member"

    def test_falls_back_to_recipes_api_key(self, collector, monkeypatch):
        monkeypatch.setenv("RECIPES_API_KEY", "rec_live_fallback")
        assert collector.member_key() == "rec_live_fallback"

    def test_falls_back_to_the_enrollment_secrets_file(self, collector, tmp_path):
        import base64

        (tmp_path / "secrets.json").write_text(
            json.dumps(
                {"member_api_key_plain_b64": base64.b64encode(b"rec_live_from_file").decode()}
            )
        )
        assert collector.member_key() == "rec_live_from_file"

    def test_no_key_anywhere_is_not_a_crash(self, collector):
        assert collector.member_key() is None
        assert collector.main() == 0, "a host that has not enrolled yet must not fail its cron"


class TestPayload:
    def test_reads_what_the_emitter_writes(self, collector, tmp_path):
        """The two scripts are one contract — drive the real emitter, not a fixture."""
        env = dict(os.environ)
        env["LOOPSKILL_OUTBOX"] = str(tmp_path / "outbox")
        subprocess.run(
            ["sh", str(EMITTER), "p4-proof", "success", "true", "0.5", "9", "ok"],
            check=True,
            capture_output=True,
            env=env,
            timeout=30,
        )
        payload, files, _ = collector.build_payload()
        assert len(files) == 1
        run = payload["loop_runs"][0]
        assert run["loop_slug"] == "p4-proof"
        assert run["outcome"] == "success"
        assert run["accepted_change"] is True
        assert run["cost_usd"] == 0.5
        assert run["duration_seconds"] == 9

    def test_a_torn_spool_file_is_left_for_the_next_cycle(self, collector, tmp_path):
        box = tmp_path / "outbox"
        box.mkdir()
        (box / "good.json").write_text('{"loop_slug":"a","outcome":"success"}')
        (box / "torn.json").write_text('{"loop_slug":"b","outc')
        payload, files, _ = collector.build_payload()
        assert len(payload["loop_runs"]) == 1
        assert [f.name for f in files] == ["good.json"], "an unreadable file must not be marked sent"

    def test_respects_the_server_side_batch_cap(self, collector, tmp_path):
        box = tmp_path / "outbox"
        box.mkdir()
        for i in range(collector.MAX_LOOP_RUNS + 25):
            (box / f"{i:04d}.json").write_text('{"loop_slug":"a","outcome":"success"}')
        payload, files, _ = collector.build_payload()
        assert len(payload["loop_runs"]) == collector.MAX_LOOP_RUNS
        assert len(files) == collector.MAX_LOOP_RUNS

    def test_empty_host_produces_a_payload_with_no_junk_keys(self, collector):
        payload, files, cron = collector.build_payload()
        assert files == []
        assert "loop_runs" not in payload
        assert "lockfile_state" not in payload
        assert payload["cycle_ts"]

    def test_cron_health_counts_managed_loop_failures(self, collector, tmp_path):
        (tmp_path / "jobs.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {"name": "loopskill/p4-proof", "last_status": "error"},
                        {"name": "something-else", "last_status": "ok"},
                    ]
                }
            )
        )
        _, _, cron = collector.build_payload()
        assert cron["counts"] == {"total": 2, "ok": 1, "error": 1}
        assert cron["failed"][0]["job_name"] == "loopskill/p4-proof"


class TestNeverFailsItsCron:
    def test_unreachable_api_exits_zero(self, collector, monkeypatch, tmp_path):
        monkeypatch.setenv("LOOPSKILL_MEMBER_KEY", "rec_live_x")
        box = tmp_path / "outbox"
        box.mkdir()
        (box / "a.json").write_text('{"loop_slug":"a","outcome":"success"}')

        def _boom(*_a, **_k):
            raise OSError("network is down")

        monkeypatch.setattr(collector.urllib.request, "urlopen", _boom)
        assert collector.main() == 0
        assert (box / "a.json").exists(), "an unsent record must NOT be moved to .sent"
