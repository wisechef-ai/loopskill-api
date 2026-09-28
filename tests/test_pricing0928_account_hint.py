"""pricing0928 (t_7f5808d2, option C): one account line on the anonymous install path.

Pins: anonymous installs still succeed (no gate) and carry ``account_hint``;
signed-in callers get none; the link uses the utm_* channel that survives the
OAuth hop (not ?ref=, which /signin drops); the copy makes no claim the product
cannot back (no "alerts"); and the /skill meta-skill tells agents to relay it.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.auth_ctx import AuthContext
from app.services.account_hint import account_hint, signin_url
from tests.test_tier_semantics_flip import (  # noqa: F401 — fixtures re-exported
    _make_skill,
    _make_user_with_key,
    db,
    engine_fixture,
    patched_client,
)

SKILL_MD = Path(__file__).resolve().parent.parent / "docs" / "recipes-skill" / "SKILL.md"


def test_anonymous_install_is_not_gated_and_carries_the_hint(patched_client, db):  # noqa: F811
    _make_skill(db, slug="hint-me", title="Hint", tier="free")
    resp = patched_client.get("/api/skills/install?slug=hint-me")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tarball_url"]
    hint = body["account_hint"]
    assert "hint-me" in hint and "/signin?next=/library" in hint
    assert "never needs an account" in hint


def test_signed_in_install_gets_no_hint(patched_client, db):  # noqa: F811
    _make_skill(db, slug="hint-keyed", title="Hint", tier="free")
    key, _ = _make_user_with_key(db, tier="free")
    resp = patched_client.get("/api/skills/install?slug=hint-keyed", headers={"x-api-key": key})
    assert resp.status_code == 200, resp.text
    assert resp.json().get("account_hint") is None


@pytest.mark.parametrize("scope", ["user", "master", "fleet", "cbt_token"])
def test_hint_is_anonymous_only(scope):
    assert account_hint("x", AuthContext(scope=scope)) is None
    assert account_hint("x", None) is not None
    assert account_hint("x", AuthContext.anonymous()) is not None


def test_signin_link_uses_utm_not_referral_ref():
    q = parse_qs(urlparse(signin_url("super-memory")).query)
    assert q["next"] == ["/library"]
    assert q["utm_source"] == ["install"]
    assert q["utm_campaign"] == ["super-memory"]
    assert "ref" not in q  # ?ref= is the WIS-660 referral code; /signin drops install:<slug>


def test_hostile_slug_never_lands_in_the_link():
    q = parse_qs(urlparse(signin_url("x&next=//evil.example")).query)
    assert q["next"] == ["/library"]
    assert q["utm_campaign"] == ["unknown"]


def test_copy_makes_no_alert_claim():
    """Nothing notifies an installer about breakage today; the hint must not say so."""
    text = account_hint("x").lower()
    assert "alert" not in text and "notif" not in text


def test_meta_skill_tells_agents_to_relay_the_hint():
    body = SKILL_MD.read_text(encoding="utf-8")
    assert "account_hint" in body
    assert "Never block or delay an install on it" in body
