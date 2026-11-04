"""Body of the middleware's no-key 401 (coldstart_1029).

A bare ``401 {"detail": "Invalid or missing x-api-key header"}`` is a dead end
for a keyless agent. Observed live: a cold agent built a valid
``POST /api/v1/skill-error`` payload, hit that exact body, and gave up, because
nothing it had read connected "you need a key" to "you can mint one yourself".

So the rejection also names the one self-serve path that mints a key. The keys
are machine-readable rather than prose because agents parse this body. The
``detail`` string is unchanged: dozens of tests, docstrings and mirrored
clients assert it verbatim.
"""

from __future__ import annotations

NO_KEY_DETAIL = "Invalid or missing x-api-key header"


def no_key_401_body() -> dict:
    """Return a fresh body per call so no caller can mutate a shared dict."""
    return {
        "detail": NO_KEY_DETAIL,
        "how_to_get_a_key": {
            "enroll": "POST /api/agents/register",
            # ah_1104: the same enrolment as one command (bash + curl + OpenSSL 3).
            "enroll_script": (
                "curl -fsSL https://app.loopskill.io/api/agents/enroll.sh | bash -s -- <agent-name>"
            ),
            "docs": "https://app.loopskill.io/llms.txt",
            "machine": "https://app.loopskill.io/.well-known/agent.json",
            "note": (
                "self-serve, Ed25519 proof-of-key, no human; "
                "free tier includes feedback + report_skill_errors"
            ),
        },
    }
