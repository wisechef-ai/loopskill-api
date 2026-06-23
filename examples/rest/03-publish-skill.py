#!/usr/bin/env python3
"""Example 03 — Publish a skill via POST /api/skills/_publish (illustrative).

This script demonstrates the full publish flow:
  1. Read a SKILL.md file and an optional SKILL.toml
  2. Package them into a .tar.gz tarball
  3. Sign the tarball with an Ed25519 key (or use a placeholder for illustration)
  4. POST to /api/skills/_publish as multipart/form-data

Auth: x-api-key header (rec_* key) — must match the skill's creator account
Env:  RECIPES_API_KEY        — your API key (required)
      RECIPES_API_KEY_PRIV   — path to Ed25519 private key PEM (optional, for real signing)
      RECIPES_BASE_URL       — override base URL (default: https://recipes.wisechef.ai)

Usage:
    # Dry-run illustration (no real key):
    RECIPES_API_KEY=*** python examples/rest/03-publish-skill.py --skill-md path/to/SKILL.md

    # Real publish (requires cryptography package and a signing key):
    RECIPES_API_KEY=*** RECIPES_API_KEY_PRIV=~/.keys/ed25519.pem \\
        python examples/rest/03-publish-skill.py --skill-md path/to/SKILL.md --toml path/to/skill.toml
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
import urllib.request

BASE_URL = os.environ.get("RECIPES_BASE_URL", "https://recipes.wisechef.ai").rstrip("/")

# ---------------------------------------------------------------------------
# Multipart helper (stdlib only — no requests dependency)
# ---------------------------------------------------------------------------

BOUNDARY = b"----RecipesPublishBoundary"


def _encode_part(name: str, value: bytes | str, filename: str | None = None, content_type: str = "application/octet-stream") -> bytes:
    if isinstance(value, str):
        value = value.encode()
    header = f'Content-Disposition: form-data; name="{name}"'
    if filename:
        header += f'; filename="{filename}"'
    parts = [
        b"--" + BOUNDARY,
        (header + f"\r\nContent-Type: {content_type}").encode(),
        b"",
        value,
    ]
    return b"\r\n".join(parts)


def build_multipart(*parts: bytes) -> tuple[bytes, str]:
    body = b"\r\n".join(parts) + b"\r\n--" + BOUNDARY + b"--\r\n"
    content_type = f"multipart/form-data; boundary={BOUNDARY.decode()}"
    return body, content_type


# ---------------------------------------------------------------------------
# Tarball builder
# ---------------------------------------------------------------------------

def build_tarball(skill_md_path: str, skill_toml_path: str | None) -> bytes:
    """Pack SKILL.md (and optionally skill.toml) into a .tar.gz tarball in memory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(skill_md_path, arcname="SKILL.md")
        if skill_toml_path:
            tar.add(skill_toml_path, arcname="skill.toml")
        else:
            # Minimal placeholder skill.toml for illustration purposes
            toml_content = (
                '[skill]\n'
                'name = "example-skill"\n'
                'version = "0.1.0"\n'
                'description = "An example skill"\n'
                'license = "MIT"\n'
                'entrypoint = "SKILL.md"\n'
            ).encode()
            info = tarfile.TarInfo(name="skill.toml")
            info.size = len(toml_content)
            tar.addfile(info, io.BytesIO(toml_content))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Ed25519 signing (requires `pip install cryptography`)
# ---------------------------------------------------------------------------

def sign_tarball(tarball: bytes, priv_key_path: str) -> tuple[bytes, bytes]:
    """Sign sha256(tarball) with an Ed25519 private key. Returns (signature, pubkey_der)."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PublicFormat,
            load_pem_private_key,
        )
    except ImportError:
        print("Error: `cryptography` package is required for real signing.", file=sys.stderr)
        print("  pip install cryptography", file=sys.stderr)
        sys.exit(1)

    with open(priv_key_path, "rb") as f:
        priv_key = load_pem_private_key(f.read(), password=None)

    digest = hashlib.sha256(tarball).digest()
    signature = priv_key.sign(digest)
    pub_der = priv_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return signature, pub_der


def fake_signature() -> tuple[bytes, bytes]:
    """Return placeholder signature bytes for illustration purposes only."""
    print("⚠  Using placeholder signature — NOT suitable for real publishing.", file=sys.stderr)
    return b"\x00" * 64, b"\x00" * 44  # invalid placeholders


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

def publish_skill(api_key: str, skill_md_path: str, skill_toml_path: str | None, dry_run: bool) -> None:
    tarball = build_tarball(skill_md_path, skill_toml_path)
    sha256 = hashlib.sha256(tarball).hexdigest()
    print(f"Tarball: {len(tarball):,} bytes, sha256={sha256[:16]}…")

    priv_key_path = os.environ.get("RECIPES_API_KEY_PRIV", "")
    if priv_key_path and os.path.exists(priv_key_path):
        signature, pub_key_der = sign_tarball(tarball, priv_key_path)
        print(f"Signed with {priv_key_path}")
    else:
        signature, pub_key_der = fake_signature()

    # Read skill.toml content for the `skill_toml` form field
    if skill_toml_path:
        with open(skill_toml_path, "rb") as f:
            toml_bytes = f.read()
    else:
        toml_bytes = (
            '[skill]\nname = "example-skill"\nversion = "0.1.0"\n'
            'description = "Example"\nlicense = "MIT"\nentrypoint = "SKILL.md"\n'
        ).encode()

    import base64
    parts = (
        _encode_part("skill_toml", toml_bytes, filename="skill.toml", content_type="text/plain"),
        _encode_part("tarball", tarball, filename="skill.tar.gz", content_type="application/gzip"),
        _encode_part("signature", base64.b64encode(signature)),
        _encode_part("signing_pubkey", base64.b64encode(pub_key_der)),
        _encode_part("is_public", "false"),
        _encode_part("changelog", "Initial release"),
    )
    body, content_type = build_multipart(*parts)

    url = f"{BASE_URL}/api/skills/_publish"
    print(f"\n{'[DRY-RUN] Would POST' if dry_run else 'POST'} {url}")

    if dry_run:
        print("  (skipping actual HTTP request in dry-run mode)")
        return

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "x-api-key": api_key,
            "Content-Type": content_type,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode())

    print("\nResponse:")
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish a skill to recipes.wisechef.ai")
    parser.add_argument("--skill-md", default="SKILL.md", help="Path to SKILL.md")
    parser.add_argument("--toml", default=None, help="Path to skill.toml (optional)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Skip the actual HTTP request (default: on)")
    parser.add_argument("--real", action="store_true", help="Actually send the request (overrides --dry-run)")
    args = parser.parse_args()

    api_key = os.environ.get("RECIPES_API_KEY", "")
    if not api_key:
        print("Error: RECIPES_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    dry_run = args.dry_run and not args.real
    publish_skill(api_key, args.skill_md, args.toml, dry_run=dry_run)


if __name__ == "__main__":
    main()
