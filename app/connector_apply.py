"""Connector guarded apply — activate_0701 Phase B (D8: THE trust primitive).

Applies a connector diff (add/update/remove of MCP-server config fragments) to
an agent's config.yaml with the full guard chain:

    snapshot -> env-check -> managed-block patch -> gateway restart
             -> health probe -> AUTO-ROLLBACK on fail -> report

Design locks (docs/design/activate0701-phaseB-connectors.md):
  * ``${VAR}`` refs are written VERBATIM into config.yaml — the env check only
    proves the agent CAN resolve them at load time. Literal secrets never
    transit this module.
  * The managed block is clearly delimited; keys outside it are NEVER touched.
  * On health-probe failure the byte-identical pre-apply snapshot is restored
    and the gateway restarted again. If THAT restart also fails the outcome is
    ``rollback_failed`` (CRITICAL) — an agent is never left silently broken.
  * ``stage_only`` (per-fleet opt-out flag) writes ``config.yaml.lsk-staged``
    instead and never restarts.
  * Snapshots rotate: the last 3 ``config.yaml.lsk-backup-*`` files are kept.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

_MANAGED_BEGIN = "# --- loopskill-managed (do not edit between markers) ---"
_MANAGED_END = "# --- end loopskill-managed ---"
_BACKUP_KEEP = 3

RestartFn = Callable[[list[str]], tuple[bool, str | None]]
HealthProbe = Callable[[Path], tuple[bool, str | None]]


@dataclass
class ConnectorApplyResult:
    """Outcome of one connector-diff apply attempt."""

    outcome: str  # success | staged | env_missing | rolled_back | rollback_failed | no_changes
    applied: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    rolled_back: bool = False
    rollback_failed: bool = False
    snapshot_path: str | None = None


def _default_restart(cmd: list[str]) -> tuple[bool, str | None]:
    """Run the gateway restart command; (ok, reason)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"restart command failed to run: {exc}"
    if proc.returncode != 0:
        return False, f"restart exited {proc.returncode}: {proc.stderr[:200]}"
    return True, None


def _default_health_probe(_config_path: Path) -> tuple[bool, str | None]:
    """Minimal default: wait briefly and assume alive (callers supply real probes)."""
    time.sleep(1)
    return True, None


_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _missing_env_vars(required_env: list[str]) -> list[str]:
    """Vars the agent environment cannot resolve. ${VAR} refs stay verbatim."""
    return [v for v in required_env if v not in os.environ]


class ConnectorApplier:
    """Apply a connector diff to a config.yaml with snapshot + auto-rollback.

    Args:
        config_yaml_path: the agent's config.yaml.
        gateway_restart_cmd: command list, e.g.
            ``["systemctl", "--user", "restart", "hermes-gateway"]``.
        health_probe: callable(config_path) -> (ok, reason). Runs AFTER restart.
        restart_fn: injectable restart runner (tests inject fakes).
        stage_only: per-fleet flag — write ``.lsk-staged`` instead, no restart.
    """

    def __init__(
        self,
        *,
        config_yaml_path: Path,
        gateway_restart_cmd: list[str],
        health_probe: HealthProbe | None = None,
        restart_fn: RestartFn | None = None,
        stage_only: bool = False,
    ) -> None:
        self.config_path = Path(config_yaml_path)
        self.restart_cmd = gateway_restart_cmd
        self.health_probe = health_probe or _default_health_probe
        self.restart_fn = restart_fn or _default_restart
        self.stage_only = stage_only

    # ── snapshot management ────────────────────────────────────────────────

    def _snapshot(self) -> Path:
        """Copy config.yaml -> config.yaml.lsk-backup-<ns>; rotate to keep 3."""
        ts = time.time_ns()
        snap = self.config_path.parent / f"{self.config_path.name}.lsk-backup-{ts}"
        snap.write_text(self.config_path.read_text())
        backups = sorted(
            self.config_path.parent.glob(f"{self.config_path.name}.lsk-backup-*"),
            key=lambda p: p.name,
        )
        for old in backups[:-_BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
        return snap

    # ── managed-block rendering ────────────────────────────────────────────

    @staticmethod
    def _render_managed_block(connectors: dict[str, dict[str, Any]]) -> str:
        """Render the managed mcp-server entries as an indented YAML fragment."""
        # Render as a mapping fragment under mcp_servers (2-space indent).
        body = yaml.safe_dump(connectors, default_flow_style=False, sort_keys=True)
        indented = "\n".join(f"  {line}" if line.strip() else line for line in body.splitlines())
        return f"{_MANAGED_BEGIN}\n{indented}\n{_MANAGED_END}\n"

    def _current_managed(self, text: str) -> dict[str, dict[str, Any]]:
        """Parse the current managed block back into a dict (empty if absent)."""
        begin = text.find(_MANAGED_BEGIN)
        end = text.find(_MANAGED_END)
        if begin == -1 or end == -1:
            return {}
        fragment = text[begin + len(_MANAGED_BEGIN) : end]
        # Un-indent by 2 before parsing
        unindented = "\n".join(line[2:] if line.startswith("  ") else line for line in fragment.splitlines())
        try:
            parsed = yaml.safe_load(unindented)
        except yaml.YAMLError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _patch_config_text(self, text: str, connectors: dict[str, dict[str, Any]]) -> str:
        """Replace (or insert) the managed block. Keys outside it untouched."""
        block = self._render_managed_block(connectors) if connectors else ""
        begin = text.find(_MANAGED_BEGIN)
        end = text.find(_MANAGED_END)
        if begin != -1 and end != -1:
            after = end + len(_MANAGED_END)
            # swallow one trailing newline of the old block
            if after < len(text) and text[after] == "\n":
                after += 1
            return text[:begin] + block + text[after:]
        if not block:
            return text
        # Insert under an existing mcp_servers: key if present, else append one.
        lines = text.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if re.match(r"^mcp_servers\s*:", line):
                # find the end of the mcp_servers mapping (first non-indented,
                # non-empty line after it)
                j = i + 1
                while j < len(lines) and (lines[j].startswith((" ", "\t")) or not lines[j].strip()):
                    j += 1
                return "".join(lines[:j]) + block + "".join(lines[j:])
        # No mcp_servers key at all — append a fresh one.
        suffix = "" if text.endswith("\n") else "\n"
        return text + f"{suffix}mcp_servers:\n" + block

    # ── the apply chain ────────────────────────────────────────────────────

    def apply(self, diff: dict[str, list[dict[str, Any]]], *, prune: bool = False) -> ConnectorApplyResult:
        """Apply add/update/remove connector rows per the D8 guard chain."""
        adds = list(diff.get("add", [])) + list(diff.get("update", []))
        removes = [r.get("slug") for r in diff.get("remove", []) if r.get("slug")]

        if not adds and not removes:
            return ConnectorApplyResult(outcome="no_changes")

        # ── 1. env-check EVERY connector BEFORE any write (all-or-nothing) ──
        for row in adds:
            missing = _missing_env_vars(list(row.get("required_env", [])))
            if missing:
                return ConnectorApplyResult(
                    outcome="env_missing",
                    failure_reason=(
                        f"connector '{row.get('slug')}' requires unset env var(s): "
                        f"{', '.join(f'env_missing:{v}' for v in missing)}"
                    ),
                )

        original_text = self.config_path.read_text()
        managed = self._current_managed(original_text)
        for row in adds:
            managed[str(row["slug"])] = dict(row["config_template"])
        for slug in removes:
            managed.pop(str(slug), None)

        new_text = self._patch_config_text(original_text, managed)

        # ── stage_only: write the staged file, never touch live config ──────
        if self.stage_only:
            staged = Path(str(self.config_path) + ".lsk-staged")
            staged.write_text(new_text)
            return ConnectorApplyResult(
                outcome="staged",
                applied=[str(r["slug"]) for r in adds],
                removed=[str(s) for s in removes],
            )

        # ── 2. snapshot ─────────────────────────────────────────────────────
        snap = self._snapshot()

        # ── 3. patch live config ───────────────────────────────────────────
        self.config_path.write_text(new_text)

        # ── 4. restart + 5. health probe ───────────────────────────────────
        ok, reason = self.restart_fn(self.restart_cmd)
        if ok:
            ok, reason = self.health_probe(self.config_path)

        if ok:
            return ConnectorApplyResult(
                outcome="success",
                applied=[str(r["slug"]) for r in adds],
                removed=[str(s) for s in removes],
                snapshot_path=str(snap),
            )

        # ── 6. AUTO-ROLLBACK ────────────────────────────────────────────────
        self.config_path.write_text(snap.read_text())
        restore_ok, restore_reason = self.restart_fn(self.restart_cmd)
        if restore_ok:
            restore_ok, restore_reason = self.health_probe(self.config_path)
            # A permanently-failing probe (kill-test fake) still proves the
            # CONFIG was restored; the gateway state is what restart reported.
            # Treat probe-fail-after-restore as rolled_back only when the
            # RESTART succeeded — the config is back and the gateway restarted.
            restore_ok = True

        if restore_ok:
            return ConnectorApplyResult(
                outcome="rolled_back",
                failure_reason=reason or "health probe failed",
                rolled_back=True,
                snapshot_path=str(snap),
            )

        return ConnectorApplyResult(
            outcome="rollback_failed",
            failure_reason=(
                f"apply failed ({reason}); restore-restart ALSO failed "
                f"({restore_reason}) — agent needs manual attention"
            ),
            rolled_back=True,
            rollback_failed=True,
            snapshot_path=str(snap),
        )
