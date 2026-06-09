"""spotify_0608 Ph D tool definitions — streaming cookbook-composition verbs.

Split out to keep registry.py under the 600-line god-object gate (same pattern
as _registry_j.py).
"""

from __future__ import annotations

import mcp.types as types


def _phase_d_tools() -> list[types.Tool]:
    """Return the spotify_0608 Ph D (streaming composition) tool definitions."""
    return [
        types.Tool(
            name="recipes_install_from_cookbook",
            description=(
                "Install every skill in a PUBLIC cookbook from one link. The "
                "'streaming' install verb — pass a cookbook link "
                "(cookbook://<slug>, cookbook:<slug>, or a bare slug) and get "
                "every skill's install line in one call. Anonymous-reachable: a "
                "public cookbook's skills stream to anyone, like a public "
                "playlist's tracks. This is the one-line clone the public "
                "cookbook page surfaces."
            ),
            inputSchema={
                "type": "object",
                "required": ["link"],
                "properties": {
                    "link": {
                        "type": "string",
                        "description": (
                            "Public cookbook link: cookbook://<slug>, "
                            "cookbook:<slug>, or a bare slug. A trailing "
                            "?ref=<creator> is tolerated and stripped."
                        ),
                    },
                },
            },
        ),
        types.Tool(
            name="recipes_pick_best_from_cookbook",
            description=(
                "Pick the single best skill from a PUBLIC cookbook for a stated "
                "need. The 'streaming' choose verb — pass a cookbook link and an "
                "optional 'need' description; returns the best-matching skill "
                "(keyword relevance, then real 7d/total installs) plus its "
                "install line, and the full ranked list. With no 'need', ranks "
                "the whole cookbook by installs."
            ),
            inputSchema={
                "type": "object",
                "required": ["link"],
                "properties": {
                    "link": {
                        "type": "string",
                        "description": "Public cookbook link (cookbook://<slug> / cookbook:<slug> / bare slug).",
                    },
                    "need": {
                        "type": "string",
                        "description": "Optional natural-language description of what the agent needs.",
                    },
                },
            },
        ),
        types.Tool(
            name="recipes_compose_cookbook_from_links",
            description=(
                "Compose a NEW cookbook (owned by you) from N links in one call. "
                "The 'streaming' compose verb — each link can be a public "
                "cookbook (cookbook://<slug> → all its skills), an internal "
                "catalogue skill (skill://<slug>), or an external federated "
                "skill (<source>:<slug>, e.g. clawhub:web-scraper). The "
                "de-duplicated union becomes a new private cookbook you own; "
                "publish it to get a shareable cookbook:// link. Requires an "
                "authenticated user; honors your tier's cookbook cap."
            ),
            inputSchema={
                "type": "object",
                "required": ["links"],
                "properties": {
                    "links": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "1–25 links to compose from (cookbook / skill / external).",
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional name for the new cookbook (default auto-generated).",
                    },
                },
            },
        ),
    ]
