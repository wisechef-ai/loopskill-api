"""Outbound-network guard for the test suite.

Why this exists (incident 2026-08-03)
-------------------------------------
Five Dependabot PRs (#165-#169) sat with the ``pytest + coverage`` job
``in_progress`` for hours. None of them touched application code — #169's whole
diff is one line of ``requirements.txt``. The tests were hanging on a LIVE
network call.

``ClawHubAdapter._map`` enriches each result row with an owner handle::

    owner = row.get("ownerHandle") or clawhub_url.resolve_owner(str(slug))

``resolve_owner`` issues a real ``GET https://clawhub.ai/api/search`` for any
row that lacks ``ownerHandle``. Tests that exercise ``/api/skills/external``
inject synthetic rows which deliberately have no ``ownerHandle``, so a 50-row
fixture fired 50 sequential upstream requests. That was merely slow while
clawhub.ai was fast; when the upstream degraded (measured 3.7 s/request on
2026-08-03) the same fixture became 50 x 3.7 s per test, and with a 12 s
transport timeout (``federation_live._HTTP_TIMEOUT_S``) the worst case is 600 s
per test. Multiplied across the suite that exceeds any CI budget, so the job
never returned and no failure was ever reported.

The defect is not the dependency bump and not the adapter. It is that the test
suite was allowed to reach the public internet at all: CI correctness became a
function of a third party's uptime.

What this guard does
--------------------
Blocks outbound TCP to anything that is not loopback, and fails LOUDLY and
INSTANTLY with the offending address instead of hanging. Loopback stays open on
purpose -- ``tests/test_metasearch_p5_reconcile_target.py`` binds a real uvicorn
server on ``127.0.0.1`` because its urllib-based harness cannot run against
``TestClient``.

A test that genuinely must reach the internet can opt out with
``@pytest.mark.network``, but note that such a test is by definition
non-hermetic and should not gate a merge.
"""

from __future__ import annotations

import socket

# Hostnames/IPs that remain reachable: in-process test servers bind here.
_ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", "", "::"})


class BlockedNetworkCallError(RuntimeError):
    """Raised when a test attempts to reach a non-loopback host."""


def _is_loopback(address: object) -> bool:
    """True when ``address`` is a loopback endpoint (or a non-IP socket)."""
    if not isinstance(address, tuple) or not address:
        # AF_UNIX and friends carry a str/bytes path -- not internet egress.
        return True
    host = address[0]
    if not isinstance(host, (str, bytes)):
        return True
    if isinstance(host, bytes):
        host = host.decode("utf-8", "replace")
    return host in _ALLOWED_HOSTS or host.startswith("127.")


def install(monkeypatch) -> None:
    """Patch ``socket`` so non-loopback connects raise instead of hanging."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _guarded_connect(self, address, *args, **kwargs):
        if not _is_loopback(address):
            raise BlockedNetworkCallError(
                f"Blocked outbound network call to {address!r} during a test.\n"
                "The test suite must be hermetic -- CI correctness cannot depend "
                "on a third party's uptime.\n"
                "Fix by stubbing the client (e.g. monkeypatch "
                "app.services.clawhub_url.resolve_owner), or mark the test "
                "@pytest.mark.network if it is genuinely an integration probe."
            )
        return real_connect(self, address, *args, **kwargs)

    def _guarded_connect_ex(self, address, *args, **kwargs):
        if not _is_loopback(address):
            raise BlockedNetworkCallError(f"Blocked outbound network call to {address!r} during a test.")
        return real_connect_ex(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex, raising=True)
