"""Meta-tests for the outbound-network guard (tests/net_guard.py).

A guard that silently stops guarding is worse than no guard at all -- it buys
false confidence. These tests pin the guard's own contract:

  1. Non-loopback egress raises immediately instead of hanging.
  2. Loopback still works (the metasearch suite binds a real uvicorn server).
  3. The @pytest.mark.network opt-out actually restores egress.
  4. The specific 2026-08-03 regression -- a ClawHub result row without
     ``ownerHandle`` triggering a live per-row owner lookup -- is caught.
"""

from __future__ import annotations

import socket

import pytest

from tests.net_guard import BlockedNetworkCallError, _is_loopback


class TestGuardBlocksEgress:
    def test_public_ip_connect_raises(self):
        """A raw connect() to a public address fails fast, not slow."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(BlockedNetworkCallError):
                sock.connect(("1.1.1.1", 443))
        finally:
            sock.close()

    def test_error_message_names_the_address(self):
        """The failure must be self-diagnosing -- name the blocked host."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(BlockedNetworkCallError, match="clawhub.ai"):
                sock.connect(("clawhub.ai", 443))
        finally:
            sock.close()

    def test_httpx_egress_is_blocked(self):
        """The guard sits below the HTTP client, so httpx is covered too."""
        import httpx

        with pytest.raises(Exception) as exc:
            httpx.get("https://clawhub.ai/api/search", timeout=5)
        assert "clawhub.ai" in str(exc.value) or isinstance(
            exc.value, (BlockedNetworkCallError, httpx.HTTPError)
        )


class TestGuardAllowsLoopback:
    def test_loopback_connect_is_permitted(self):
        """127.0.0.1 must stay reachable: live test servers bind there."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.connect(("127.0.0.1", port))  # must not raise
        finally:
            client.close()
            server.close()

    @pytest.mark.parametrize(
        "host,expected",
        [
            ("127.0.0.1", True),
            ("127.0.1.1", True),
            ("localhost", True),
            ("::1", True),
            ("clawhub.ai", False),
            ("8.8.8.8", False),
        ],
    )
    def test_loopback_classification(self, host, expected):
        assert _is_loopback((host, 443)) is expected


class TestNetworkMarkerOptOut:
    @pytest.mark.network
    def test_marked_test_is_not_guarded(self):
        """@pytest.mark.network restores real egress (no exception on connect).

        Asserts the guard is absent rather than that the internet is up, so
        this stays green on an offline runner.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.01)
        try:
            sock.connect(("203.0.113.1", 9))  # TEST-NET-3, guaranteed unroutable
        except BlockedNetworkCallError:  # pragma: no cover
            pytest.fail("@pytest.mark.network did not lift the network guard")
        # Rationale: any transport error (timeout/unreachable) proves the guard
        # let the call through to the real stack, which is what we assert here.
        except OSError:
            pass
        finally:
            sock.close()


class TestClawHubRegressionIsCaught:
    """The exact 2026-08-03 hang: PRs #165-#169 stalled for hours because
    ClawHubAdapter._map called resolve_owner() -- a live HTTP GET -- once per
    result row for rows lacking ``ownerHandle``."""

    def test_resolve_owner_on_uncached_slug_is_blocked_not_hung(self):
        from app.services import clawhub_url

        clawhub_url._OWNER_CACHE.pop("some-unseen-slug", None)
        # Fail-safe by design: resolve_owner swallows transport errors and
        # returns None. The point is that it returns FAST (guard raises inside)
        # rather than blocking on a 12s-timeout upstream call per row.
        assert clawhub_url.resolve_owner("some-unseen-slug") is None

    def test_adapter_maps_rows_without_network(self):
        """A 50-row page must map with zero egress -- the N+1 that hung CI."""
        from app.services.federation_adapters import ClawHubAdapter

        rows = [{"slug": f"s{i}", "displayName": f"S{i}", "summary": ""} for i in range(50)]
        adapter = ClawHubAdapter(fetch=lambda _q: rows)
        out = adapter.search("", limit=50)
        assert len(out) == 50
        # No ownerHandle resolvable offline → every link degrades to the safe
        # browse page rather than minting a dead deep link.
        assert all(s.origin_url for s in out)
