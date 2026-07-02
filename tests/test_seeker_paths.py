"""Tests for app.seeker.vendor_paths cross-platform resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import seeker


def test_linux_paths_use_dot_dirs(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    paths = seeker.vendor_paths(platform="linux")
    assert paths["claude"] == tmp_path / ".claude" / "skills"
    assert paths["codex"] == tmp_path / ".codex" / "skills"
    assert paths["hermes"] == tmp_path / ".hermes" / "skills"
    assert paths["opencode"] == tmp_path / ".opencode" / "skills"


def test_linux_paths_honor_xdg_config_home(monkeypatch, tmp_path):
    xdg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    paths = seeker.vendor_paths(platform="linux")
    assert paths["claude"] == xdg / "claude" / "skills"
    assert paths["codex"] == xdg / "codex" / "skills"


def test_macos_paths_use_application_support(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    paths = seeker.vendor_paths(platform="darwin")
    expected_base = tmp_path / "Library" / "Application Support"
    assert paths["claude"] == expected_base / "Claude" / "skills"
    assert paths["codex"] == expected_base / "Codex" / "skills"


def test_windows_paths_honor_appdata(monkeypatch, tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))

    paths = seeker.vendor_paths(platform="win32")
    assert paths["claude"] == appdata / "Claude" / "skills"
    assert paths["codex"] == appdata / "Codex" / "skills"


def test_windows_falls_back_to_home_appdata(monkeypatch, tmp_path):
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    paths = seeker.vendor_paths(platform="win32")
    expected = tmp_path / "AppData" / "Roaming" / "Claude" / "skills"
    assert paths["claude"] == expected


def test_unknown_platform_treated_as_linux(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    paths = seeker.vendor_paths(platform="freebsd")
    assert paths["claude"] == tmp_path / ".claude" / "skills"
