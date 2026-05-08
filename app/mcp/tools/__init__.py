"""Recipes MCP tool implementations.

Each tool is a plain async function ``(db: Session, **args) -> dict`` so the
same callable serves the SSE transport, the stdio transport, and unit tests.
"""

from app.mcp.tools.search import recipes_search
from app.mcp.tools.install import recipes_install
from app.mcp.tools.list_cookbook import recipes_list_cookbook
from app.mcp.tools.recall import recipes_recall
from app.mcp.tools.recipify import recipes_recipify
from app.mcp.tools.carousel_today import recipes_carousel_today
from app.mcp.tools.subrecipe_resolve import recipes_subrecipe_resolve
from app.mcp.tools.doctor import recipes_doctor
from app.mcp.tools.seeker import recipes_seeker
from app.mcp.tools.recipes_sync import recipes_sync
from app.mcp.tools.feedback import recipes_feedback
from app.mcp.tools.recipify_request import recipes_request_recipe
from app.mcp.tools.skill_error import recipes_report_skill_error

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
]
