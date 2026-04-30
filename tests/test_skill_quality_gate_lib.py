"""Tests for the importable app/skill_quality_gate.py library."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from app.skill_quality_gate import (
    scan_directory,
    scan_tarball_bytes,
    _is_private_or_example_ip,
    _scan_text,
)


def _make_tarball(files: dict[str, str]) -> bytes:
    """Build an in-memory .tar.gz with the given path→content map."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# ── Private-IP allowlist ───────────────────────────────────────────────────


def test_private_ip_allowlist() -> None:
    assert _is_private_or_example_ip("127.0.0.1")
    assert _is_private_or_example_ip("10.0.0.1")
    assert _is_private_or_example_ip("192.168.1.1")
    assert _is_private_or_example_ip("172.20.0.1")
    assert _is_private_or_example_ip("169.254.1.1")
    assert _is_private_or_example_ip("192.0.2.1")  # TEST-NET-1
    assert _is_private_or_example_ip("198.51.100.5")  # TEST-NET-2
    assert _is_private_or_example_ip("203.0.113.5")  # TEST-NET-3
    assert _is_private_or_example_ip("224.0.0.1")  # multicast
    assert _is_private_or_example_ip("0.0.0.0")
    # Public should NOT match
    assert not _is_private_or_example_ip("168.119.57.68")
    assert not _is_private_or_example_ip("8.8.8.8")
    assert not _is_private_or_example_ip("1.1.1.1")


# ── scan_text basics ───────────────────────────────────────────────────────


def test_scan_text_clean() -> None:
    findings = _scan_text("SKILL.md", "---\nname: clean\n---\nUse $YOUR_API_KEY")
    assert findings == []


def test_scan_text_blocks_uuid() -> None:
    findings = _scan_text(
        "SKILL.md", "agent_id = '201ace6b-23d2-45dd-af43-f00a9be1b132'"
    )
    assert any(f.category == "internal_uuid" for f in findings)


def test_scan_text_blocks_public_ip() -> None:
    findings = _scan_text("SKILL.md", "Server at 168.119.57.68")
    cats = {f.category for f in findings}
    assert "public_ipv4" in cats
    assert "hetzner_internal" in cats  # double-tag for known internal range


def test_scan_text_allows_private_ip() -> None:
    findings = _scan_text("SKILL.md", "Local: 127.0.0.1, LAN: 192.168.1.1")
    assert findings == []


def test_scan_text_blocks_ssh_combo() -> None:
    findings = _scan_text("SKILL.md", "Run: `ssh deploy@example.com`")
    assert any(f.category == "ssh_user_combo" for f in findings)


def test_scan_text_blocks_discord_mention() -> None:
    findings = _scan_text("SKILL.md", "Ping <@627546493745627166>")
    assert any(f.category == "discord_mention" for f in findings)


def test_scan_text_warns_personal_name() -> None:
    findings = _scan_text("SKILL.md", "Author: Adam Krawczyk")
    assert any(f.category == "personal_name" and f.severity == "warn" for f in findings)


def test_scan_text_warns_hermes_path() -> None:
    findings = _scan_text("SKILL.md", "Config: ~/.hermes/config.yaml")
    assert any(f.category == "hermes_path" for f in findings)


def test_scan_text_warns_internal_hostname() -> None:
    findings = _scan_text("SKILL.md", "Deploy to wisechef-agents")
    assert any(f.category == "internal_hostname" for f in findings)


# ── scan_tarball_bytes (publisher endpoint integration) ────────────────────


def test_tarball_scan_clean() -> None:
    tar = _make_tarball({
        "SKILL.md": "---\nname: clean\n---\n# Use env vars only.\n",
        "scripts/run.py": "import os\nkey = os.environ['YOUR_API_KEY']\n",
    })
    findings = scan_tarball_bytes(tar)
    assert findings == [] or all(f["severity"] != "block" for f in findings)


def test_tarball_scan_blocks_leaky_uuid() -> None:
    tar = _make_tarball({
        "SKILL.md": "agent: 201ace6b-23d2-45dd-af43-f00a9be1b132",
    })
    findings = scan_tarball_bytes(tar)
    assert any(
        f["category"] == "internal_uuid" and f["severity"] == "block"
        for f in findings
    )


def test_tarball_scan_blocks_public_ip() -> None:
    tar = _make_tarball({"SKILL.md": "host = 168.119.57.68"})
    findings = scan_tarball_bytes(tar)
    block_cats = {f["category"] for f in findings if f["severity"] == "block"}
    assert "public_ipv4" in block_cats
    assert "hetzner_internal" in block_cats


def test_tarball_scan_blocks_ssh_combo() -> None:
    tar = _make_tarball({"SKILL.md": "ssh deploy@my-prod-host.example.com"})
    findings = scan_tarball_bytes(tar)
    assert any(f["category"] == "ssh_user_combo" for f in findings)


def test_tarball_scan_corrupt_returns_empty() -> None:
    """Match security_scan.py fail-open behavior: corrupt tarballs return [].

    The publisher endpoint validates tarball structure via signature verification
    and storage; this gate is a content-leak/generalization scan that only runs
    when the tarball is parseable.
    """
    findings = scan_tarball_bytes(b"this is not a tarball")
    assert findings == []


def test_tarball_scan_skips_binaries() -> None:
    tar = _make_tarball({
        "SKILL.md": "clean",
        "assets/logo.png": "168.119.57.68 inside binary should be skipped",
    })
    findings = scan_tarball_bytes(tar)
    # The binary is skipped — no findings for the leaky-looking string in PNG
    assert all(f["file_path"] != "assets/logo.png" for f in findings)


# ── scan_directory ─────────────────────────────────────────────────────────


def test_scan_directory(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("UUID: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    findings = scan_directory(tmp_path)
    assert any(f.category == "internal_uuid" for f in findings)
