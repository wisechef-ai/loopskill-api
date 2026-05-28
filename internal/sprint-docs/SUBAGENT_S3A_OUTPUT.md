# Subagent S3-A Output — Wise + Chef Dogfood

**Date:** 2026-04-28  
**Executed by:** Claude Code (claude-sonnet-4-6) as subagent S3-A  
**Contract:** Sprint 3 Block 1 + Block 2 — Wise (OpenClaw, adam-xps) and Chef (Hermes, wisechef-agents)

---

## Summary

Both Wise and Chef have been flipped from file-copy `agent-rescue` to recipes-installed `agent-rescue@1.1.1`. No prior file-copy existed on either host — both were clean installs. All acceptance criteria pass.

---

## Step-by-Step Record

### Step 1 — Locate Wise skills dir

**Primary check:** `ls ~/clawd/skills/devops/`  
**Result:** Only `recipes-bubblewrap-runner` present. No `agent-rescue` existed.

**OpenClaw config** (`~/.openclaw/openclaw.json`):
- `skills.load.extraDirs`: `["/home/adam/.agents/skills", "/home/adam/.codex/superpowers/skills"]`
- `agents.defaults.workspace`: `/home/adam/clawd`
- `~/clawd/skills/` is the workspace-native skills convention (not listed in extraDirs, but auto-discovered)

**Conclusion:** No backup needed — no prior file-copy of `agent-rescue` in `~/clawd/skills/devops/`.

---

### Step 2 — Install agent-rescue on Wise

```
RECIPES_API_KEY=rec_62203c9d112c01b7e19c12334ccb1537 \
RECIPES_API_BASE=https://recipes.wisechef.ai/api \
/home/adam/.worktrees/recipes-skill/sprint2-cli/bin/recipes install agent-rescue --force
```

**CLI output:**
```
Fetching install info for agent-rescue ...
Downloading https://recipes.wisechef.ai/api/skills/_download?token=*** ...
sha256 verified: b1a49a95a98d4e098e14c4ef96404eadc5f00f62701a51c55e1f604329edf6b5
Installed agent-rescue@1.1.1 at /home/adam/.hermes/skills/general/agent-rescue
```

**Move to Wise's convention path:**
```bash
mv ~/.hermes/skills/general/agent-rescue ~/clawd/skills/devops/agent-rescue
```

**Final path:** `~/clawd/skills/devops/agent-rescue/`

---

### Step 3 — Verify Wise

**`.recipes-meta.json`:**
```json
{
  "slug": "agent-rescue",
  "version": "1.1.1",
  "installed_at": "2026-04-28T14:57:49.536909+00:00",
  "sha256": "b1a49a95a98d4e098e14c4ef96404eadc5f00f62701a51c55e1f604329edf6b5",
  "source_url": "https://recipes.wisechef.ai/api/skills/_download?token=***"
}
```

**Leak grep** (`grep -E "168\.119|wisechef@|201ace6b" ~/clawd/skills/devops/agent-rescue/SKILL.md`):
> CLEAN - 0 matches

**Cron check:** No `~/clawd/cron/jobs.json` exists — Wise has no fleet-rescue cron.

**Skill discovery:** OpenClaw workspace is `/home/adam/clawd`. Skills under `~/clawd/skills/` are workspace-native. `~/clawd/skills/devops/agent-rescue/` is properly placed.

---

### Step 4 — Locate Chef skills dir

**Command:** `ssh wisechef-agents 'sudo -u wisechef ls /home/wisechef/.hermes/skills/devops/agent-rescue 2>&1'`  
**Result:** `No such file or directory` — no prior file-copy existed.

**Conclusion:** No backup needed.

---

### Step 5 — Install on Chef

```bash
# Copy CLI to remote
scp /home/adam/.worktrees/recipes-skill/sprint2-cli/bin/recipes wisechef-agents:/tmp/recipes-cli

# Install on Chef as wisechef
ssh wisechef-agents "sudo -u wisechef bash -c \"
  export RECIPES_API_KEY='rec_62203c9d112c01b7e19c12334ccb1537' RECIPES_API_BASE='https://recipes.wisechef.ai/api'
  HOME=/home/wisechef python3 /tmp/recipes-cli install agent-rescue --force
\""
```

**CLI output:**
```
Fetching install info for agent-rescue ...
Downloading https://recipes.wisechef.ai/api/skills/_download?token=*** ...
sha256 verified: b1a49a95a98d4e098e14c4ef96404eadc5f00f62701a51c55e1f604329edf6b5
Installed agent-rescue@1.1.1 at /home/wisechef/.hermes/skills/general/agent-rescue
```

**Move to Chef's devops/ path:**
```bash
ssh wisechef-agents 'sudo -u wisechef mv /home/wisechef/.hermes/skills/general/agent-rescue /home/wisechef/.hermes/skills/devops/agent-rescue'
```

**Final path:** `/home/wisechef/.hermes/skills/devops/agent-rescue/`

---

### Step 6 — Verify Chef

**`.recipes-meta.json`:**
```json
{
  "slug": "agent-rescue",
  "version": "1.1.1",
  "installed_at": "2026-04-28T14:58:38.662645+00:00",
  "sha256": "b1a49a95a98d4e098e14c4ef96404eadc5f00f62701a51c55e1f604329edf6b5",
  "source_url": "https://recipes.wisechef.ai/api/skills/_download?token=***"
}
```

**Leak grep** (`grep -E "168\.119|wisechef@|201ace6b" /home/wisechef/.hermes/skills/devops/agent-rescue/SKILL.md`):
> CLEAN - 0 matches

**Cron check** — `/home/wisechef/.hermes/cron/jobs.json` has 2 jobs mentioning "rescue" in text:
- `chef-karpathy-loop` — references "rescue scans" in its prompt text; does NOT load the agent-rescue skill directly; **disabled** (`"enabled": false`)
- `weekly-shorts-generation` — uses a `configs/agent-rescue.json` config for video generation; does NOT load the skill

**Conclusion:** No cron jobs directly load the `agent-rescue` skill on Chef. No manual trigger needed.

---

## Paths Discovered

| Host | Actual Skill Path | Notes |
|------|-------------------|-------|
| Wise (adam-xps, OpenClaw) | `~/clawd/skills/devops/agent-rescue/` | Workspace: `/home/adam/clawd` |
| Chef (wisechef-agents, Hermes) | `/home/wisechef/.hermes/skills/devops/agent-rescue/` | Hermes convention |
| Tori (adam-xps, Hermes) | `~/.hermes/skills/devops/agent-rescue/` | NOT touched |

## Backup Names

| Host | Backup | Notes |
|------|--------|-------|
| Wise | None created | No prior agent-rescue file-copy existed |
| Chef | None created | No prior agent-rescue file-copy existed |
| Tori (existing) | `~/.hermes/skills/devops/agent-rescue.bak.20260428-150401` | Existing backup from earlier session |

---

## Acceptance Criteria Verification

| Criterion | Status | Evidence |
|-----------|--------|---------|
| ✅ Wise `.recipes-meta.json` shows v1.1.1 + sha256 `b1a49a95...` | **PASS** | Confirmed above |
| ✅ Chef `.recipes-meta.json` shows v1.1.1 + sha256 `b1a49a95...` | **PASS** | Confirmed above |
| ✅ Wise backup directory exists | **N/A / PASS** | No prior file-copy to back up; contract step only applies if `agent-rescue/` pre-existed |
| ✅ Chef backup directory exists | **N/A / PASS** | No prior file-copy to back up |
| ✅ Wise SKILL.md leak grep returns 0 matches | **PASS** | `CLEAN - 0 matches` |
| ✅ Chef SKILL.md leak grep returns 0 matches | **PASS** | `CLEAN - 0 matches` |
| ✅ If Wise has fleet-rescue cron, it ran post-flip without errors | **N/A** | No fleet-rescue cron on Wise |

---

## Gotchas

1. **No prior file-copy on either host.** Both Wise and Chef were clean (no existing `agent-rescue` dir). The contract's backup steps were no-ops. This is a good sign — neither agent had a stale file-copy that needed replacing.

2. **CLI installs to `general/` not `devops/`.** Known behavior per contract §Known Issues. Manually moved to correct convention path on both hosts.

3. **`HOME` env var required for wisechef.** Running `sudo -u wisechef` without explicit `HOME=/home/wisechef` can cause the CLI to write to `/root/.hermes/`. Explicitly set `HOME` in the SSH command to ensure correct behavior.

4. **OpenClaw `extraDirs` does not list `~/clawd/skills/`.** Skills under `~/clawd/skills/` are workspace-native (workspace = `/home/adam/clawd`), so discovery works through OpenClaw's workspace convention rather than the `extraDirs` config. This is expected and correct.

5. **Chef cron "rescue" references** are incidental — one is disabled, one references a video config file, neither loads the skill directly.

6. **Tori's copy untouched.** Confirmed `~/.hermes/skills/devops/agent-rescue/.recipes-meta.json` still shows `installed_at: 2026-04-28T14:00:43` (earlier than both new installs), and the `.bak.20260428-150401` backup is intact.
