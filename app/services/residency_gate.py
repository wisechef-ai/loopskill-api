"""Residency gate — activate_0701 Phase E.

Fail-closed server-side residency enforcement. EU-resident fleets cannot receive
non-EU-tagged connectors or composite loops with derived non-EU residency.
"""

from __future__ import annotations

from typing import Any


def filter_diff_by_residency(
    diff: dict[str, Any],
    fleet_residency: str | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Filter a reconcile diff by fleet residency constraints.

    Args:
        diff: the reconcile diff dict with sections for skills, connectors,
              composite_loops (any may be absent).
        fleet_residency: "eu" | "row" | None (None = unrestricted).

    Returns:
        (filtered_diff, blocked_reasons) — filtered_diff has offending entries
        removed; blocked_reasons lists what was filtered and why.
    """
    if fleet_residency is None or fleet_residency == "row":
        return diff, []

    blocked: list[dict[str, str]] = []
    filtered = dict(diff)

    if fleet_residency == "eu":
        # Filter connectors with residency_tag="non-eu"
        conn_sections = ("add", "update")
        for section in conn_sections:
            conns = list(filtered.get(section, []))
            kept = []
            for c in conns:
                tag = c.get("residency_tag")
                if tag == "non-eu":
                    blocked.append(
                        {
                            "type": "connector",
                            "slug": str(c.get("slug", "?")),
                            "section": section,
                            "reason": "residency_blocked_non_eu",
                        }
                    )
                else:
                    kept.append(c)
            filtered[section] = kept

        # Filter composite loops with derived residency="non-eu"
        for section in conn_sections:
            loops = list(filtered.get("composite_loops", {}).get(section, []))
            kept_loops = []
            for cl in loops:
                residency = cl.get("residency")
                if residency == "non-eu":
                    blocked.append(
                        {
                            "type": "composite_loop",
                            "slug": str(cl.get("slug", "?")),
                            "section": section,
                            "reason": "residency_blocked_non_eu",
                        }
                    )
                else:
                    kept_loops.append(cl)
            if "composite_loops" in filtered and isinstance(filtered["composite_loops"], dict):
                cl_dict = dict(filtered["composite_loops"])
                cl_dict[section] = kept_loops
                filtered["composite_loops"] = cl_dict
    else:
        # Unknown residency value — fail-closed: block anything non-null-tagged
        for section in ("add", "update"):
            conns = list(filtered.get(section, []))
            kept = [c for c in conns if not c.get("residency_tag")]
            for c in conns:
                if c.get("residency_tag"):
                    blocked.append(
                        {
                            "type": "connector",
                            "slug": str(c.get("slug", "?")),
                            "section": section,
                            "reason": "residency_blocked_unknown_fleet",
                        }
                    )
            filtered[section] = kept

    return filtered, blocked
