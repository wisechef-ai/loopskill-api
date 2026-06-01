"""Recipes MCP tool implementations.

Each tool is a plain async function ``(db: Session, **args) -> dict`` so the
same callable serves the SSE transport, the stdio transport, and unit tests.
"""

from app.mcp.tools.carousel_today import recipes_carousel_today
from app.mcp.tools.cookbook_install import (
    CookbookInstallError,
    recipes_cookbook_install,
)
from app.mcp.tools.doctor import recipes_doctor
from app.mcp.tools.feedback import recipes_feedback
from app.mcp.tools.fleet import (
    recipes_fleet_create,
    recipes_fleet_list,
    recipes_fleet_subscribe,
    recipes_fleet_sync,
)
from app.mcp.tools.install import recipes_install
from app.mcp.tools.list_cookbook import recipes_list_cookbook
from app.mcp.tools.publish_request import recipes_publish_request
from app.mcp.tools.recall import recipes_recall
from app.mcp.tools.recipes_sync import recipes_sync
from app.mcp.tools.recipify import recipes_recipify
from app.mcp.tools.recipify_request import recipes_request_recipe
from app.mcp.tools.search import recipes_search
from app.mcp.tools.seeker import recipes_seeker
from app.mcp.tools.share import (
    recipes_share_create,
    recipes_share_list,
    recipes_share_revoke,
    recipes_share_rotate,
)
from app.mcp.tools.skill_error import recipes_report_skill_error
from app.mcp.tools.skill_patch import recipes_propose_skill_patch
from app.mcp.tools.subrecipe_resolve import recipes_subrecipe_resolve
from app.mcp.tools.fork_deploy import recipes_cookbook_attach, recipes_tailor_version
from app.mcp.tools.tailor import recipes_fork_list, recipes_tailor

__all__ = [
    "recipes_search",
    "recipes_install",
    "recipes_list_cookbook",
    "recipes_recall",
    "recipes_recipify",
    "recipes_carousel_today",
    "recipes_subrecipe_resolve",
    "recipes_doctor",
    "recipes_seeker",
    "recipes_sync",
    "recipes_feedback",
    "recipes_request_recipe",
    "recipes_report_skill_error",
    "recipes_propose_skill_patch",
    # Phase D: share-token MCP tools
    "recipes_share_create",
    "recipes_share_list",
    "recipes_share_revoke",
    "recipes_share_rotate",
    # Phase E: fleet tools
    "recipes_fleet_create",
    "recipes_fleet_subscribe",
    "recipes_fleet_sync",
    "recipes_fleet_list",
    # Phase C: publish-request MCP tool
    "recipes_publish_request",
    # cookbook_share_2105 Phase F: cookbook-scoped install
    "recipes_cookbook_install",
    "CookbookInstallError",
    # integrator_2905 W1: tailor/fork tools
    "recipes_fork_list",
    "recipes_tailor",
    # loopclose_3005 Phase C: close the MCP tailor loop
    "recipes_tailor_version",
    "recipes_cookbook_attach",
]
