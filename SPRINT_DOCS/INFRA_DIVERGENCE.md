# INFRA_DIVERGENCE — Phase H pre-flight

Date: 2026-05-06
Branch: `v7/phase-H-dogfood`
Diagnose script: `scripts/diagnose_chef_pipeline_v7.py`
Diagnose output: `SPRINT_DOCS/CHEF_DIAGNOSIS.json`

## Summary

The Phase H pre-flight diagnostic flagged one **red** check that is
infra-level (not skill-solvable from inside this repo): provisioning
credentials for the new `wisechef-maestro` CX23 box are unavailable. The
phase therefore scope-shifted from scenario A (build a cookbook fix that
resolves Chef's pipeline failure on a freshly provisioned client box) to
scenario B (ship the canonical `interplus-deploy-v1` sub-recipe + local
smoke install only).

## Red checks

### `hcloud_credentials` (red — provisioning blocked)

- `HCLOUD_TOKEN` is absent from the environment.
- `~/.hcloud/config.json` does not exist.
- Bitwarden is locked in the agent's session (cannot unlock interactively).
- Operator constraint: do **not** modify `wisechef-agents` or
  `wisechef-hq` (read-only access only).

**Effect**: cannot run `hcloud server create --type cx23 --image …` to
spin a fresh `wisechef-maestro` box, which means scenario A (full
end-to-end deploy of the maestro stack to a clean box and re-engage the
content pipeline from there) cannot complete inside the phase budget.

**Adam-side handoff**: when Bitwarden is unlocked, run
```
hcloud server create --name wisechef-maestro --type cx23 \
  --image debian-12 --ssh-key adam@wisechef --location nbg1
```
then `ansible-playbook playbooks/maestro_bootstrap.yml -i inventory/hetzner.yaml`.

## Skipped checks (no creds in this phase)

- `portal_deploy` — outbound HTTPS blocked from the agent sandbox (curl rc=6).
- `og_image_route` — same outbound block.
- `cloudflare_articles` — Cloudflare API token not provisioned in this phase.
- `chef_ack_discord` — `DISCORD_BOT_TOKEN` not in env.

These are deliberately skipped, not silently failing. A future operator
re-running the diagnose with the right env will get colored results
without code changes.

## Green checks

- `resend_quota` — no Resend 429/quota errors in the last 24h on
  `wisechef-hq` (ssh + journalctl grep).
- `repo_drift` — branch HEAD matches `origin/main` HEAD; no rebase drift.

## Phase H scope after divergence

**In:**

1. `scripts/diagnose_chef_pipeline_v7.py` — repeatable read-only triage.
2. `cookbooks/interplus-deploy-v1.yaml` — canonical 5-skill, 5-step
   sub-recipe spec for "deploy WiseChef agent stack to a small business
   client".
3. `tests/test_phase_h_smoke.py` — local in-memory smoke covering the
   recipes_install + recipes_search + cookbook install loop.
4. `SPRINT_DOCS/CHEF_DIAGNOSIS.json` — frozen diagnose output.
5. This file.

**Out (deferred to Adam-side manual step or next phase):**

- Provisioning `wisechef-maestro` on Hetzner.
- Promoting interplus-deploy-v1 to `wisechef-agents` (read-only this phase).
- Discord post to `#tori` (no bot token in env — see SUBAGENT_H_OUTPUT).
- Authoring a Chef-specific fix-skill (cannot be validated without
  reaching the Chef pipeline; Resend/repo are green so the failure mode
  to fix is not currently observable from inside the sandbox).

## Paperclip ticket (suggested)

Title: `infra: provision wisechef-maestro CX23 + unblock Phase H scenario A`
Body: link this file + `CHEF_DIAGNOSIS.json`. Owner: Adam. Blocking on:
Bitwarden unlock + manual `hcloud server create`.
