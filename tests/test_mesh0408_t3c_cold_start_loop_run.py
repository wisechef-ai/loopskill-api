"""mesh_0408 T3-C — the README's headline quickstart 404s on a cold clone.

ROOT CAUSE (established 2026-08-05 by a real cold-clone repro, not inference):

The DUAL-MOUNT contract itself (``app/verifier_routes.py::_build_router``,
mounted by ``app.main.create_app`` via ``app.loop_routes.router`` — see
``app/loop_routes.py`` lines 25-29) is correctly wired in the source tree on
``origin/main``. A genuine ``docker compose build --no-cache`` + fresh-clone
cold start DOES serve ``POST /api/loops/{slug}/run`` and
``POST /api/verifiers/{slug}/run`` — verified live, twice (once with a normal
cached rebuild, once with a fully independent `git clone` + `--no-cache`
build), both returning HTTP 200 with `passed: true` for the seeded
`hello-world-loop`.

The actual defect is in ``README.md`` itself: the auth header line the
Quickstart tells a stranger to copy-paste is

    -H "x-api-key: *** \

— a redaction placeholder (`***`) was committed in place of the literal
`${API_KEY}`/example token, but the trailing backslash + missing closing
double-quote make the *placeholder itself* syntactically invalid shell: a
stranger who pastes the block verbatim gets "unexpected EOF while looking
for matching `"`" from their shell — not even a 404, the request never goes
out. (Confirmed by ``git blame``: the line was introduced by commit
fd48137fe548c28496d6df5dd00436ca74b3e005, "README self-host honesty —
separate quickstart from production requirements (#64) (#70)", which split
the quickstart out of the production section but left the header value as an
unclosed placeholder instead of a real dev-key reference.)

Because the ROUTES are fine and it is the DOCS that lie, this is outcome
(b): the README is corrected (not the routing code) to end in a command a
stranger can literally copy-paste and get a non-error response from a cold
clone with zero config — while ALSO pinning route-introspection coverage so
any FUTURE regression that narrows the dual-mount surface (the shape the
task brief hypothesized, and which is a real risk given how many routers
``app.main.create_app`` wires by hand) fails loudly here instead of only
being caught by a stranger on GitHub.

Tests:
  * test_documented_loop_run_route_is_reachable — the literal README route,
    POST /api/loops/{slug}/run, must not 404.
  * test_dual_mount_contract_both_prefixes_serve_same_handler — the
    docstring's claim: /api/loops/{slug}/run and /api/verifiers/{slug}/run
    must both be reachable and resolve to the SAME endpoint function.
  * test_readme_quickstart_command_is_valid_shell — the exact bytes of the
    README's documented curl command must be syntactically valid shell (this
    is the regression test for the actual, root-caused defect).
  * TestRouteIntrospectionDoesNotShrink — enumerates app.routes and fails if
    any of the documented loop/verifier paths go missing, so a future PR
    that narrows route registration is caught here instead of by a
    stranger's cold clone.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"

# The documented dual-mount contract (app/verifier_routes.py::_build_router,
# docstring lines ~17-22): every one of these (method, path-template) pairs
# must be mounted under BOTH /api/loops and /api/verifiers.
_DUAL_MOUNT_ROUTES: list[tuple[str, str]] = [
    ("GET", ""),
    ("GET", "/{slug}"),
    ("POST", ""),
    ("POST", "/{slug}/run"),
    ("POST", "/{slug}/rate"),
]
_DUAL_MOUNT_PREFIXES = ("/api/loops", "/api/verifiers")


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from app.config import settings
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, headers={"x-api-key": settings.API_KEY})


def _mk_verifier(db, *, slug="hello-world-loop"):
    from app.models import Verifier

    v = Verifier(
        id=uuid.uuid4(),
        slug=slug,
        title="Hello World Loop",
        description="the 30-second proof a loop runs",
        is_public=True,
        success_condition="the thing was done",
        verification_script="true",
        max_turns=1,
        stopping_criteria={"success": "done", "failure": "error", "budget": None},
        tool_allowlist=[],
        system_prompt="You are a verifier.",
        tags=[],
    )
    db.add(v)
    db.commit()
    return v


# ---------------------------------------------------------------------------
# Regression test for the reported defect: the documented route must not 404.
# ---------------------------------------------------------------------------


def test_documented_loop_run_route_is_reachable(middleware_client, db_session):
    """POST /api/loops/{slug}/run — the README's headline quickstart command.

    This is the regression test for the reported cold-start defect. A 404
    here means a stranger following the README verbatim gets stopped dead on
    the one command that demonstrates the product.
    """
    _mk_verifier(db_session)
    resp = middleware_client.post("/api/loops/hello-world-loop/run")
    assert resp.status_code != 404, (
        "POST /api/loops/{slug}/run 404'd — this is the exact cold-start "
        f"defect from the README quickstart. Body: {resp.text}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["passed"] is True
    assert body["loop_slug"] == "hello-world-loop"


# ---------------------------------------------------------------------------
# Dual-mount contract: both prefixes reachable, same handler.
# ---------------------------------------------------------------------------


def test_dual_mount_contract_both_prefixes_serve_same_handler(middleware_client, db_session):
    """The verifier_routes.py docstring claims routes are mounted under BOTH
    /api/loops (compat) and /api/verifiers (canonical), bound to the SAME
    handler callable. Assert both are reachable AND identical in behaviour
    (byte-identical payload modulo the run_id/duration_seconds that vary
    per-execution)."""
    _mk_verifier(db_session)

    loops_resp = middleware_client.post("/api/loops/hello-world-loop/run")
    verifiers_resp = middleware_client.post("/api/verifiers/hello-world-loop/run")

    assert loops_resp.status_code == 200, loops_resp.text
    assert verifiers_resp.status_code == 200, verifiers_resp.text

    loops_body = loops_resp.json()
    verifiers_body = verifiers_resp.json()

    # Strip the fields that legitimately vary per-invocation (run_id,
    # duration_seconds) before comparing — everything else must match
    # exactly, proving both prefixes hit the same handler / same logic.
    def _stable(d: dict) -> dict:
        return {k: v for k, v in d.items() if k not in ("run_id", "duration_seconds")}

    assert _stable(loops_body) == _stable(verifiers_body)


def test_dual_mount_contract_endpoint_function_identity(middleware_client):
    """Route-object level proof (not just behavioural): the FastAPI route
    objects for /api/loops/{slug}/run and /api/verifiers/{slug}/run must
    point at the literal same Python function object — the strongest
    possible assertion of "same handler"."""
    app = middleware_client.app
    endpoints: dict[str, object] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path in ("/api/loops/{slug}/run", "/api/verifiers/{slug}/run") and "POST" in methods:
            endpoints[path] = route.endpoint

    assert set(endpoints) == {"/api/loops/{slug}/run", "/api/verifiers/{slug}/run"}, (
        f"expected both dual-mount /run routes present, found: {sorted(endpoints)}"
    )
    assert endpoints["/api/loops/{slug}/run"] is endpoints["/api/verifiers/{slug}/run"], (
        "dual-mount contract violated: /api/loops/{slug}/run and "
        "/api/verifiers/{slug}/run resolve to DIFFERENT handler functions"
    )


# ---------------------------------------------------------------------------
# The actual root-caused defect: the README's copy-pasted command is not
# even valid shell syntax (unclosed quote around the `***` redaction
# placeholder in the x-api-key header).
# ---------------------------------------------------------------------------


def _extract_readme_quickstart_command() -> str:
    """Pull the fenced ```sh block containing the documented /run curl call
    out of README.md, exactly as a stranger would copy-paste it."""
    text = README_PATH.read_text(encoding="utf-8")
    # Grab every ```sh ... ``` fenced block, then pick the one that contains
    # the hello-world-loop /run command.
    for block in re.findall(r"```sh\n(.*?)```", text, flags=re.DOTALL):
        if "/api/loops/hello-world-loop/run" in block:
            return block
    raise AssertionError(
        "Could not find the /api/loops/hello-world-loop/run quickstart "
        "code block in README.md — has the Quickstart section moved?"
    )


def test_readme_quickstart_command_is_valid_shell():
    """Regression test for the ROOT-CAUSED defect: the README's headline
    quickstart command must be syntactically valid shell, not just
    'the route exists'. Before the fix, the -H header line was

        -H "x-api-key: *** \\

    — an unclosed double-quote around the `***` redaction placeholder — so a
    stranger pasting the block verbatim got a shell parse error
    ('unexpected EOF while looking for matching `"`') before the request
    ever reached the server.
    """
    command = _extract_readme_quickstart_command()

    # `***` is a literal redaction placeholder, never valid inside a curl
    # header value — if it's still there, the README hasn't been fixed.
    assert "***" not in command, (
        "README quickstart still contains an unclosed '***' redaction "
        f"placeholder in the curl command:\n{command}"
    )

    result = subprocess.run(
        ["bash", "-n"],
        input=command,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "README quickstart command is not valid shell syntax — a stranger "
        f"pasting it verbatim gets a shell error, not an HTTP response.\n"
        f"Command:\n{command}\nbash -n stderr:\n{result.stderr}"
    )


def test_readme_quickstart_references_documented_route():
    """Sanity: the README must still document the exact route this test
    file pins reachability for (POST /api/loops/{slug}/run)."""
    text = README_PATH.read_text(encoding="utf-8")
    assert "curl -X POST localhost:8200/api/loops/hello-world-loop/run" in text


# ---------------------------------------------------------------------------
# Route-introspection: fail loudly if the mounted surface ever shrinks.
# ---------------------------------------------------------------------------


class TestRouteIntrospectionDoesNotShrink:
    """Enumerate app.routes directly (not behaviourally) and assert every
    documented dual-mount loop/verifier path is present. This is the check
    that would have caught a REAL registration regression (as opposed to the
    doc-only defect actually found) — if a future change drops the
    /api/loops compat mount, or /run, or /rate, this fails here instead of
    on a stranger's cold clone.
    """

    @staticmethod
    def _mounted_paths(app) -> set[tuple[str, str]]:
        mounted: set[tuple[str, str]] = set()
        for route in app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            if path is None:
                continue
            for method in methods:
                mounted.add((method, path))
        return mounted

    def test_all_documented_dual_mount_routes_present(self, middleware_client):
        mounted = self._mounted_paths(middleware_client.app)

        missing: list[str] = []
        for method, suffix in _DUAL_MOUNT_ROUTES:
            for prefix in _DUAL_MOUNT_PREFIXES:
                path = f"{prefix}{suffix}" if suffix else prefix
                if (method, path) not in mounted:
                    missing.append(f"{method} {path}")

        assert not missing, (
            "Dual-mount contract regression: the following documented "
            f"routes are no longer mounted: {missing}"
        )

    def test_run_route_present_under_both_prefixes_specifically(self, middleware_client):
        """Narrow, high-signal check mirroring the exact defect shape the
        cold-clone bug report hypothesized (dual-mount routes not fully
        registered) — isolated from the other dual-mount routes so a future
        failure here is unambiguous."""
        mounted = self._mounted_paths(middleware_client.app)
        assert ("POST", "/api/loops/{slug}/run") in mounted
        assert ("POST", "/api/verifiers/{slug}/run") in mounted
