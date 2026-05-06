# V7 Phase H — Subagent output

Branch: `v7/phase-H-dogfood`
Date: 2026-05-06
Decision: **scenario B (infra-level)** — see `INFRA_DIVERGENCE.md`.

## Summary

1. Pre-flight diagnose ran; one red (hcloud creds), no other reds. Decision
   was **infra-level**, scope shifted to canonical sub-recipe + local
   smoke.
2. Authored `cookbooks/interplus-deploy-v1.yaml` (5 skills, 5 steps) per
   the phase brief.
3. Wrote `tests/test_phase_h_smoke.py` (6 tests, all passing) covering:
   schema validation, search hits for "daily briefing" → maestro and
   "deploy WiseChef" → interplus-deploy-v1, signed-tarball install for
   maestro, not_found for unknown slugs, full install loop over the
   5-skill cookbook.
4. Documented the divergence in `SPRINT_DOCS/INFRA_DIVERGENCE.md`.
5. Discord post **skipped** — `DISCORD_BOT_TOKEN` not in env (per phase
   brief: "Do NOT attempt to post if the bot token isn't accessible —
   log skip and document").

## Diagnose output (truncated; full JSON in `SPRINT_DOCS/CHEF_DIAGNOSIS.json`)

```
green: resend_quota, repo_drift
red:   hcloud_credentials  (HCLOUD_TOKEN absent + no ~/.hcloud config)
skip:  portal_deploy, og_image_route, cloudflare_articles, chef_ack_discord
decision: infra-level
rationale: HCLOUD_TOKEN unavailable — cannot provision wisechef-maestro
           CX23 box; scope shifts to canonical interplus-deploy-v1
           sub-recipe + local smoke install only
```

## Smoke test results

```
$ env -u PYTHONPATH WR_DATABASE_URL=sqlite:///:memory: \
    DATABASE_URL=sqlite:///:memory: SIGNING_SECRET=test-secret \
    .venv/bin/python -m pytest tests/test_phase_h_smoke.py -q --tb=short
......                                                                   [100%]
6 passed in 1.54s
```

## Adam-side handoffs

- [ ] Unlock Bitwarden, export `HCLOUD_TOKEN`, run
      `hcloud server create --name wisechef-maestro --type cx23
      --image debian-12 --ssh-key adam@wisechef --location nbg1`.
- [ ] `ansible-playbook playbooks/maestro_bootstrap.yml -i
      inventory/hetzner.yaml` once the box is up.
- [ ] Promote `interplus-deploy-v1.yaml` to `wisechef-agents` cookbook
      mirror (this phase was read-only against that host).
- [ ] File the Paperclip ticket described in `INFRA_DIVERGENCE.md`.
- [ ] Manually post smoke summary to `#tori` (bot token blocked here).

## Files changed

```
scripts/diagnose_chef_pipeline_v7.py       (new, +200)
SPRINT_DOCS/CHEF_DIAGNOSIS.json            (new, generated)
SPRINT_DOCS/INFRA_DIVERGENCE.md            (new)
SPRINT_DOCS/V7_SUBAGENT_H_OUTPUT.md        (new — this file)
cookbooks/interplus-deploy-v1.yaml         (new, +14)
tests/test_phase_h_smoke.py                (new, +143)
```

## Non-negotiables status

- ✅ Did not touch `wisechef-agents`.
- ✅ Did not attempt HCLOUD provisioning.
- ✅ Pre-flight diagnose ran before any other work.
- ✅ Each chunk committed under `feat(v7/phase-H):` prefix.
