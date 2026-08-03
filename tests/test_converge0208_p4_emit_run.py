"""converge_0208 P4 — the missing telemetry emitter, `scripts/loopskill-emit-run.sh`.

`docs/SELF_HOST.md` has published this interface since activate_0701 Phase T:

    ./loopskill-emit-run.sh <loop_slug> <outcome> [accepted] [cost_usd] [duration_s] [detail]

…and the script it points at has never existed in this repo. A stranger
following SELF_HOST.md hits a `No such file or directory` at the last step of
the loop path, which is exactly why `loop_runs` has one row, ever.

Two properties are load-bearing and get their own tests:

1. **It never fails its caller.** A telemetry emitter that breaks the loop it
   is measuring is worse than no telemetry. Every failure mode — missing args,
   unwritable spool, bad outcome — exits 0.
2. **It writes through the EXISTING spool.** `loopskill-collect-reports.py`
   already batches `~/.hermes/loopskill/outbox/*.json` into one
   `POST /api/sync-report`. There is no second telemetry path and this must not
   invent one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EMIT = REPO_ROOT / "scripts" / "loopskill-emit-run.sh"

# Mirrors app/services/sync_report.py ingest + LoopRun columns.
_OUTCOMES = ("success", "failure", "budget_stop", "max_turns_stop")


def _run(tmp_home: Path, *args: str, env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    env["HOME"] = str(tmp_home)
    env.pop("LOOPSKILL_OUTBOX", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["sh", str(EMIT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _outbox(tmp_home: Path) -> Path:
    return tmp_home / ".hermes" / "loopskill" / "outbox"


def _spooled(tmp_home: Path) -> list[dict]:
    box = _outbox(tmp_home)
    if not box.is_dir():
        return []
    return [json.loads(p.read_text()) for p in sorted(box.glob("*.json"))]


def test_emitter_exists_and_is_executable():
    """The script SELF_HOST.md documents must actually ship in scripts/."""
    assert EMIT.is_file(), f"{EMIT} does not exist — SELF_HOST.md points at vapour"
    assert os.access(EMIT, os.X_OK), f"{EMIT} is not executable"


def test_documented_signature_spools_every_field(tmp_path):
    """The published 6-arg positional contract, end to end."""
    r = _run(
        tmp_path,
        "p4-proof-loop",
        "success",
        "true",
        "0.0123",
        "42",
        "all good",
    )
    assert r.returncode == 0, r.stderr
    runs = _spooled(tmp_path)
    assert len(runs) == 1
    rec = runs[0]
    assert rec["loop_slug"] == "p4-proof-loop"
    assert rec["outcome"] == "success"
    assert rec["accepted_change"] is True
    assert rec["cost_usd"] == pytest.approx(0.0123)
    assert rec["duration_seconds"] == 42
    assert rec["detail"] == "all good"
    assert rec["instance_key"]
    assert rec["started_at"]


def test_optional_args_default_and_stay_null(tmp_path):
    """Only slug + outcome are required; the rest must be JSON null, not ''."""
    r = _run(tmp_path, "p4-proof-loop", "failure")
    assert r.returncode == 0, r.stderr
    rec = _spooled(tmp_path)[0]
    assert rec["accepted_change"] is False
    assert rec["cost_usd"] is None
    assert rec["duration_seconds"] is None
    assert rec["detail"] is None


def test_spools_into_the_path_the_collector_reads(tmp_path):
    """No second telemetry path: the collector globs ~/.hermes/loopskill/outbox/*.json."""
    _run(tmp_path, "p4-proof-loop", "success")
    box = _outbox(tmp_path)
    assert box.is_dir()
    assert list(box.glob("*.json")), f"nothing in the collector's spool dir {box}"


@pytest.mark.parametrize("outcome", _OUTCOMES)
def test_accepts_every_server_side_outcome(tmp_path, outcome):
    _run(tmp_path, "slug", outcome)
    assert _spooled(tmp_path)[0]["outcome"] == outcome


# ── "never fails its caller" ─────────────────────────────────────────────────


def test_missing_arguments_never_fail_the_caller(tmp_path):
    """A misuse must warn on stderr and exit 0 — never break the loop."""
    for args in ([], ["only-a-slug"]):
        r = _run(tmp_path, *args)
        assert r.returncode == 0, f"args={args} exited {r.returncode}"
        assert r.stderr.strip(), "a misuse must at least be loud on stderr"
    assert _spooled(tmp_path) == [], "a misuse must not spool a junk record"


def test_unknown_outcome_never_fails_the_caller(tmp_path):
    r = _run(tmp_path, "slug", "banana")
    assert r.returncode == 0
    assert r.stderr.strip()
    assert _spooled(tmp_path) == []


def test_unwritable_spool_never_fails_the_caller(tmp_path):
    """Read-only HOME (full disk, wrong perms) — still exit 0."""
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        r = _run(ro, "slug", "success")
        assert r.returncode == 0, r.stderr
    finally:
        ro.chmod(0o700)


def test_emitter_does_no_network_io(tmp_path):
    """The emitter spools; the collector POSTs. Blocking on the network here
    would make a loop's runtime hostage to the telemetry endpoint."""
    body = EMIT.read_text()
    for forbidden in ("curl", "wget", "urllib", "nc "):
        assert forbidden not in body, f"emitter must not do network IO ({forbidden!r})"


# ── correctness of the emitted JSON ──────────────────────────────────────────


def test_detail_with_json_hostile_characters_stays_valid_json(tmp_path):
    r'''Quotes, backslashes, newlines and tabs must be escaped, not injected.

    The pre-existing host copy of this script interpolated $DETAIL straight
    into a Python """…""" heredoc — a detail containing a quote run or a
    newline produced either invalid JSON or arbitrary Python.
    '''
    nasty = 'he said "hi"\\ \n\ttab & <&|; $(echo pwned) `echo pwned` """'
    r = _run(tmp_path, "slug", "failure", "false", "null", "null", nasty)
    assert r.returncode == 0, r.stderr
    rec = _spooled(tmp_path)[0]  # json.loads would raise if it were malformed
    assert "pwned" not in json.dumps(rec).replace("$(echo pwned)", "").replace(
        "`echo pwned`", ""
    ), "command substitution must not be evaluated"
    assert '"hi"' in rec["detail"]
    assert "\n" in rec["detail"]


def test_detail_is_capped_at_the_server_field_limit(tmp_path):
    """MAX_FIELD_LEN is 2000 server-side; don't ship a 1 MB detail up the wire."""
    _run(tmp_path, "slug", "success", "false", "null", "null", "x" * 9000)
    assert len(_spooled(tmp_path)[0]["detail"]) <= 2000


def test_spool_write_is_atomic(tmp_path):
    """A half-written *.json is a record the collector will drop on the floor.

    Write-then-rename: no temp file may ever carry the .json suffix the
    collector globs.
    """
    body = EMIT.read_text()
    assert "mv " in body, "expected write-to-temp + mv (atomic publish)"
    _run(tmp_path, "slug", "success")
    leftovers = [p.name for p in _outbox(tmp_path).iterdir() if not p.name.endswith(".json")]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_two_emits_in_the_same_second_do_not_collide(tmp_path):
    """Loops fire in bursts; a second-resolution filename alone loses records."""
    for _ in range(5):
        _run(tmp_path, "slug", "success")
    assert len(_spooled(tmp_path)) == 5


def test_outbox_is_overridable_for_non_hermes_hosts(tmp_path):
    box = tmp_path / "custom-outbox"
    r = _run(tmp_path, "slug", "success", env_extra={"LOOPSKILL_OUTBOX": str(box)})
    assert r.returncode == 0, r.stderr
    assert list(box.glob("*.json"))


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    r = subprocess.run(
        ["shellcheck", "-S", "style", str(EMIT)], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stdout + r.stderr
