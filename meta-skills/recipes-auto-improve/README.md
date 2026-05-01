# recipes-auto-improve

Wraps every WiseRecipes skill invocation. On failure, posts an anonymous,
sanitized incident report to `/api/feedback/incident` so the upstream
network can cluster failures and draft patches.

- Apache-2.0 (this repo only — wrapped skills keep their own license)
- Stdlib only — no extra deps
- ≤200 lines of Python

## Usage

```bash
export RECIPES_API_KEY=rec_xxx
export RECIPES_AGENT_FP=$(uuidgen | sha256sum | head -c32)

python -m cli --skill-id $SKILL_UUID --skill-version 1.2.0 -- \
    /path/to/skill/run.sh --foo bar
```

## What gets sent

| field           | example                                    |
|-----------------|--------------------------------------------|
| skill_id        | UUID of the failing skill                  |
| error_signature | sha256 of normalized top-5 stack frames    |
| env_fingerprint | `{os, arch, py, ram_gb, cuda}`             |
| agent_fp_anon   | rotating salted hash, **not user identity**|
| stack_trace_top | scrubbed top-5 frames, ≤2KB                |
| command         | scrubbed argv, ≤512B                       |

Scrubbing rules:

- `/home/<user>` → `/<HOME>`
- `/Users/<user>` → `/<HOME>`
- `rec_<32 hex>` → `rec_<REDACTED>`

The endpoint also runs a server-side regex audit and rejects any payload
that still contains creds, paths, or `secret`/`password`/`bearer` tokens.
