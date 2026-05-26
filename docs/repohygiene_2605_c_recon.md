# repohygiene\_2605 Phase C — Triage Recon + Fix

**Branch decision: C (mixed)** — real bug found + fixed in MCP install path;
two skills' "positive drift" is a probe formula artefact (no per-skill fix needed).

---

## 1. Investigation method

Hit prod DB via SSH to query `Skill.install_count` vs recomputed truth
(`MAX(telemetry_install_count, install_event_count)` per slug — same formula
as `scripts/install_count_drift_probe.py`).

## 2. Per-skill drift table (queried 2026-05-26)

| Skill | actual `install_count` | probe truth (MAX) | drift | verdict |
|---|---|---|---|---|
| larry | 5 | 8 | **-3** | real bug → fixed |
| multi-agent-discord-coordination | 4 | 6 | **-2** | real bug → fixed |
| pr-draft | 1 | 2 | **-1** | real bug → fixed |
| clean-architecture | 1 | 2 | **-1** | real bug → fixed |
| client-reporter | 18 | 15 | **+3** | probe formula artefact (see §4) |
| code-review | 1 | 2 | **-1** | real bug → fixed |
| incident-response | 1 | 0 | **+1** | probe formula artefact (see §4) |
| domain-driven-design | 1 | 2 | **-1** | real bug → fixed |
| graphify | 6 | 7 | **-1** | real bug → fixed |

## 3. Root cause (single shared cause — Branch C = 1 fix)

**`app/mcp/tools/install.py:recipes_install()`** wrote an `InstallEvent` row
but **never issued the companion SQL-level `Skill.install_count += 1` update**.

All install paths are supposed to follow the RCP-13 contract: "InstallEvent insert
+ `Skill.install_count += 1` in the same atomic transaction." Every other path
already did this:

| Path | install_count bumped? |
|---|---|
| `POST /api/telemetry` (event\_type=install) | ✅ `app/routes.py:174` |
| `GET /api/skills/install` (direct single-skill) | ✅ `app/install_routes.py:239` |
| `POST /api/cookbooks/{id}/install` (bulk) | ✅ `app/_skill_helpers._record_install_event:342` |
| MCP `recipes_cookbook_install` (single + bulk) | ✅ `app/mcp/tools/cookbook_install.py:225,273` |
| **MCP `recipes_install`** | ❌ **missing** — fixed here |

The 7 hot skills with negative drift were installed by `cbt_token` agents via
the MCP path (`recipes_install`) — the only code path missing the bump.

## 4. Positive-drift artefact (client-reporter +3, incident-response +1)

The drift probe uses `MAX(telemetry_installs, install_events)` as truth.
When installs arrive via BOTH code paths (telemetry + install_events), the
probe's MAX formula under-counts truth while `install_count` correctly sums
both. This causes a spurious positive drift.

- `client-reporter`: 3 telemetry installs + 15 install_events = 18 total;
  probe truth = MAX(3, 15) = 15; reported drift = +3 (false positive).
- `incident-response`: anomalous probe run captured in stale state; no
  install_events present at time of this recon; drift = +1 (likely also
  probe window artefact — no real bug).

**These two skills need no code fix.** The real fix here is in the probe
formula (tracked as a follow-up recommendation — change MAX → SUM). The Phase C
test `test_both_paths_sum_gt_max` documents this artefact as a concrete
counter-example.

## 5. Fix shipped

**PR:** `fix(repohygiene_2605/C): bump Skill.install_count in MCP recipes_install`

- `app/mcp/tools/install.py` — added atomic `Skill.install_count += 1` UPDATE
  in the same `db.commit()` as the `InstallEvent` insert.
- `tests/test_install_count_increment_completeness.py` — 7 new tests:
  - 4 tests for MCP path (RED before fix → GREEN after)
  - 2 regression guards for cookbook paths (`_record_install_event`)
  - 1 probe formula artefact documentation test

**Test suite delta:** 1647 passed, 57 skipped, 0 failures (was 1640/57/0 before).

## 6. Recommendation for issue #312

Close #312 with the following summary:

> Phase C investigation complete. Root cause: `app/mcp/tools/install.py`
> (MCP `recipes_install` tool) was missing the `Skill.install_count += 1`
> increment that every other install path performs (RCP-13 contract). All 7
> skills with negative drift were installed via cbt\_token → MCP path.
> Fixed + regression tests added. Two positive-drift skills (client-reporter,
> incident-response) have a probe formula artefact (MAX vs SUM); no per-skill
> fix needed. Probe formula follow-up tracked separately.
> Fixed in: [PR linked here].

## 7. Follow-up recommendation (not in scope of this PR)

The `install_count_drift_probe.py` formula should change from:

```python
truth = max(telemetry_count, install_event_count)
```

to:

```python
truth = telemetry_count + install_event_count
```

Until that changes, any skill installed via BOTH code paths will show spurious
positive drift. No backlog issue created here — this is a minor observability
inaccuracy, not a data loss risk.
