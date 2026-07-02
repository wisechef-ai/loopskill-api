"""Phase 0 (loopskill_activate_0701): the reconcile CLI must call the route the
server actually mounts.

Live-prod finding (2026-07-02): the CLI posted to ``/api/reconcile`` (flat),
but ``app/reconcile_routes.py`` registers the handler as
``POST /{cookbook_id}/reconcile`` dual-mounted under ``/api/bundles`` and
``/api/cookbooks`` — so every real CLI run 404'd. The evergreen_j tests never
caught it because their fake opener matched the CLI's own (wrong) URL instead
of the server's route table. These tests pin the URL contract from the SERVER
side: the CLI's URL must resolve against the real FastAPI route table.
"""

from __future__ import annotations

import json
import urllib.request

from app.reconcile_cli import _post_reconcile


class _CaptureOpener:
    """Opener that records the URL and returns an empty 200 body."""

    def __init__(self) -> None:
        self.url: str | None = None

    def __call__(self, req: urllib.request.Request):
        self.url = req.full_url

        class _Resp:
            status = 200

            def read(self) -> bytes:
                return json.dumps({"diff": {}, "generation": "g1"}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()


def test_cli_posts_to_the_mounted_bundle_route() -> None:
    cap = _CaptureOpener()
    _post_reconcile("https://api.example", "cb-123", "", [], "rec_key", opener=cap)
    assert cap.url == "https://api.example/api/bundles/cb-123/reconcile"


def test_cli_url_exists_in_server_route_table() -> None:
    """The URL shape the CLI generates must match a real mounted route."""
    from app.reconcile_routes import router

    paths = {r.path for r in router.routes}
    assert "/api/bundles/{cookbook_id}/reconcile" in paths
    # And the FLAT path the CLI used to hit must NOT be assumed to exist.
    assert "/api/reconcile" not in paths
