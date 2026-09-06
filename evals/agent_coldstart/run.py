#!/usr/bin/env python3
"""LoopSkill agent cold-start benchmark RUNNER.

Drives `evals/agent_coldstart/tasks.yaml` (READ-ONLY fixture — never edited
by this file) against a cold agent harness (hermes | claude | codex | fake),
inside a fresh, isolated $HOME per run, and scores the result via each
task's deterministic `success_check`.

See RUBRIC.md for the scoring contract (pass/fail/error/timeout, cost-to-
value) and GAMEABILITY.md for known residual gaming risks per task.

Usage:
    python evals/agent_coldstart/run.py --harness hermes --task install-named-skill
    python evals/agent_coldstart/run.py --harness fake --task install-named-skill \\
        --fake-cmd 'true' --results-dir /tmp/coldstart-results
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import yaml

TASKS_PATH = Path(__file__).parent / "tasks.yaml"
LOOPSKILL_BASE_DEFAULT = "https://app.loopskill.io"

# Regex for the pre-flight isolation proof: a fresh $HOME must contain zero
# credential-shaped markers before a harness ever runs. Deliberately NOT the
# bare product name: a seeded task file may legitimately SAY "LoopSkill" in
# prose (the publish task's SKILL.md does) — the scan proves a cold START
# (no key material, no pre-wired MCP config), not that the word is unspoken.
SECRET_LEAK_PATTERN = re.compile(
    r"rec_live_|rec_agent_|rec_chef_"  # key material
    r"|loopskill_api_key\s*="  # env-style credential assignment
    r"|app\.loopskill\.io/api/mcp"  # a pre-wired MCP endpoint
    r"|LOOPSKILL_(API_KEY|MASTER_KEY)",  # secret var names
    re.IGNORECASE,
)

# Harness binaries this runner knows how to invoke for real (fake is a
# synthetic in-process harness used only by the test suite — no network, no
# external binary).
REAL_HARNESSES = ("hermes", "claude", "codex")


class ColdstartError(Exception):
    """Runner-level failure that must score as ERROR, never FAIL.

    Per RUBRIC.md: an ERROR means the harness/check could not run for a
    reason unrelated to agent behavior, and must not count against the
    product.
    """


@dataclasses.dataclass
class HarnessResult:
    """What a harness invocation reports back to the runner."""

    tool_calls: int | None
    tokens_in: int | None
    tokens_out: int | None
    transcript_path: str
    timed_out: bool
    error: str | None = None  # non-None => outcome must be 'error'


@dataclasses.dataclass
class RunResult:
    """The full per-run result record (schema fixed by the task brief)."""

    run_id: str
    started_at: str
    host: str
    harness: str
    model: str | None
    task_id: str
    outcome: str  # pass | fail | timeout | error
    tool_calls: int | None
    tokens_in: int | None
    tokens_out: int | None
    wall_seconds: float
    check_exit: int | None
    check_tail: str
    transcript_path: str
    readback: str | None = None  # only set for report-skill-error

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------
# tasks.yaml loading (read-only)
# --------------------------------------------------------------------------


def load_tasks() -> dict[str, Any]:
    """Parse tasks.yaml. Never write to this file from here."""
    with TASKS_PATH.open() as f:
        return yaml.safe_load(f)


def get_task(task_id: str) -> dict[str, Any]:
    suite = load_tasks()
    for task in suite["tasks"]:
        if task["id"] == task_id:
            return task
    raise KeyError(f"unknown task id: {task_id!r}")


def render(text: str, run_id: str) -> str:
    """Substitute the suite's one template var, `{{run_id}}`, literally."""
    return text.replace("{{run_id}}", run_id)


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


def assert_no_secret_leak(home: Path) -> None:
    """Fail closed if the fresh $HOME already contains a LoopSkill marker.

    This is the isolation-contract proof required by the task brief: a cold
    agent must start with zero LoopSkill key/MCP material anywhere under
    $HOME.
    """
    for path in home.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        if SECRET_LEAK_PATTERN.search(text):
            raise ColdstartError(f"isolation violated: secret-like marker found in {path}")


def seed_preconditions(task: dict[str, Any], home: Path, run_id: str) -> None:
    """Materialize any `preconditions[].seed_files` entries under `home`."""
    for pre in task.get("preconditions") or []:
        if not isinstance(pre, dict) or "seed_files" not in pre:
            continue
        for seed in pre["seed_files"]:
            rel = render(seed["path"], run_id).lstrip("~/")
            dest = home / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if "content" in seed:
                dest.write_text(render(seed["content"], run_id))
            elif "content_from" in seed:
                url, _, _fragment = render(seed["content_from"], run_id).partition("#")
                with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
                    body = json.loads(resp.read())
                field = _fragment or "readme"
                dest.write_text(body.get(field) or "")
            else:
                raise ColdstartError(f"seed_files entry has neither content nor content_from: {seed}")


# --------------------------------------------------------------------------
# Provider credential plumbing (hermes harness only)
# --------------------------------------------------------------------------

# Which env var(s) a provider needs, copied ONLY as needed — never the whole
# parent .env (which carries the LoopSkill keys this benchmark must be blind
# to).
PROVIDER_CRED_VARS = {
    "anthropic": ["ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN"],
    "openai": ["OPENAI_API_KEY"],
    "copilot": ["GITHUB_COPILOT_TOKEN", "COPILOT_GITHUB_TOKEN"],
}


def read_parent_model_block(parent_hermes_home: Path) -> dict[str, str]:
    cfg_path = parent_hermes_home / "config.yaml"
    text = cfg_path.read_text()
    doc = yaml.safe_load(text)
    model = doc.get("model", {}) if isinstance(doc, dict) else {}
    return {
        "default": model.get("default", "claude-opus-5"),
        "provider": model.get("provider", "anthropic"),
    }


def read_parent_env_vars(parent_hermes_home: Path, var_names: list[str]) -> dict[str, str]:
    env_path = parent_hermes_home / ".env"
    found: dict[str, str] = {}
    if not env_path.exists():
        return found
    for line in env_path.read_text().splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in var_names and value.strip():
            found[key] = value
    return found


def _copy_claude_oauth_credentials(home: Path, provider: str) -> bool:
    """Fallback auth path when no static provider API key env var exists.

    This box's parent Hermes authenticates Anthropic via OAuth (Claude Code
    login) rather than a static ANTHROPIC_API_KEY/ANTHROPIC_TOKEN env var
    (both are present-but-empty in .env here). auth.json's credential_pool
    entry for `anthropic` has `source: claude_code` and stores only a
    fingerprint — the actual OAuth token lives in
    `~/.claude/.credentials.json` (`claudeAiOauth.accessToken`), which
    Hermes/Claude Code read directly. We copy ONLY that one small
    credentials file (not the rest of ~/.claude, which holds unrelated
    session/project state) into the isolated $HOME so the cold agent
    authenticates the SAME way the parent does, without ever touching
    LoopSkill material. Returns True if a credential was copied.
    """
    if provider != "anthropic":
        return False
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if not creds_path.exists():
        return False
    try:
        payload = json.loads(creds_path.read_text())
    except json.JSONDecodeError:
        return False
    if "claudeAiOauth" not in payload:
        return False
    dest_dir = home / ".claude"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / ".credentials.json"
    dest_path.write_text(json.dumps(payload, indent=2))
    dest_path.chmod(0o600)
    return True


def _copy_codex_oauth_credentials(home: Path) -> bool:
    """Same rationale as `_copy_claude_oauth_credentials`, for Codex.

    Codex stores its OpenAI OAuth session in `~/.codex/auth.json`. Copy
    only that file into the isolated $HOME's `.codex/` dir.
    """
    creds_path = Path.home() / ".codex" / "auth.json"
    if not creds_path.exists():
        return False
    try:
        payload = json.loads(creds_path.read_text())
    except json.JSONDecodeError:
        return False
    dest_dir = home / ".codex"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / "auth.json"
    dest_path.write_text(json.dumps(payload, indent=2))
    dest_path.chmod(0o600)
    return True


def write_isolated_hermes_home(tmp_home: Path, parent_hermes_home: Path) -> tuple[Path, str]:
    """Build a minimal, LoopSkill-blind HERMES_HOME under the fresh $HOME.

    Returns (hermes_home_path, model_string_used).
    """
    hermes_home = tmp_home / ".hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)

    model_block = read_parent_model_block(parent_hermes_home)
    provider = model_block["provider"]
    var_names = PROVIDER_CRED_VARS.get(provider, [])
    creds = read_parent_env_vars(parent_hermes_home, var_names)

    config = {
        "model": {"default": model_block["default"], "provider": provider},
        "toolsets": ["hermes-cli"],
    }
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))

    if creds:
        env_lines = [f"{k}={v}" for k, v in creds.items()]
        (hermes_home / ".env").write_text("\n".join(env_lines) + "\n")
    else:
        _copy_claude_oauth_credentials(tmp_home, provider)

    return hermes_home, model_block["default"]


# --------------------------------------------------------------------------
# Harness invocations
# --------------------------------------------------------------------------


def _clip_tail(text: str, n: int = 600) -> str:
    return text[-n:] if len(text) > n else text


def run_fake_harness(prompt: str, home: Path, max_minutes: int, cwd: Path) -> HarnessResult:
    """Synthetic harness for the test suite: no network, no real binary.

    Behavior is entirely controlled by env vars so tests can exercise every
    outcome branch deterministically:
      FAKE_HARNESS_CMD          shell command to run (default: 'true')
      FAKE_HARNESS_TOOL_CALLS   integer to report as tool_calls (default 0)
      FAKE_HARNESS_TOKENS_IN    integer or unset (-> null)
      FAKE_HARNESS_TOKENS_OUT   integer or unset (-> null)
    """
    cmd = os.environ.get("FAKE_HARNESS_CMD", "true")
    transcript_path = home / "fake_harness_transcript.txt"
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["FAKE_HARNESS_PROMPT"] = prompt
    timed_out = False
    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=max_minutes * 60,
        )
        transcript_path.write_text((proc.stdout or "") + (proc.stderr or ""))
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        transcript_path.write_text((exc.stdout or "") + (exc.stderr or ""))

    def _opt_int(name: str) -> int | None:
        raw = os.environ.get(name)
        return int(raw) if raw is not None else None

    return HarnessResult(
        tool_calls=_opt_int("FAKE_HARNESS_TOOL_CALLS") or 0,
        tokens_in=_opt_int("FAKE_HARNESS_TOKENS_IN"),
        tokens_out=_opt_int("FAKE_HARNESS_TOKENS_OUT"),
        transcript_path=str(transcript_path),
        timed_out=timed_out,
    )


def _count_hermes_tool_calls(hermes_home: Path) -> int | None:
    """Best-effort tool-call count from a HERMES_HOME's own session state.

    Documented location: HERMES_HOME/state.db (sqlite), table `tool_calls`
    if present, else HERMES_HOME/sessions/*.jsonl counting lines whose JSON
    has a `type` of `tool_use` / `tool_call`. Returns None (not an error) if
    neither surface is found — tokens/tool_calls are optional per RUBRIC.md.
    """
    import sqlite3

    db_path = hermes_home / "state.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%tool_call%'")
                tables = [r[0] for r in cur.fetchall()]
                total = 0
                for t in tables:
                    cur.execute(f"SELECT COUNT(*) FROM {t}")  # noqa: S608 (table name from sqlite_master, not user input)
                    total += cur.fetchone()[0]
                if tables:
                    return total
            finally:
                conn.close()
        except sqlite3.Error:
            # Rationale: state.db schema is a Hermes internal implementation
            # detail this benchmark does not control; a read failure here
            # must degrade to "unknown", not crash the run.
            pass

    sessions_dir = hermes_home / "sessions"
    if sessions_dir.is_dir():
        count = 0
        found_any = False
        for jf in sessions_dir.rglob("*.jsonl"):
            found_any = True
            for line in jf.read_text(errors="ignore").splitlines():
                if '"tool_use"' in line or '"tool_call"' in line or '"type": "tool"' in line:
                    count += 1
        if found_any:
            return count
    return None


def run_hermes_harness(prompt: str, home: Path, max_minutes: int, parent_hermes_home: Path) -> HarnessResult:
    hermes_home, model = write_isolated_hermes_home(home, parent_hermes_home)
    assert_no_secret_leak(home)  # config/env we just wrote must itself be clean

    transcript_path = home / "hermes_transcript.txt"
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["HERMES_HOME"] = str(hermes_home)
    timed_out = False
    stdout = ""
    stderr = ""
    try:
        proc = subprocess.run(
            ["hermes", "chat", "-q", prompt],
            cwd=str(home),
            env=env,
            capture_output=True,
            text=True,
            timeout=max_minutes * 60,
        )
        stdout, stderr = proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="ignore")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="ignore")
    transcript_path.write_text(stdout + "\n---stderr---\n" + stderr)

    tool_calls = _count_hermes_tool_calls(hermes_home)
    return HarnessResult(
        tool_calls=tool_calls,
        tokens_in=None,
        tokens_out=None,
        transcript_path=str(transcript_path),
        timed_out=timed_out,
    )


def run_claude_harness(prompt: str, home: Path, max_minutes: int) -> HarnessResult:
    cwd = home / "work"
    cwd.mkdir(parents=True, exist_ok=True)
    _copy_claude_oauth_credentials(home, "anthropic")
    transcript_path = home / "claude_transcript.json"
    env = dict(os.environ)
    env["HOME"] = str(home)
    timed_out = False
    stdout = ""
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=max_minutes * 60,
        )
        stdout = proc.stdout or ""
        transcript_path.write_text(stdout + "\n---stderr---\n" + (proc.stderr or ""))
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        out = exc.stdout
        stdout = out if isinstance(out, str) else (out or b"").decode(errors="ignore")
        transcript_path.write_text(stdout)

    tool_calls: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    harness_error: str | None = None
    if stdout.strip():
        try:
            payload = json.loads(stdout)
            tool_calls = payload.get("num_turns")
            usage = payload.get("usage") or {}
            tokens_in = usage.get("input_tokens")
            tokens_out = usage.get("output_tokens")
            if payload.get("is_error"):
                # Rationale: `claude -p` reports HARNESS failures (account
                # session limit, auth, quota) as `is_error: true` with the
                # message in `result`. That is not a product signal — per
                # RUBRIC it must be outcome `error` (excluded), never `fail`.
                harness_error = f"claude harness error: {str(payload.get('result') or '')[:200]}"
        except json.JSONDecodeError:
            # Rationale: --output-format json can still emit partial/non-JSON
            # output on a killed/crashed process; degrade to unknown metrics
            # rather than treating a parse failure as a benchmark crash.
            pass

    return HarnessResult(
        tool_calls=tool_calls,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        transcript_path=str(transcript_path),
        timed_out=timed_out,
        error=harness_error,
    )


def run_codex_harness(prompt: str, home: Path, max_minutes: int) -> HarnessResult:
    cwd = home / "work"
    cwd.mkdir(parents=True, exist_ok=True)
    _copy_codex_oauth_credentials(home)
    transcript_path = home / "codex_transcript.jsonl"
    env = dict(os.environ)
    env["HOME"] = str(home)
    timed_out = False
    stdout = ""
    try:
        proc = subprocess.run(
            [
                "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C",
                str(cwd),
                prompt,
            ],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=max_minutes * 60,
        )
        stdout = proc.stdout or ""
        transcript_path.write_text(stdout + "\n---stderr---\n" + (proc.stderr or ""))
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        out = exc.stdout
        stdout = out if isinstance(out, str) else (out or b"").decode(errors="ignore")
        transcript_path.write_text(stdout)

    tool_calls = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    saw_event = False
    # Discrete tool-call unit for this harness: each completed
    # command_execution, file_change, web_search, or search item is one
    # tool invocation (mirrors what the transcript actually records as
    # distinct agent actions). Nested under `item.type` for
    # `item.completed`/`item.started` events; top-level `type` is only
    # the envelope kind (thread.started, turn.completed, etc.).
    TOOL_ITEM_TYPES = {"command_execution", "file_change", "web_search", "search"}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        saw_event = True
        envelope_type = str(event.get("type") or "")
        item = event.get("item") or {}
        item_type = str(item.get("type") or "")
        if envelope_type == "item.completed" and item_type in TOOL_ITEM_TYPES:
            tool_calls += 1
        usage = event.get("usage") or (event.get("msg") or {}).get("usage")
        if usage:
            tokens_in = usage.get("input_tokens", tokens_in)
            tokens_out = usage.get("output_tokens", tokens_out)

    return HarnessResult(
        tool_calls=tool_calls if saw_event else None,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        transcript_path=str(transcript_path),
        timed_out=timed_out,
    )


HARNESS_BINARY = {"hermes": "hermes", "claude": "claude", "codex": "codex"}


def invoke_harness(
    harness: str, prompt: str, home: Path, max_minutes: int, parent_hermes_home: Path
) -> HarnessResult:
    if harness == "fake":
        cwd = home / "work"
        cwd.mkdir(parents=True, exist_ok=True)
        return run_fake_harness(prompt, home, max_minutes, cwd)
    if harness in HARNESS_BINARY and shutil.which(HARNESS_BINARY[harness]) is None:
        return HarnessResult(
            tool_calls=None,
            tokens_in=None,
            tokens_out=None,
            transcript_path="",
            timed_out=False,
            error=f"harness binary not found on PATH: {HARNESS_BINARY[harness]}",
        )
    if harness == "hermes":
        return run_hermes_harness(prompt, home, max_minutes, parent_hermes_home)
    if harness == "claude":
        return run_claude_harness(prompt, home, max_minutes)
    if harness == "codex":
        return run_codex_harness(prompt, home, max_minutes)
    raise ColdstartError(f"unknown harness: {harness!r}")


# --------------------------------------------------------------------------
# success_check execution
# --------------------------------------------------------------------------


def run_success_check(
    check_script: str,
    home: Path,
    run_id: str,
    task_id: str,
    loopskill_base: str,
    max_minutes: int,
) -> tuple[int, str]:
    """Run success_check as `bash -c`. Returns (exit_code, combined_tail)."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["LOOPSKILL_BASE"] = loopskill_base
    env["RUN_ID"] = run_id
    env["TASK_ID"] = task_id
    proc = subprocess.run(
        ["bash", "-c", check_script],
        cwd=str(home),
        env=env,
        capture_output=True,
        text=True,
        timeout=max(60, max_minutes * 60),
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, _clip_tail(combined)


def readback_skill_error_report(run_id: str, wall_seconds: float) -> str:
    """Optional server-side read-back for `report-skill-error`.

    Per GAMEABILITY.md #1 this check is otherwise self-attested. If
    LOOPSKILL_MASTER_KEY is set, attempt an authenticated read-back of
    recently created skill_error_reports; if no such public/admin GET route
    exists (verified: it does not, as of this runner's authoring — see
    app/skill_error_routes.py and app/admin_routes.py), record
    'unavailable' rather than inventing an endpoint.
    """
    if not os.environ.get("LOOPSKILL_MASTER_KEY"):
        return "not_attempted"
    # No admin/public GET route for skill_error_reports exists in this
    # codebase (confirmed by inspection of app/skill_error_routes.py and
    # app/admin_routes.py at authoring time). Recording explicitly rather
    # than fabricating a call to a nonexistent endpoint.
    return "unavailable"


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def default_parent_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def run_task(
    harness: str,
    task_id: str,
    results_dir: Path,
    loopskill_base: str = LOOPSKILL_BASE_DEFAULT,
    parent_hermes_home: Path | None = None,
    run_id: str | None = None,
) -> RunResult:
    task = get_task(task_id)
    run_id = run_id or uuid.uuid4().hex[:8]
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    max_minutes = int(task["max_minutes"])
    prompt = render(task["prompt"], run_id)
    parent_hermes_home = parent_hermes_home or default_parent_hermes_home()

    tmp_home = Path(tempfile.mkdtemp(prefix="coldstart-"))
    start = time.monotonic()
    outcome = "error"
    tool_calls: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    check_exit: int | None = None
    check_tail = ""
    transcript_path = ""
    readback: str | None = None

    try:
        assert_no_secret_leak(tmp_home)  # trivially true on a brand-new tempdir
        seed_preconditions(task, tmp_home, run_id)
        assert_no_secret_leak(tmp_home)  # seed files must not themselves leak secrets

        harness_result = invoke_harness(harness, prompt, tmp_home, max_minutes, parent_hermes_home)
        transcript_path = harness_result.transcript_path
        tool_calls = harness_result.tool_calls
        tokens_in = harness_result.tokens_in
        tokens_out = harness_result.tokens_out

        if harness_result.error:
            outcome = "error"
            check_tail = harness_result.error
        elif harness_result.timed_out:
            outcome = "timeout"
            check_tail = "harness exceeded max_minutes"
        else:
            try:
                check_exit, check_tail = run_success_check(
                    task["success_check"],
                    tmp_home,
                    run_id,
                    task_id,
                    loopskill_base,
                    max_minutes,
                )
                outcome = "pass" if check_exit == 0 else "fail"
            except subprocess.TimeoutExpired:
                outcome = "error"
                check_tail = "success_check itself timed out"
            except OSError as exc:
                # Rationale: success_check failing to even execute (missing
                # bash, permissions, etc.) is a harness/runner problem, not
                # an agent failure — must score as error per RUBRIC.md.
                outcome = "error"
                check_tail = f"success_check could not execute: {exc}"

            if task_id == "report-skill-error" and outcome == "pass":
                readback = readback_skill_error_report(run_id, time.monotonic() - start)
    except ColdstartError as exc:
        outcome = "error"
        check_tail = str(exc)
    finally:
        wall_seconds = time.monotonic() - start

    result = RunResult(
        run_id=run_id,
        started_at=started_at,
        host=os.uname().nodename,
        harness=harness,
        model=None,
        task_id=task_id,
        outcome=outcome,
        tool_calls=tool_calls,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        wall_seconds=round(wall_seconds, 2),
        check_exit=check_exit,
        check_tail=check_tail,
        transcript_path=transcript_path,
        readback=readback,
    )

    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = results_dir / f"{harness}_{task_id}_{run_id}.json"
    out_path.write_text(json.dumps(result.to_dict(), indent=2))
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", required=True, choices=["hermes", "claude", "codex", "fake"])
    parser.add_argument("--task", required=True, help="task id from tasks.yaml")
    parser.add_argument(
        "--results-dir",
        default=str(Path(__file__).parent / "results"),
        help="directory to write the per-run result JSON into",
    )
    parser.add_argument("--loopskill-base", default=LOOPSKILL_BASE_DEFAULT)
    parser.add_argument("--run-id", default=None, help="override the auto-generated 8-hex run id")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    result = run_task(
        harness=args.harness,
        task_id=args.task,
        results_dir=Path(args.results_dir),
        loopskill_base=args.loopskill_base,
        run_id=args.run_id,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.outcome in ("pass", "error") else 1


if __name__ == "__main__":
    sys.exit(main())
