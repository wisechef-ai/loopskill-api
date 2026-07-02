"""Phase B (loopskill_activate_0701) — CONNECTOR ARTIFACT client-side tests.

Covers the contract in docs/design/activate0701-phaseB-connectors.md §Tests
(client section): apply happy path writes managed block + preserves unrelated
yaml keys; env_missing refusal (no write); health-fail rollback restores
byte-identical config; rollback_failed path; stage_only writes staged file, no
restart; snapshot rotation keeps 3.

The KILL-TEST (broken connector → health probe fails → auto-rollback restores
snapshot → gateway survives → outcome rolled_back) is
``test_health_fail_rollback_restores_byte_identical_config``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


from app.connector_apply import (
    ConnectorApplier,
    ConnectorApplyResult,
)


# ────────────────────────── helpers ────────────────────────────────────────

_INITIAL_CONFIG = """\
# Hermes agent config
agent_name: tori
model: glm-5
# existing mcp servers (NOT managed by loopskill)
mcp_servers:
  existing:
    command: /usr/bin/existing-tool
# unrelated top-level key
logging:
  level: info
"""


def _write_config(path: Path, text: str = _INITIAL_CONFIG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _fake_restart_ok(_cmd: list[str]) -> tuple[bool, str | None]:
    """A restart callable that always succeeds."""
    return True, None


def _make_health_ok_marker(marker: Path) -> Callable[[Path], tuple[bool, str | None]]:
    """A health probe that flips a marker file then returns ok."""

    def probe(_config: Path) -> tuple[bool, str | None]:
        marker.write_text("alive")
        return True, None

    return probe


def _make_health_fail_marker(marker: Path) -> Callable[[Path], tuple[bool, str | None]]:
    """A health probe that ALWAYS reports the gateway is dead — KILL-TEST."""

    def probe(_config: Path) -> tuple[bool, str | None]:
        return False, "gateway process not alive"

    return probe


def _stdio_diff(slug: str = "zai") -> dict[str, Any]:
    return {
        "add": [
            {
                "slug": slug,
                "semver": "1.0.0",
                "config_template": {
                    "command": "npx",
                    "args": ["-y", "zai-mcp"],
                    "env": {"ZAI_API_KEY": "${ZAI_API_KEY}"},
                },
                "required_env": ["ZAI_API_KEY"],
            }
        ],
        "update": [],
        "remove": [],
    }


# ─────────────────────── happy path ────────────────────────────────────────


class TestHappyPath:
    def test_writes_managed_block_and_preserves_unrelated_keys(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.yaml"
        _write_config(config_path)
        marker = tmp_path / "alive"
        monkeypatch.setenv("ZAI_API_KEY", "test-key")

        applier = ConnectorApplier(
            config_yaml_path=config_path,
            gateway_restart_cmd=["/bin/true"],
            health_probe=_make_health_ok_marker(marker),
        )
        result = applier.apply(_stdio_diff())

        assert isinstance(result, ConnectorApplyResult)
        assert result.outcome == "success"
        assert result.applied == ["zai"]

        text = config_path.read_text()
        # Managed block is present
        assert "loopskill-managed" in text
        assert "${ZAI_API_KEY}" in text  # var ref verbatim
        # Unrelated keys preserved
        assert "agent_name: tori" in text
        assert "model: glm-5" in text
        assert "existing-tool" in text  # pre-existing mcp server untouched
        assert "level: info" in text


# ─────────────────────── env_missing refusal ───────────────────────────────


class TestEnvMissing:
    def test_missing_env_var_refuses_no_write(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.yaml"
        original = _INITIAL_CONFIG
        _write_config(config_path, original)
        # Ensure ZAI_API_KEY is NOT set
        monkeypatch.delenv("ZAI_API_KEY", raising=False)

        applier = ConnectorApplier(
            config_yaml_path=config_path,
            gateway_restart_cmd=["/bin/true"],
            health_probe=lambda _c: (True, None),
        )
        result = applier.apply(_stdio_diff())

        assert result.outcome == "env_missing"
        assert "ZAI_API_KEY" in (result.failure_reason or "")
        # config.yaml must be byte-identical (no write happened)
        assert config_path.read_text() == original


# ───────────────── KILL-TEST: health-fail rollback ─────────────────────────


class TestHealthFailRollback:
    """THE KILL-TEST: a broken connector deploy → health probe fails →
    auto-rollback restores the byte-identical snapshot → gateway survives →
    outcome rolled_back reported."""

    def test_health_fail_rollback_restores_byte_identical_config(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.yaml"
        original = _INITIAL_CONFIG
        _write_config(config_path, original)
        monkeypatch.setenv("ZAI_API_KEY", "test-key")
        # Snapshot rotation root
        monkeypatch.setenv("HOME", str(tmp_path))  # so backups land in tmp

        restart_calls: list[int] = []

        def restart(_cmd: list[str]) -> tuple[bool, str | None]:
            restart_calls.append(1)
            return True, None  # restart ITSELF succeeds; the health PROBE fails

        applier = ConnectorApplier(
            config_yaml_path=config_path,
            gateway_restart_cmd=["/bin/true"],
            health_probe=_make_health_fail_marker(tmp_path / "dead"),
            restart_fn=restart,
        )
        result = applier.apply(_stdio_diff(slug="broken"))

        assert result.outcome == "rolled_back"
        assert result.rolled_back is True
        assert result.failure_reason is not None
        # Gateway survived — restart was called at least twice (apply + rollback)
        assert len(restart_calls) >= 2
        # THE critical assertion: config.yaml is byte-identical to the snapshot
        assert config_path.read_text() == original, (
            "rollback must restore the byte-identical pre-apply config — "
            "an agent must NEVER be left silently broken"
        )
        # The managed block must NOT be in the restored config
        assert "loopskill-managed" not in config_path.read_text()


# ───────────────────── rollback_failed path ────────────────────────────────


class TestRollbackFailed:
    def test_rollback_failed_when_restore_restart_also_fails(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.yaml"
        _write_config(config_path)
        monkeypatch.setenv("ZAI_API_KEY", "k")

        def restart_always_fails(_cmd: list[str]) -> tuple[bool, str | None]:
            return False, "systemctl failed"

        applier = ConnectorApplier(
            config_yaml_path=config_path,
            gateway_restart_cmd=["/bin/true"],
            health_probe=_make_health_fail_marker(tmp_path / "d"),
            restart_fn=restart_always_fails,
        )
        result = applier.apply(_stdio_diff(slug="x"))
        # Health fails AND restart-after-restore also fails → rollback_failed
        assert result.outcome == "rollback_failed"
        # CRITICAL: surface rollback_failed so the host cron surfaces it.
        assert result.rollback_failed is True


# ──────────────────────── stage_only mode ──────────────────────────────────


class TestStageOnly:
    def test_stage_only_writes_staged_file_no_restart(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.yaml"
        _write_config(config_path)
        monkeypatch.setenv("ZAI_API_KEY", "k")
        restart_count = 0

        def restart(_cmd: list[str]) -> tuple[bool, str | None]:
            nonlocal restart_count
            restart_count += 1
            return True, None

        applier = ConnectorApplier(
            config_yaml_path=config_path,
            gateway_restart_cmd=["/bin/true"],
            health_probe=lambda _c: (True, None),
            restart_fn=restart,
            stage_only=True,
        )
        result = applier.apply(_stdio_diff(slug="stage-me"))
        assert result.outcome == "staged"
        assert restart_count == 0, "stage_only MUST NOT restart the gateway"
        staged_path = Path(str(config_path) + ".lsk-staged")
        assert staged_path.exists()
        assert "loopskill-managed" in staged_path.read_text()
        # original config.yaml unchanged
        assert "loopskill-managed" not in config_path.read_text()


# ──────────────────── snapshot rotation (keep 3) ───────────────────────────


class TestSnapshotRotation:
    def test_snapshot_rotation_keeps_last_3(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "config.yaml"
        _write_config(config_path)
        monkeypatch.setenv("ZAI_API_KEY", "k")
        applier = ConnectorApplier(
            config_yaml_path=config_path,
            gateway_restart_cmd=["/bin/true"],
            health_probe=lambda _c: (True, None),
            restart_fn=lambda _c: (True, None),
        )

        # Run 5 applies; only the last 3 snapshots should remain.
        for i in range(5):
            diff = _stdio_diff(slug=f"snap{i}")
            res = applier.apply(diff)
            assert res.outcome == "success", res

        backups = sorted(config_path.parent.glob("config.yaml.lsk-backup-*"))
        assert len(backups) == 3, f"expected exactly 3 rotating backups, got {len(backups)}"
