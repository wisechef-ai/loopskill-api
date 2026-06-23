#!/usr/bin/env python3
"""Example 02 — Install signed-URL roundtrip.

Demonstrates:
  1. GET /api/skills/install?slug=<slug>  → signed download URL + manifest
  2. Fetch the tarball from the signed URL
  3. Verify the SHA-256 checksum from the manifest
  4. (Note on Ed25519 signature verification)

Auth: x-api-key header (rec_* key)
Env:  RECIPES_API_KEY   — your API key (required)
      RECIPES_BASE_URL  — override base URL (default: https://recipes.wisechef.ai)

Usage:
    RECIPES_API_KEY=*** python examples/rest/02-install-signed-url-roundtrip.py web-scraper
    RECIPES_API_KEY=*** python examples/rest/02-install-signed-url-roundtrip.py --slug seo-audit --save
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request

BASE_URL = os.environ.get("RECIPES_BASE_URL", "https://recipes.wisechef.ai").rstrip("/")


def get_install_url(api_key: str, slug: str) -> dict:
    """Call GET /api/skills/install?slug=<slug> and return the response dict."""
    url = f"{BASE_URL}/api/skills/install?slug={slug}"
    req = urllib.request.Request(
        url,
        headers={"x-api-key": api_key, "Accept": "application/json"},
        method="GET",
    )
    print(f"[1/3] GET {url}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def fetch_tarball(signed_url: str) -> bytes:
    """Download the tarball from the pre-signed URL (no auth header needed)."""
    print(f"[2/3] Fetching tarball from signed URL …")
    with urllib.request.urlopen(signed_url, timeout=60) as resp:
        data = resp.read()
    print(f"      Downloaded {len(data):,} bytes.")
    return data


def verify_checksum(data: bytes, manifest: dict) -> None:
    """Verify the SHA-256 checksum from the manifest against fetched bytes."""
    expected = manifest.get("sha256") or manifest.get("checksum") or ""
    if not expected:
        print("[3/3] ⚠  No SHA-256 checksum in manifest — skipping verification.")
        return
    actual = hashlib.sha256(data).hexdigest()
    if actual == expected:
        print(f"[3/3] ✓  SHA-256 verified: {actual}")
    else:
        print(f"[3/3] ✗  SHA-256 MISMATCH!", file=sys.stderr)
        print(f"      expected: {expected}", file=sys.stderr)
        print(f"      got:      {actual}", file=sys.stderr)
        sys.exit(1)


def note_ed25519() -> None:
    """Print a note about Ed25519 signature verification."""
    print(
        "\nNote — Ed25519 signature verification:\n"
        "  The API signs each tarball release with an Ed25519 key. The manifest\n"
        "  contains a 'signature' field (base64-encoded) and the signing public\n"
        "  key is available from GET /api/keys/signing-pubkey.\n"
        "  To verify with cryptography:\n\n"
        "    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey\n"
        "    from cryptography.hazmat.primitives.serialization import load_der_public_key\n"
        "    import base64, hashlib\n\n"
        "    digest = hashlib.sha256(tarball_bytes).digest()\n"
        "    pub_key = load_der_public_key(base64.b64decode(pubkey_b64))\n"
        "    pub_key.verify(base64.b64decode(signature_b64), digest)\n"
        "    # raises InvalidSignature on failure\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Install signed-URL roundtrip for a skill")
    parser.add_argument("slug", nargs="?", default="web-scraper", help="Skill slug to install")
    parser.add_argument("--slug", dest="slug_flag", default="", help="Skill slug (alternative to positional)")
    parser.add_argument("--save", action="store_true", help="Save tarball to a temp file")
    args = parser.parse_args()

    slug = args.slug_flag or args.slug

    api_key = os.environ.get("RECIPES_API_KEY", "")
    if not api_key:
        print("Error: RECIPES_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    try:
        response = get_install_url(api_key, slug)
    except Exception as exc:
        print(f"Failed to get install URL: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\nManifest:")
    print(json.dumps(response, indent=2))

    signed_url = response.get("url") or response.get("download_url") or ""
    if not signed_url:
        print("\n⚠  No download URL in response — cannot fetch tarball (check tier/auth).")
        note_ed25519()
        return

    try:
        tarball = fetch_tarball(signed_url)
    except Exception as exc:
        print(f"Failed to fetch tarball: {exc}", file=sys.stderr)
        sys.exit(1)

    manifest = response.get("manifest") or response
    verify_checksum(tarball, manifest)

    if args.save:
        with tempfile.NamedTemporaryFile(suffix=f"-{slug}.tar.gz", delete=False) as f:
            f.write(tarball)
            print(f"\nSaved to: {f.name}")

    note_ed25519()


if __name__ == "__main__":
    main()
