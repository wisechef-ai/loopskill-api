"""superset_0606 Phase A — federation security spine tests.

Covers app/services/federation_fetch.py:
  - is_safe_url / guarded_get  : SSRF + redirect-target re-validation
  - normalize_install_leaf / safe_install_leaf : path-traversal defense
  - resolve_license / is_redistributable : the 4-step license gate (decision #13)

All offline — DNS resolution for the SSRF cases uses literal IPs and the
public github host, redirect cases inject a fake transport via monkeypatch.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import federation_fetch as ff


# ─────────────────────────────── SSRF: is_safe_url ──────────────────────────


class TestIsSafeUrl:
    def test_public_https_is_safe(self):
        assert ff.is_safe_url("https://api.github.com/repos/x/y") is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",  # AWS/GCP/Azure metadata
            "http://169.254.170.2/v2/credentials",  # ECS task creds
            "http://127.0.0.1:8080/admin",  # loopback
            "http://10.0.0.5/internal",  # private 10.x
            "http://192.168.1.1/",  # private 192.168
            "http://172.16.0.1/",  # private 172.16
            "http://100.64.0.1/",  # CGNAT
            "http://[::1]/",  # IPv6 loopback
        ],
    )
    def test_blocks_private_and_metadata(self, url):
        assert ff.is_safe_url(url) is False

    def test_blocks_metadata_hostname(self):
        assert ff.is_safe_url("http://metadata.google.internal/computeMetadata/v1/") is False

    @pytest.mark.parametrize("scheme_url", ["file:///etc/passwd", "ftp://host/x", "gopher://x", "data:text/plain,x"])
    def test_blocks_non_http_schemes(self, scheme_url):
        assert ff.is_safe_url(scheme_url) is False

    def test_empty_and_garbage_fail_closed(self):
        assert ff.is_safe_url("") is False
        assert ff.is_safe_url("not a url") is False
        assert ff.is_safe_url("https://") is False

    def test_dns_failure_fails_closed(self):
        # An unresolvable host fails closed (no IP to clear).
        assert ff.is_safe_url("https://this-host-does-not-exist.invalid/") is False


class TestIsBlockedIp:
    """Direct unit coverage of the IP-class predicate (the DNS-resolved branch)."""

    import ipaddress as _ip

    @pytest.mark.parametrize(
        "addr",
        [
            "10.0.0.1",
            "192.168.0.1",
            "172.16.5.5",
            "127.0.0.1",
            "169.254.0.1",  # link-local
            "100.64.0.1",  # CGNAT
            "::1",  # IPv6 loopback
            "::ffff:10.0.0.1",  # IPv4-mapped IPv6 private
            "::ffff:127.0.0.1",  # IPv4-mapped IPv6 loopback
            "::ffff:100.64.0.1",  # IPv4-mapped IPv6 CGNAT
            "224.0.0.1",  # multicast
            "0.0.0.0",  # unspecified
        ],
    )
    def test_blocked(self, addr):
        assert ff._is_blocked_ip(self._ip.ip_address(addr)) is True

    @pytest.mark.parametrize("addr", ["8.8.8.8", "1.1.1.1", "::ffff:8.8.8.8", "2606:4700:4700::1111"])
    def test_allowed_public(self, addr):
        assert ff._is_blocked_ip(self._ip.ip_address(addr)) is False


# ─────────────────────────────── SSRF: guarded_get ──────────────────────────


class _FakeResp:
    def __init__(self, status_code=200, headers=None, text="ok"):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def json(self):
        import json

        return json.loads(self.text)


class TestGuardedGet:
    def test_unsafe_target_returns_none_without_fetching(self, monkeypatch):
        called = {"n": 0}

        def _spy(*a, **k):  # pragma: no cover - must NOT be reached
            called["n"] += 1
            return _FakeResp()

        monkeypatch.setattr(ff.httpx, "get", _spy)
        assert ff.guarded_get("http://169.254.169.254/creds") is None
        assert called["n"] == 0, "guarded_get must block BEFORE issuing the request"

    def test_safe_target_fetches(self, monkeypatch):
        monkeypatch.setattr(ff.httpx, "get", lambda *a, **k: _FakeResp(200, text="body"))
        monkeypatch.setattr(ff, "is_safe_url", lambda u: True)
        resp = ff.guarded_get("https://example.com/x")
        assert resp is not None and resp.status_code == 200

    def test_redirect_to_unsafe_is_blocked(self, monkeypatch):
        # First hop: a redirect to a metadata IP. The guard must re-validate the
        # Location and refuse to follow it.
        seq = [_FakeResp(302, headers={"location": "http://169.254.169.254/creds"})]

        def _get(url, **k):
            return seq.pop(0)

        monkeypatch.setattr(ff.httpx, "get", _get)
        # is_safe_url is real → blocks the metadata redirect target.
        assert ff.guarded_get("https://example.com/start") is None

    def test_redirect_to_safe_is_followed(self, monkeypatch):
        responses = {
            "https://a.example/start": _FakeResp(302, headers={"location": "https://b.example/final"}),
            "https://b.example/final": _FakeResp(200, text="arrived"),
        }
        monkeypatch.setattr(ff, "is_safe_url", lambda u: True)
        monkeypatch.setattr(ff.httpx, "get", lambda url, **k: responses[url])
        resp = ff.guarded_get("https://a.example/start")
        assert resp is not None and resp.text == "arrived"

    def test_redirect_loop_capped(self, monkeypatch):
        # Always redirect to a new safe URL → must stop at the hop cap → None.
        monkeypatch.setattr(ff, "is_safe_url", lambda u: True)
        counter = {"n": 0}

        def _get(url, **k):
            counter["n"] += 1
            return _FakeResp(302, headers={"location": f"https://example.com/{counter['n']}"})

        monkeypatch.setattr(ff.httpx, "get", _get)
        assert ff.guarded_get("https://example.com/0") is None
        assert counter["n"] <= ff._MAX_FETCH_REDIRECTS + 1

    def test_transport_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(ff, "is_safe_url", lambda u: True)

        def _boom(*a, **k):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(ff.httpx, "get", _boom)
        assert ff.guarded_get("https://example.com/x") is None

    def test_redirect_without_location_returns_none(self, monkeypatch):
        monkeypatch.setattr(ff, "is_safe_url", lambda u: True)
        monkeypatch.setattr(ff.httpx, "get", lambda *a, **k: _FakeResp(302, headers={}))
        assert ff.guarded_get("https://example.com/x") is None


# ─────────────────────────────── Path safety ────────────────────────────────


class TestInstallLeaf:
    @pytest.mark.parametrize("leaf", ["web-scraper", "my_skill", "Skill123", "a.b-c_d"])
    def test_valid_leaves(self, leaf):
        assert ff.normalize_install_leaf(leaf) == leaf

    @pytest.mark.parametrize(
        "leaf",
        [
            "",
            ".",
            "..",
            "../../etc/passwd",
            "a/b",
            "a\\b",
            "/abs",
            "~/home",
            "with space",
            "-leading-dash",  # must start alnum
            ".hidden",  # must start alnum
        ],
    )
    def test_rejects_unsafe_leaves(self, leaf):
        with pytest.raises(ValueError):
            ff.normalize_install_leaf(leaf)

    def test_rejects_nul_byte(self):
        with pytest.raises(ValueError):
            ff.normalize_install_leaf("evil\x00name")

    def test_safe_install_leaf_from_namespaced_slug(self):
        assert ff.safe_install_leaf("owner/repo--my-skill") == "my-skill"
        assert ff.safe_install_leaf("a.com--task") == "task"
        assert ff.safe_install_leaf("plain") == "plain"

    def test_safe_install_leaf_rejects_traversal_slug(self):
        # A slug whose final segment is a traversal is rejected.
        with pytest.raises(ValueError):
            ff.safe_install_leaf("owner/repo--..")
        with pytest.raises(ValueError):
            ff.safe_install_leaf("x--")  # empty trailing segment


# ─────────────────────────────── License gate ───────────────────────────────


class TestLicenseGate:
    @pytest.mark.parametrize(
        "lic",
        ["MIT", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "ISC", "MPL-2.0", "CC-BY-4.0", "Unlicense", "0BSD"],
    )
    def test_redistributable_licenses(self, lic):
        assert ff.is_redistributable(lic) is True

    @pytest.mark.parametrize(
        "lic",
        [None, "", "LicenseRef-Anthropic-Commercial", "Proprietary", "CC-BY-NC-4.0", "GPL-3.0-only", "AGPL-3.0"],
    )
    def test_non_redistributable_licenses(self, lic):
        assert ff.is_redistributable(lic) is False

    def test_compound_license_nvidia(self):
        # decision #12: NVIDIA ships "Apache-2.0 AND CC-BY-4.0" — both
        # redistributable, so the compound declaration is installable.
        assert ff.is_redistributable("Apache-2.0 AND CC-BY-4.0") is True

    def test_resolve_order_skill_dir_wins(self):
        # skill-dir LICENSE.txt beats repo + frontmatter (decision #13 step 1).
        lic, redist = ff.resolve_license(
            skill_dir_license="MIT",
            repo_root_license="GPL-3.0",
            skill_md="---\nlicense: Proprietary\n---",
        )
        assert lic == "mit" and redist is True

    def test_resolve_order_repo_second(self):
        lic, redist = ff.resolve_license(repo_root_license="Apache-2.0", skill_md="---\nlicense: x\n---")
        assert lic == "apache-2.0" and redist is True

    def test_resolve_order_frontmatter_third(self):
        lic, redist = ff.resolve_license(skill_md="---\nname: x\nlicense: BSD-3-Clause\n---\n# body")
        assert lic == "bsd-3-clause" and redist is True

    def test_resolve_none_found_is_deep_link(self):
        lic, redist = ff.resolve_license()
        assert lic is None and redist is False

    def test_resolve_source_available_frontmatter_blocks_install(self):
        lic, redist = ff.resolve_license(skill_md="---\nlicense: LicenseRef-Anthropic\n---")
        assert lic == "licenseref-anthropic" and redist is False

    def test_resolve_frontmatter_no_license_line(self):
        lic, redist = ff.resolve_license(skill_md="---\nname: x\n---\n# no license here")
        assert lic is None and redist is False
