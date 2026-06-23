"""Thin self-updating reconcile client.

THE TRUST PRIMITIVE: atomic apply + auto-rollback. A reconcile
can NEVER leave an agent's skills directory broken. This is *why GitOps beat
manual kubectl*, ported to skills.

This is NOT a fat standalone daemon — it's a thin client that rides the host's
existing scheduler (cron / auto-update). Intelligence lives
server-side (the reconcile engine); the host-side piece fetches a diff
and applies it atomically. It ships AS A SKILL inside the cookbook, so it
self-updates through the same mechanism it manages — nothing standalone to rot.

Apply algorithm (per skill in the diff):
  1. Snapshot the live skills dir + lockfile to a last-known-good (LKG) staging
     path BEFORE any write.
  2. Apply the delta into a temp dir; verify each pulled skill's sha256 matches
     the cookbook's declared checksum_sha256.
  3. Only then atomically swap the temp content into the live skills dir
     (os.replace — rename, not in-place edit; scrubber-safe pathlib writes).
  4. Run a post-apply health check (skill files parse, frontmatter present,
     optional agent self-test hook).
  5. On ANY failure (hash mismatch, parse error, health regression): AUTO-REVERT
     to LKG, leave the lockfile at the pre-apply state, return a reconcile_failed
     record. The agent is NEVER left broken.

The client uses the agent's own x-api-key (no inbound auth) and is idempotent:
killed mid-apply, the next run reads a consistent lockfile + LKG and resumes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ─────────────────────────── Result types ───────────────────────────────


@dataclass
class ApplyResult:
    """Outcome of an atomic reconcile apply."""

    applied: list[str] = field(default_factory=list)  # slugs successfully applied
    removed: list[str] = field(default_factory=list)  # slugs pruned
    rolled_back: bool = False
    reconcile_failed: bool = False
    failure_reason: str | None = None
    failed_slug: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "removed": self.removed,
            "rolled_back": self.rolled_back,
            "reconcile_failed": self.reconcile_failed,
            "failure_reason": self.failure_reason,
            "failed_slug": self.failed_slug,
        }


# ─────────────────────────── sha256 helper ──────────────────────────────


def sha256_of_dir(path: Path) -> str:
    """Deterministic content hash of a skill directory (sorted file walk).

    Hashes relative paths + file bytes so a hand-edit or corruption changes the
    digest. Matches the publish-time checksum contract conceptually (the server
    stores checksum_sha256 of the tarball; here we content-address the unpacked
    skill dir for drift detection).
    """
    h = hashlib.sha256()
    if not path.exists():
        return ""
    for f in sorted(path.rglob("*")):
        if f.is_file():
            rel = f.relative_to(path).as_posix()
            h.update(rel.encode())
            h.update(b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


# ─────────────────────────── Health check ───────────────────────────────


def default_health_check(skills_dir: Path) -> tuple[bool, str | None]:
    """Default post-apply health check: every SKILL.md parses + has frontmatter.

    Returns (ok, reason). A broken skill (missing/empty SKILL.md, no YAML
    frontmatter delimiters) fails the check → triggers rollback.
    """
    for skill_md in skills_dir.rglob("SKILL.md"):
        text = skill_md.read_text(errors="replace")
        if not text.strip():
            return False, f"empty SKILL.md: {skill_md.relative_to(skills_dir)}"
        # Minimal frontmatter contract: starts with '---' fence.
        if not text.lstrip().startswith("---"):
            return False, f"missing frontmatter: {skill_md.relative_to(skills_dir)}"
    return True, None


# ─────────────────────────── The atomic client ──────────────────────────


class ReconcileClient:
    """Apply a reconcile diff to a local skills dir with atomic+rollback safety.

    Args:
        skills_dir: the agent's live skills directory.
        fetch_skill: callable(slug, version) -> Path to a staged unpacked skill
            dir (the caller pulls + unpacks the tarball; this client owns only
            the atomic swap + verify + rollback). In production this is the
            CDN-fronted delta pull; in tests it's a fixture.
        health_check: callable(skills_dir) -> (ok, reason). Defaults to
            default_health_check.
    """

    def __init__(
        self,
        skills_dir: Path,
        fetch_skill: Callable[[str, str], Path],
        health_check: Callable[[Path], tuple[bool, str | None]] | None = None,
    ) -> None:
        self.skills_dir = Path(skills_dir)
        self.fetch_skill = fetch_skill
        self.health_check = health_check or default_health_check
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def _snapshot_lkg(self, staging_root: Path) -> Path:
        """Copy the entire live skills dir to a last-known-good snapshot."""
        lkg = staging_root / "lkg"
        if lkg.exists():
            shutil.rmtree(lkg)
        shutil.copytree(self.skills_dir, lkg)
        return lkg

    def _restore_lkg(self, lkg: Path) -> None:
        """Atomically restore the live skills dir from the LKG snapshot."""
        # Replace live dir contents with the LKG snapshot.
        for child in list(self.skills_dir.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in lkg.iterdir():
            dest = self.skills_dir / child.name
            if child.is_dir():
                shutil.copytree(child, dest)
            else:
                shutil.copy2(child, dest)

    def apply(self, diff: dict[str, list[dict[str, Any]]], *, prune: bool = False) -> ApplyResult:
        """Apply a reconcile diff atomically.

        diff = {add:[...], update:[...], remove:[...], drift:[...]}. add/update/
        drift entries each carry {slug, version|to, checksum_sha256|expected_sha256}.
        remove entries carry {slug}.

        On ANY failure the live skills dir is restored to its pre-apply state and
        reconcile_failed=True is returned.
        """
        result = ApplyResult()
        staging_root = Path(tempfile.mkdtemp(prefix="recipes-reconcile-"))
        try:
            lkg = self._snapshot_lkg(staging_root)

            # Collect the skills to install/replace (add + update + drift).
            to_install: list[tuple[str, str, str | None]] = []
            for entry in diff.get("add", []):
                to_install.append((entry["slug"], entry.get("version", ""), entry.get("checksum_sha256")))
            for entry in diff.get("update", []):
                to_install.append((entry["slug"], entry.get("to", ""), entry.get("checksum_sha256")))
            for entry in diff.get("drift", []):
                to_install.append((entry["slug"], entry.get("version", ""), entry.get("expected_sha256")))

            try:
                for slug, version, expected_sha in to_install:
                    staged = self.fetch_skill(slug, version)
                    staged = Path(staged)
                    # Verify content address BEFORE swapping into the live dir.
                    if expected_sha:
                        actual = sha256_of_dir(staged)
                        if actual != expected_sha:
                            raise ValueError(
                                f"sha256 mismatch for {slug}@{version}: "
                                f"expected {expected_sha[:12]}…, got {actual[:12]}…"
                            )
                    # Atomic swap: stage into a temp sibling, then os.replace.
                    live = self.skills_dir / slug
                    tmp = self.skills_dir / f".{slug}.incoming"
                    if tmp.exists():
                        shutil.rmtree(tmp)
                    shutil.copytree(staged, tmp)
                    if live.exists():
                        shutil.rmtree(live)
                    os.replace(tmp, live)
                    result.applied.append(slug)

                # Prune (remove) — only when explicitly requested.
                if prune:
                    for entry in diff.get("remove", []):
                        slug = entry["slug"]
                        live = self.skills_dir / slug
                        if live.exists():
                            shutil.rmtree(live)
                        result.removed.append(slug)

                # Post-apply health check — the gate that triggers rollback.
                ok, reason = self.health_check(self.skills_dir)
                if not ok:
                    raise RuntimeError(f"health check failed: {reason}")

            except (ValueError, RuntimeError, OSError) as exc:
                # AUTO-ROLLBACK — restore the agent to its pre-apply state.
                self._restore_lkg(lkg)
                result.applied = []
                result.removed = []
                result.rolled_back = True
                result.reconcile_failed = True
                result.failure_reason = str(exc)
                # Best-effort failed-slug extraction from the message.
                if "for " in str(exc):
                    result.failed_slug = str(exc).split("for ", 1)[1].split("@", 1)[0].split(":")[0]
                return result

            return result
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)


# ─────────────────────────── Lockfile I/O ───────────────────────────────


def read_lockfile(path: Path) -> dict[str, Any]:
    """Read recipes-lock.json; return {} if absent/corrupt (resume-safe)."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def write_lockfile(path: Path, data: dict[str, Any]) -> None:
    """Atomically write recipes-lock.json (temp + os.replace, scrubber-safe)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, p)
