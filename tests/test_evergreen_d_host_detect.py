"""evergreen_0206 Phase D — host auto-detection (Hermes + Codex live; q2)."""

from __future__ import annotations


from app.reconcile_host_detect import (
    LIVE_HOSTS,
    cron_template,
    detect_hosts,
    select_host,
)


def _mk_host(home, kind):
    dirs = {
        "hermes": ".hermes/skills",
        "codex": ".codex/skills",
        "claude": ".claude/skills",
        "opencode": ".opencode/skills",
    }
    d = home / dirs[kind]
    d.mkdir(parents=True)
    return d


class TestDetect:
    def test_detects_hermes(self, tmp_path):
        _mk_host(tmp_path, "hermes")
        hosts = detect_hosts(home=tmp_path)
        assert len(hosts) == 1
        assert hosts[0].kind == "hermes"
        assert hosts[0].live is True

    def test_detects_codex_live(self, tmp_path):
        _mk_host(tmp_path, "codex")
        hosts = detect_hosts(home=tmp_path)
        assert hosts[0].kind == "codex"
        assert hosts[0].live is True, "Codex is a live host this sprint (Adam q2)"

    def test_claude_detected_but_not_live(self, tmp_path):
        _mk_host(tmp_path, "claude")
        hosts = detect_hosts(home=tmp_path)
        assert hosts[0].kind == "claude"
        assert hosts[0].live is False, "Claude detection ships, wiring is follow-on"

    def test_nothing_detected(self, tmp_path):
        assert detect_hosts(home=tmp_path) == []

    def test_multiple_hosts_priority_order(self, tmp_path):
        _mk_host(tmp_path, "codex")
        _mk_host(tmp_path, "hermes")
        _mk_host(tmp_path, "claude")
        hosts = detect_hosts(home=tmp_path)
        # Priority: hermes, codex, claude
        assert [h.kind for h in hosts] == ["hermes", "codex", "claude"]


class TestSelect:
    def test_select_prefers_explicit_host(self, tmp_path):
        _mk_host(tmp_path, "hermes")
        _mk_host(tmp_path, "codex")
        h = select_host(home=tmp_path, prefer="codex")
        assert h.kind == "codex"

    def test_select_explicit_undetected_returns_none(self, tmp_path):
        """An explicit --host that isn't present must NOT silently fall back."""
        _mk_host(tmp_path, "hermes")
        assert select_host(home=tmp_path, prefer="codex") is None

    def test_select_default_picks_first_live(self, tmp_path):
        _mk_host(tmp_path, "claude")  # detected but not live
        _mk_host(tmp_path, "codex")  # live
        h = select_host(home=tmp_path)
        assert h.kind == "codex", "default selection skips non-live claude for live codex"

    def test_select_none_when_empty(self, tmp_path):
        assert select_host(home=tmp_path) is None


class TestCronTemplate:
    def test_hermes_template_has_cookbook_and_dirs(self, tmp_path):
        _mk_host(tmp_path, "hermes")
        h = select_host(home=tmp_path)
        tpl = cron_template(h, cookbook_id="cb-123", api_base="https://recipes.wisechef.ai")
        assert "cb-123" in tpl
        assert "recipes-reconcile" in tpl
        assert str(h.skills_dir) in tpl
        assert "recipes-lock.json" in tpl

    def test_codex_template_renders(self, tmp_path):
        _mk_host(tmp_path, "codex")
        h = select_host(home=tmp_path)
        tpl = cron_template(h, cookbook_id="cb-9", api_base="https://x")
        assert "cb-9" in tpl
        assert "codex" in tpl


class TestLiveHostsContract:
    def test_live_hosts_are_hermes_and_codex(self):
        assert LIVE_HOSTS == {"hermes", "codex"}
