# Secret / Credential Incident Response

> **Classification:** Internal security procedure  
> **Owner:** wisechef-ai/platform-security  
> **Last updated:** 2026-05-28  
> **Applies to:** wisechef-ai/recipes-api and all repos under the `wisechef-ai` GitHub org

---

## 1. Scope

This document describes the steps to take when a real, live credential is discovered
in the git history or working tree of any wisechef-ai repository.

Triggers include:
- A `trufflehog --only-verified` finding in CI (hard gate in `secret-scan.yml`)
- A `gitleaks` finding marked high-confidence
- A human report (bug bounty, internal audit, third-party notification)

---

## 2. Severity Classification

| Severity | Credential type | Examples |
|----------|----------------|---------|
| **Critical** | Production service credentials | Stripe live keys, database passwords, JWT signing secrets, cloud provider keys |
| **High** | External service tokens | GitHub tokens, Slack tokens, Discord bot tokens, SendGrid API keys |
| **Medium** | Internal/staging credentials | Test DB passwords, staging API keys |
| **Low** | Pseudosecrets / false positives | Placeholder values, truncated examples, SRI hashes |

Treat all **unverified** findings as Low until confirmed live.

---

## 3. Immediate Response (< 15 minutes)

### 3.1 Confirm the finding is real

```bash
# Example: verify a Stripe key is live
curl -s https://api.stripe.com/v1/charges \
  -u "sk_live_FOUND_KEY:" \
  | python3 -m json.tool | grep '"object"'
# Authenticated response → VERIFIED. Proceed to 3.2 immediately.
# 401/403 response → invalid/revoked. Document and close.
```

### 3.2 Revoke / rotate immediately

**Do NOT wait for a PR or deployment pipeline.** Rotate the secret directly in the
provider console before taking any other action.

| Secret type | Rotation location |
|-------------|-------------------|
| Stripe live key | https://dashboard.stripe.com/apikeys → Roll key |
| GitHub Personal Access Token | https://github.com/settings/tokens → Revoke |
| GitHub Actions secret | Repo → Settings → Secrets → Update |
| PostgreSQL password | `ALTER USER <user> WITH PASSWORD '<new>';` |
| JWT signing secret | Update `SECRET_KEY` in prod env + restart API service |
| Discord bot token | https://discord.com/developers/applications → Regenerate token |
| SendGrid API key | https://app.sendgrid.com/settings/api_keys → Revoke |

### 3.3 Notify the security channel

Post in `#security-incidents` (internal Discord):

```
🚨 CREDENTIAL EXPOSURE — <date>
Repo: wisechef-ai/recipes-api
Type: <e.g. Stripe live key>
Scope: <what the credential could access>
Status: ROTATED at <HH:MM UTC>
Detected by: secret-scan CI / human report
PR/commit: <link>
```

---

## 4. Containment (< 1 hour)

### 4.1 Assess blast radius

1. Check the credential's **audit log** in the provider dashboard for any
   unauthorised use between the time the commit was pushed and rotation.
2. If the repo is public: assume the credential was indexed by secret-scanning
   bots **within minutes** of the push. Treat as compromised even if no
   observed abuse.
3. If the repo is private: risk is lower but rotation is still mandatory.

### 4.2 Remove from git history

> ⚠️ Rewriting public history causes disruption for all contributors.
> Coordinate with the team before force-pushing to any shared branch.

**Option A — BFG Repo Cleaner (recommended for large repos):**

```bash
# 1. Clone a fresh mirror
git clone --mirror https://github.com/wisechef-ai/recipes-api.git repo-mirror.git
cd repo-mirror.git

# 2. Replace the secret everywhere in history
echo "sk_live_ACTUAL_KEY=REDACTED" > /tmp/replacements.txt
java -jar bfg.jar --replace-text /tmp/replacements.txt

# 3. Clean up
git reflog expire --expire=now --all
git gc --prune=now --aggressive

# 4. Force-push (requires bypass of branch protection — use org admin)
git push --force
```

**Option B — `git filter-repo` (no Java dependency):**

```bash
pip install git-filter-repo
git filter-repo --replace-text /tmp/replacements.txt
git push --force --all
```

### 4.3 Invalidate any cached copies

- Trigger a GitHub Support request to purge cached views if the repo is public.
- Rotate any derived secrets (tokens minted from the compromised credential).
- Invalidate all active sessions if an auth secret was compromised.

---

## 5. Eradication & Recovery

1. **Add the secret pattern to the gitleaks baseline** (`.gitleaks.toml` allowlist
   or baseline file) so future scans don't re-flag the now-redacted placeholder.
2. **Add a regression test** asserting the old secret value never reappears.
3. **Update secrets management**: move the secret to the appropriate vault
   (GitHub Actions secret, AWS Secrets Manager, HashiCorp Vault, etc.) if it
   was previously stored in plaintext config.
4. **Re-run secret scan** on the cleaned branch before merging.

---

## 6. Post-Incident Review (< 48 hours)

File a private GitHub issue under `wisechef-ai/recipes-api` with label
`security:incident` and the following template:

```markdown
## Incident summary
- **Date detected:** 
- **Credential type:** 
- **Rotation time:** 
- **Observed abuse:** Yes / No

## Root cause
(How did the secret end up in the repo? e.g. hardcoded in test fixture,
copy-paste from prod env, committed .env file)

## Timeline
| Time (UTC) | Event |
|------------|-------|
| … | Secret committed |
| … | Detected by scan |
| … | Rotated |
| … | History rewritten |

## Remediation actions
- [ ] Secret rotated
- [ ] History cleaned
- [ ] Team notified
- [ ] Blast-radius audit complete
- [ ] Pre-commit / CI scan updated

## Follow-up
(Any process changes, tooling improvements, or training needed)
```

---

## 7. Prevention Checklist

- [ ] `pre-commit install` run in every clone (see `AGENTS.md` dev setup)
- [ ] `.env` and `*.env` in `.gitignore`
- [ ] All production secrets stored in GitHub Actions secrets or a vault —
      never in source files
- [ ] `secret-scan.yml` CI workflow enabled on all branches via branch protection
- [ ] Developers trained: never commit real keys, even to private repos

---

## 8. Contact

| Role | Contact |
|------|---------|
| Security lead | `@security` in internal Discord |
| On-call engineer | PagerDuty rotation (see runbooks) |
| GitHub org admin | Needed for force-push and support tickets |
