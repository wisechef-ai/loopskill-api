#!/usr/bin/env python3
"""Generate an Ed25519 mesh signing keypair. mesh_0408 T0-D — spec §0.3.

Usage:
    # On the host that will SIGN mesh credentials (never a laptop):
    python scripts/mesh_keygen.py --kid mesh-2026-08 \\
        --private-out /etc/loopskill/mesh-signing/mesh-2026-08.pem \\
        --jwks-dir /etc/loopskill/mesh-jwks

This writes:
  - the PRIVATE key PEM at --private-out, chmod 0600
  - the PUBLIC key PEM at <jwks-dir>/<kid>.pub.pem (world-readable is fine —
    this file's entire purpose is to be served publicly via
    /.well-known/jwks.json)

Then set on the signing host:
    WR_MESH_SIGNING_KEY_PATH=/etc/loopskill/mesh-signing/mesh-2026-08.pem
    WR_MESH_SIGNING_KID=mesh-2026-08
    WR_MESH_JWKS_DIR=/etc/loopskill/mesh-jwks

Both --private-out and --jwks-dir MUST be outside the repo tree and outside
any web-servable directory (spec §0.3 rule 2). This script refuses to write
if either path looks like it lives inside a git checkout.

Rotation: run again with a NEW --kid. Do NOT overwrite an existing key file
in place — deploy both old and new .pub.pem files to --jwks-dir for the
overlap window (spec §4.3, 10860s / round up to 4h), then remove the old
kid's public key once the overlap has elapsed, then update
WR_MESH_SIGNING_KID + WR_MESH_SIGNING_KEY_PATH to point at the new key.

Compromise: skip the overlap window entirely — retire the compromised kid's
.pub.pem from --jwks-dir IMMEDIATELY (accepting the availability gap this
causes for partitioned verifiers, per spec §0.3 rule 5 and §4.3b), then run
this script for a replacement key.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mesh.keys import generate_keypair  # noqa: E402


def _looks_like_repo_path(path: Path) -> bool:
    """Best-effort guard: refuse an output path that is inside a git
    working tree. Not a substitute for real host hardening — a warning
    that fires before the mistake becomes a committed private key.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kid", required=True, help="Key ID for this key, e.g. mesh-2026-08")
    parser.add_argument("--private-out", required=True, help="Path to write the PRIVATE key PEM (mode 0600)")
    parser.add_argument("--jwks-dir", required=True, help="Directory to write <kid>.pub.pem into")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the repo-path safety check (only for tests — never for a real key)",
    )
    args = parser.parse_args()

    private_out = Path(args.private_out)
    jwks_dir = Path(args.jwks_dir)

    if not args.force:
        if _looks_like_repo_path(private_out) or _looks_like_repo_path(jwks_dir):
            print(
                "REFUSING: output path appears to be inside a git working tree. "
                "Spec §0.3 rule 2 — the private key must live OUTSIDE the repo tree. "
                "Pass --force only if you are certain this is a test fixture, never "
                "for a real production key.",
                file=sys.stderr,
            )
            return 1

    private_pem, public_pem = generate_keypair()

    private_out.parent.mkdir(parents=True, exist_ok=True)
    private_out.write_bytes(private_pem)
    os.chmod(private_out, 0o600)

    jwks_dir.mkdir(parents=True, exist_ok=True)
    pub_path = jwks_dir / f"{args.kid}.pub.pem"
    pub_path.write_bytes(public_pem)
    os.chmod(pub_path, 0o644)

    print(f"Generated Ed25519 keypair kid={args.kid!r}")
    print(f"  private key : {private_out}  (mode 0600)")
    print(f"  public key  : {pub_path}  (mode 0644 — served via JWKS)")
    print()
    print("Next steps:")
    print(f"  1. Back up {private_out} to Bitwarden, tag 'loopskill-mesh-signing-key'.")
    print("  2. Set on the signing host:")
    print(f"       WR_MESH_SIGNING_KEY_PATH={private_out}")
    print(f"       WR_MESH_SIGNING_KID={args.kid}")
    print(f"       WR_MESH_JWKS_DIR={jwks_dir}")
    print("  3. See docs/security/mesh-key-custody.md for the full rotation")
    print("     and compromise playbooks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
