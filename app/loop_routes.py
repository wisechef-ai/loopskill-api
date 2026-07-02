"""Loop registry routes — COMPATIBILITY SHIM (loopskill_activate_0701 Phase A1).

This module is now a thin compatibility shim. The canonical implementation
lives in ``app/verifier_routes``. The shipped safety-bounded autonomous agent
artifact was renamed *loop* → *verifier* in Phase A1, but the public ``/api/loops``
prefix is kept as a compatibility surface (NO 301 redirect — the council report
§3 mandates dual-mount behaviour with byte-identical payloads).

``loop_routes.router`` IS ``verifier_routes.router`` — the same router object
serves both prefixes. ``app.main.create_app`` mounts it under ``/api/loops``
in addition to the canonical ``/api/verifiers`` mount. Old imports of handler
functions (``list_loops``, ``get_loop``, etc.) still resolve.  # compat-alias
"""

from __future__ import annotations

from app.verifier_routes import (  # noqa: F401  # compat-alias
    get_verifier as get_loop,
    list_verifiers as list_loops,
    publish_verifier as publish_loop,
    rate_verifier as rate_loop,
    run_verifier as run_loop,
)

# Compat: ``loop_routes.router`` IS ``verifier_routes.router`` — the SAME object.
# ``app.main.create_app`` mounts this router under both ``/api/verifiers`` and
# ``/api/loops``. This guarantees byte-identical payloads and handler identity
# across both prefixes (council §6 condition 3).  # compat-alias
from app.verifier_routes import router  # noqa: F401  # compat-alias
