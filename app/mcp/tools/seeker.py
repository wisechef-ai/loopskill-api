"""recipes_seeker — Phase K MCP tool.

Probes the local machine for vendor-installed skills (Claude, Codex,
Hermes, OpenCode), parses their SKILL.md frontmatter, and diffs the
result against the public catalog so the agent can recommend upgrades.

READ-ONLY on vendor directories.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import Skill
from app.seeker import (
    diff_against_catalog,
    installed_to_dict,
    recommendation_to_dict,
    scan_vendor,
    vendor_paths,
)


def recipes_seeker(db: Session, **_: Any) -> dict[str, Any]:
    """Probe local vendor skill directories and diff against the public catalog."""
    # Public-scope MCP tool: read-only probe of caller's local vendor dirs; no server data exposed.
    paths = vendor_paths()
    vendors_found: dict[str, list[dict[str, Any]]] = {}
    all_installed = []
    unsupported: list[str] = []

    for name, p in paths.items():
        if not p.exists():
            unsupported.append(name)
            continue
        skills = scan_vendor(p, vendor=name)
        vendors_found[name] = [installed_to_dict(s) for s in skills]
        all_installed.extend(skills)

    catalog = db.query(Skill).filter(Skill.is_public.is_(True)).all()
    recs = diff_against_catalog(all_installed, catalog)

    return {
        "vendors": vendors_found,
        "recommendations": [recommendation_to_dict(r) for r in recs],
        "unsupported_paths": unsupported,
    }
