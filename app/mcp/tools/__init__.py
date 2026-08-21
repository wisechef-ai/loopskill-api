"""Recipes MCP tool implementations.

Each tool is a plain async function ``(db: Session, **args) -> dict`` so the
same callable serves the SSE transport, the stdio transport, and unit tests.
"""

from app.mcp.tools.configure_feedback import loopskill_configure_feedback
from app.mcp.tools.bundle_install import (
    CookbookInstallError,
    loopskill_bundle_install,
)
from app.mcp.tools.bundle_stream import (
    loopskill_compose_bundle_from_links,
    loopskill_install_from_bundle,
    loopskill_pick_best_from_bundle,
)
from app.mcp.tools.doctor import loopskill_doctor
from app.mcp.tools.feedback import loopskill_feedback
from app.mcp.tools.fleet import (
    loopskill_fleet_create,
    loopskill_fleet_list,
    loopskill_fleet_subscribe,
    loopskill_fleet_sync,
)
from app.mcp.tools.install import loopskill_install
from app.mcp.tools.like import loopskill_like
from app.mcp.tools.list_cookbook import loopskill_list_bundle
from app.mcp.tools.publish_request import loopskill_publish_request
from app.mcp.tools.recall import loopskill_recall
from app.mcp.tools.loopskill_sync import loopskill_sync
from app.mcp.tools.recipify import loopskill_skillify
from app.mcp.tools.recipify_request import loopskill_request_skill
from app.mcp.tools.search import loopskill_search
from app.mcp.tools.seeker import loopskill_seeker
from app.mcp.tools.share import (
    loopskill_share_create,
    loopskill_share_list,
    loopskill_share_revoke,
    loopskill_share_rotate,
)
from app.mcp.tools.skill_error import loopskill_report_skill_error
from app.mcp.tools.skill_patch import loopskill_propose_skill_patch
from app.mcp.tools.subrecipe_resolve import loopskill_subskill_resolve
from app.mcp.tools.fork_deploy import loopskill_bundle_attach, loopskill_tailor_version
from app.mcp.tools.tailor import loopskill_fork_list, loopskill_tailor
from app.mcp.tools.bundle_handoff import loopskill_bundle_handoff  # compat-alias
from app.mcp.tools.loopskill_catalog import (
    loopskill_get_loop,
    loopskill_get_personality,
    loopskill_search_loops,
    loopskill_search_personalities,
)
from app.mcp.tools.connector_publish import loopskill_connector_publish
from app.mcp.tools.federation_propose import loopskill_propose_registry

# activate_0701 Phase A2 — composite loop catalog (NEW MCP names, council §6).
from app.mcp.tools.composite_loop_catalog import (
    loopskill_get_composite_loop,
    loopskill_publish_composite_loop,
    loopskill_search_composite_loops,
)

# flywheel P1 F1.1 — closes the compose→publish MCP dead-end.
from app.mcp.tools.bundle_publish import loopskill_publish_bundle

__all__ = [
    "loopskill_search",
    "loopskill_install",
    "loopskill_like",
    "loopskill_list_bundle",
    "loopskill_recall",
    "loopskill_skillify",
    "loopskill_subskill_resolve",
    "loopskill_doctor",
    "loopskill_seeker",
    "loopskill_sync",
    "loopskill_feedback",
    "loopskill_request_skill",
    "loopskill_report_skill_error",
    "loopskill_propose_skill_patch",
    # Phase D: share-token MCP tools
    "loopskill_share_create",
    "loopskill_share_list",
    "loopskill_share_revoke",
    "loopskill_share_rotate",
    # Phase E: fleet tools
    "loopskill_fleet_create",
    "loopskill_fleet_subscribe",
    "loopskill_fleet_sync",
    "loopskill_fleet_list",
    # Phase C: publish-request MCP tool
    "loopskill_publish_request",
    # cookbook_share_2105 Phase F: bundle-scoped install  # compat-alias
    "loopskill_bundle_install",
    "CookbookInstallError",
    # spotify_0608 Ph D: streaming bundle-composition verbs
    "loopskill_install_from_bundle",
    "loopskill_pick_best_from_bundle",
    "loopskill_compose_bundle_from_links",
    # integrator_2905 W1: tailor/fork tools
    "loopskill_fork_list",
    "loopskill_tailor",
    # loopclose_3005 Phase C: close the MCP tailor loop
    "loopskill_tailor_version",
    "loopskill_bundle_attach",
    # loopclose_3005 Phase I: bundle handoff
    "loopskill_bundle_handoff",
    # loopclose_3005 Phase J: user-routable feedback (THE MOAT)
    "loopskill_configure_feedback",
    # loopskill_0622 Phase 8: runnable catalog types (loops + personalities)
    "loopskill_search_loops",
    "loopskill_get_loop",
    "loopskill_search_personalities",
    "loopskill_get_personality",
    # activate_0701 Phase A2: composite loop catalog (NEW MCP names)
    "loopskill_publish_composite_loop",
    "loopskill_get_composite_loop",
    "loopskill_search_composite_loops",
    # activate_0701 Phase B: connector publish tool
    "loopskill_connector_publish",
    # bundles_0811 Phase P3.5: self-serve federation registry proposals
    "loopskill_propose_registry",
    # flywheel P1 F1.1: closes the compose→publish MCP dead-end
    "loopskill_publish_bundle",
]
