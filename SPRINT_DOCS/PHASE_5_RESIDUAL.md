# Phase 5 — Stripe Live Probe Residual

**Status:** Deferred to "when Bitwarden is unlocked."

**Why deferred:** The v7.1 plan specifies a real €0.50 charge on both Cook (€20)
and Operator (€100) tiers, then refund. This requires `STRIPE_SECRET_KEY` for
Stripe live mode and `STRIPE_WEBHOOK_SECRET` accessed via Bitwarden, which is
locked in this session. The plan's Adam directive is "boil the ocean — don't
care about pricing", but the BW unlock is a human factor we can't bypass.

## What's already shipped (this PR, see #v7.1/phase-5-stripe-pin)

- ✅ `stripe==15.1.0` exact pin in `requirements.txt` (was `>=8.0` — F5 mitigation)
- ✅ Webhook signature dry-run regression test (catches SDK 15.x dict-conversion class)
- ✅ Self-signed test event verification — proves `Webhook.construct_event()` works
- ✅ Tampered-signature negative test — proves we still reject bad signatures
- ✅ `scripts/stripe_synthetic_probe.py` — read-only Sunday probe for the audit cron
- ✅ Verified no `event.data.*` attribute-access pattern in app code (SDK 15.x trap doesn't apply here)

## What's deferred (run when Adam unlocks BW)

### 1. Live €0.50 probe on both tiers

```bash
# Unlock Bitwarden
bw unlock --raw > /tmp/bw_session
export BW_SESSION=$(cat /tmp/bw_session)

# Load Stripe live keys into env
ssh wisechef-hq 'set -a; source ~/wiserecipes-api/.env; set +a; env | grep -E "^STRIPE_"' \
  > /tmp/stripe.env

# Run the canonical paid probe per `stripe-paid-probe-bug-flush` skill
~/.hermes/skills/devops/stripe-paid-probe-bug-flush/scripts/run-probe.sh \
  --tier cook --amount 50  # €0.50
~/.hermes/skills/devops/stripe-paid-probe-bug-flush/scripts/run-probe.sh \
  --tier operator --amount 50

# Verify in DB after each
ssh wisechef-hq "set -a; source ~/wiserecipes-api/.env; set +a; \
  psql \$WR_DATABASE_URL -c \"SELECT email, subscription_tier, subscription_status \
  FROM users WHERE email = 'tori@wisechef.ai';\""
```

**Acceptance:**
- Both €0.50 charges appear in Stripe dashboard, both refunded after probe
- DB `subscription_tier` flips to cook then to operator within 60s of webhook
- No 5xx in `journalctl -u wiserecipes-api` during either run
- Test-user can immediately call `/api/cookbooks` post-payment

### 2. Wire synthetic probe into daily-agent-audit cron

The synthetic probe at `scripts/stripe_synthetic_probe.py` is **standalone-runnable** but not yet wired into a recurring cron. Two options:

**Option A** — add a Sunday-only step inside the existing `daily-agent-audit-6am` cron:

```bash
# in ~/.hermes/scripts/audit-to-paperclip.py or wherever the audit runs:
if datetime.now().weekday() == 6:  # Sunday
    subprocess.run(
        ["python3", "/home/wisechef/wiserecipes-api/scripts/stripe_synthetic_probe.py", "--json"],
        check=True,
        env={**os.environ, "STRIPE_SECRET_KEY": ..., "STRIPE_WEBHOOK_SECRET": ...},
    )
```

**Option B** — separate Hermes cron job, `stripe-synthetic-probe-weekly`:

```bash
hermes cron add stripe-synthetic-probe-weekly \
  --schedule "0 7 * * 0" \
  --command "ssh wisechef-hq 'cd ~/wiserecipes-api && \
    set -a; source .env; set +a; \
    venv/bin/python3 scripts/stripe_synthetic_probe.py --json'"
```

Option A is preferred per the v7.1 plan ("Wire Stripe €0.01 weekly probe into existing `daily-agent-audit` cron — F11 mitigation, no new cron").

### 3. End-to-end verification screenshot

After the live probe runs cleanly, post a screenshot of:
1. The two refunded charges in Stripe dashboard
2. A `journalctl -u wiserecipes-api` excerpt showing the webhook handler firing without errors

…in #tori as the canonical "v7.1 Stripe verified" evidence. The plan calls this
out specifically.

## Reference runbook

The canonical procedure is in `~/.hermes/skills/devops/stripe-paid-probe-bug-flush/`.
Use that skill's `scripts/` and `references/` when you run the probe.

## When this can be closed

Phase 5 graduates from "deferred" to "complete" when:
- [ ] Both tiers verified with live €0.50 probe + refund
- [ ] DB tier-flip evidence captured
- [ ] Synthetic probe wired into the audit cron (Option A or B)
- [ ] #tori screenshot posted
- [ ] `2026-05-07-recipes-v7.1-launched.md` updated to mark this complete

Until then, the SDK pin + webhook regression test + standalone probe script are
sufficient to keep production Stripe healthy. They catch the SDK 15.x regression
class without needing a live charge.
