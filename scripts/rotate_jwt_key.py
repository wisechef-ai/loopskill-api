#!/usr/bin/env python3
"""JWT key rotation CLI for WiseRecipes API.

Usage
-----
Add a new kid to the key ring (without activating it yet):
    python scripts/rotate_jwt_key.py add-kid --kid v2 --secret <new-secret>

Activate a kid (tokens will be signed with it from now on):
    python scripts/rotate_jwt_key.py activate --kid v2

Retire an old kid (remove it from the key ring once all tokens signed with it
have expired — default JWT lifetime is 72 hours):
    python scripts/rotate_jwt_key.py retire --kid v1

Show current configuration:
    python scripts/rotate_jwt_key.py status

Full rotation workflow
----------------------
1. Generate a cryptographically strong secret:
       python -c "import secrets; print(secrets.token_hex(32))"

2. Add the new kid (do NOT yet activate — old tokens still verify via JWT_SECRET):
       python scripts/rotate_jwt_key.py add-kid --kid v2 --secret <new-secret>
       # → prints updated WR_JWT_KEYS env var to paste into your .env / secrets vault

3. Deploy the new WR_JWT_KEYS (keep WR_JWT_ACTIVE_KID pointing at v1 or unset).
   All running instances now accept both the old and new key.

4. Activate the new kid — new tokens are now signed with v2:
       python scripts/rotate_jwt_key.py activate --kid v2
       # → prints updated WR_JWT_ACTIVE_KID to set

5. After JWT_EXPIRATION_HOURS (default 72h) no valid token signed with v1 remains.
   Retire the old key:
       python scripts/rotate_jwt_key.py retire --kid v1
       # → prints pruned WR_JWT_KEYS without v1

6. Deploy the final config.  Single-key mode is restored (or use JWT_SECRET for
   the next cycle).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys


def _load_keys() -> dict[str, str]:
    raw = os.environ.get("WR_JWT_KEYS", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, TypeError):
        pass
    return {}


def _dump_keys(keys: dict[str, str]) -> str:
    return json.dumps(keys, separators=(",", ":"))


def cmd_status(args: argparse.Namespace) -> None:  # noqa: ARG001
    """Print current JWT key-ring status."""
    keys = _load_keys()
    active = os.environ.get("WR_JWT_ACTIVE_KID", "")
    legacy = os.environ.get("WR_JWT_SECRET", "(not shown)")

    print("── JWT key-ring status ─────────────────────────────────")
    if keys:
        for kid, secret in keys.items():
            marker = " ← ACTIVE" if kid == active else ""
            print(f"  kid={kid!r}  secret={secret[:8]}...{marker}")
    else:
        print("  (no JWT_KEYS configured — legacy single-key mode)")
    print(f"  JWT_ACTIVE_KID : {active!r}")
    print(f"  JWT_SECRET     : {legacy[:8]}...")
    print("────────────────────────────────────────────────────────")


def cmd_add_kid(args: argparse.Namespace) -> None:
    """Add a new kid to the key ring (does not activate it)."""
    keys = _load_keys()
    kid = args.kid
    secret = args.secret or secrets.token_hex(32)

    if kid in keys:
        print(f"[warn] kid {kid!r} already exists — overwriting.")
    keys[kid] = secret

    new_json = _dump_keys(keys)
    print(f"\nSet this env var (then redeploy all instances before activating):\n")
    print(f"  WR_JWT_KEYS='{new_json}'\n")
    print(f"[info] kid {kid!r} added.  Call 'activate --kid {kid}' after deploy.")


def cmd_activate(args: argparse.Namespace) -> None:
    """Activate a kid so new tokens are signed with it."""
    keys = _load_keys()
    kid = args.kid
    if kid not in keys:
        print(f"[error] kid {kid!r} not found in WR_JWT_KEYS. Add it first.", file=sys.stderr)
        sys.exit(1)

    print(f"\nSet this env var:\n")
    print(f"  WR_JWT_ACTIVE_KID='{kid}'\n")
    print(f"[info] New tokens will be signed with kid={kid!r}.")
    print("[info] Old tokens (signed with previous key or JWT_SECRET) remain valid.")


def cmd_retire(args: argparse.Namespace) -> None:
    """Remove a kid from the key ring (use only after all its tokens have expired)."""
    keys = _load_keys()
    kid = args.kid
    active = os.environ.get("WR_JWT_ACTIVE_KID", "")

    if kid == active:
        print(
            f"[error] Cannot retire active kid {kid!r}. Activate another kid first.",
            file=sys.stderr,
        )
        sys.exit(1)

    if kid not in keys:
        print(f"[warn] kid {kid!r} was not in key ring — nothing to do.")
        return

    del keys[kid]
    new_json = _dump_keys(keys)
    if new_json == "{}":
        print("\nKey ring is now empty.  Remove WR_JWT_KEYS entirely or set:\n")
        print("  WR_JWT_KEYS=''\n")
    else:
        print(f"\nSet this env var:\n")
        print(f"  WR_JWT_KEYS='{new_json}'\n")
    print(f"[info] kid {kid!r} retired.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="JWT key rotation helper for WiseRecipes API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # status
    sub.add_parser("status", help="Show current JWT key-ring state")

    # add-kid
    p_add = sub.add_parser("add-kid", help="Add a kid to the key ring")
    p_add.add_argument("--kid", required=True, help="Key ID (e.g. 'v2')")
    p_add.add_argument(
        "--secret",
        default=None,
        help="HMAC secret (auto-generated if omitted)",
    )

    # activate
    p_act = sub.add_parser("activate", help="Mark a kid as active for new tokens")
    p_act.add_argument("--kid", required=True, help="Key ID to activate")

    # retire
    p_ret = sub.add_parser("retire", help="Remove an old kid from the key ring")
    p_ret.add_argument("--kid", required=True, help="Key ID to retire")

    args = parser.parse_args()
    dispatch = {
        "status": cmd_status,
        "add-kid": cmd_add_kid,
        "activate": cmd_activate,
        "retire": cmd_retire,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
