"""UTM short-link redirectors — /x/, /li/, /ig/, /yt/, /fb/.

Extracted from app/routes.py (Phase E — secfix_1905).

These are ROOT-level routes (no /api prefix). marketing_1205: X (Twitter)
and other platforms strip query params from short links, so we provide
/x/<slug>, /li/<slug> etc. that 302 to the portal skill page and set a
UTM-ref httpOnly cookie in the same request.

Mounting: main.py includes utm_router WITHOUT any prefix so paths remain
bare /x/{slug}, /li/{slug} etc.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app._skill_helpers import _set_utm_ref_cookie

utm_router = APIRouter(tags=["skills"])


for _platform in ("x", "li", "ig", "yt", "fb"):

    def _make_redirect(ref_val: str):
        @utm_router.get(f"/{ref_val}/{{skill_slug}}", include_in_schema=False)
        def _platform_redirect(skill_slug: str, ref_val: str = ref_val):
            # marketing_1205: set cookie BEFORE redirect so it lands on the
            # visitor on the same recipes.wisechef.ai origin, then 302 to the
            # public portal skill page (statically served by Caddy from
            # /home/wisechef/recipes-portal/dist/skills/<slug>/index.html).
            resp = RedirectResponse(
                url=f"/skills/{skill_slug}?ref={ref_val}",
                status_code=302,
            )
            _set_utm_ref_cookie(resp, ref_val)
            return resp

        _platform_redirect.__name__ = f"redirect_{ref_val}_slug"
        return _platform_redirect

    _make_redirect(_platform)
