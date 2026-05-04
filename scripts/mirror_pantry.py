#!/usr/bin/env python3
"""scripts/mirror_pantry.py

Clones 3 hardcoded upstream repos via the gh CLI, verifies license at clone
time (must be MIT or Apache-2.0), and outputs SHA-pinned info to stdout.

Usage:
    python scripts/mirror_pantry.py [--dest /tmp/pantry]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


PANTRY_SOURCES: list[dict] = [
    {"repo": "obra/superpowers",                 "license": "MIT"},
    {"repo": "Houseofmvps/ultraship",             "license": "MIT"},
    {"repo": "wisechef-ai/awesome-agent-recipes", "license": "MIT"},
]

PERMITTED_LICENSES = {"MIT", "Apache-2.0"}


class LicenseError(RuntimeError):
    """Raised when a repo's license doesn't match the expected value."""


def _run_gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
    )


def check_license(repo: str, expected_license: str) -> None:
    """Fetch repo metadata via gh CLI and assert the license matches.

    Raises:
        LicenseError: if SPDX ID doesn't match expected_license or isn't
                      in PERMITTED_LICENSES.
        RuntimeError: if the gh CLI call fails.
    """
    result = _run_gh(["api", f"repos/{repo}"])
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api call failed for {repo}: {result.stderr}"
        )
    data = json.loads(result.stdout)
    spdx = (data.get("license") or {}).get("spdx_id", "")
    if spdx not in PERMITTED_LICENSES:
        raise LicenseError(
            f"{repo}: license '{spdx}' not in permitted set {PERMITTED_LICENSES}"
        )
    if spdx != expected_license:
        raise LicenseError(
            f"{repo}: expected license '{expected_license}', got '{spdx}'"
        )


def get_repo_sha(repo: str) -> str:
    """Return the current HEAD commit SHA of the repo's default branch.

    Raises:
        RuntimeError: if the gh CLI call fails.
    """
    result = _run_gh(["api", f"repos/{repo}/commits/HEAD"])
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to get SHA for {repo}: {result.stderr}"
        )
    data = json.loads(result.stdout)
    return data["sha"]


def clone_repo(repo: str, expected_license: str, dest: str) -> dict:
    """Verify license, obtain SHA, clone repo into dest/.

    Returns dict with 'repo', 'sha', 'dest_path'.

    Raises:
        LicenseError: if license check fails.
        RuntimeError: on gh/git errors.
    """
    check_license(repo, expected_license)
    sha = get_repo_sha(repo)

    dest_path = Path(dest) / repo.replace("/", "__")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    result = _run_gh(["repo", "clone", repo, str(dest_path), "--", "--depth=1"])
    if result.returncode != 0:
        raise RuntimeError(f"Clone failed for {repo}: {result.stderr}")

    return {"repo": repo, "sha": sha, "dest_path": str(dest_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Mirror Pantry upstream repos")
    parser.add_argument(
        "--dest",
        default=tempfile.mkdtemp(prefix="pantry-"),
        help="Directory to clone repos into",
    )
    args = parser.parse_args()

    results = []
    for src in PANTRY_SOURCES:
        print(f"Processing {src['repo']}...", file=sys.stderr)
        try:
            info = clone_repo(src["repo"], src["license"], dest=args.dest)
            results.append(info)
            print(json.dumps(info))
        except (LicenseError, RuntimeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)

    print(f"\nMirrored {len(results)} repos.", file=sys.stderr)


if __name__ == "__main__":
    main()
