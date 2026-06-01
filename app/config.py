"""WiseRecipes API — configuration via env vars."""

from pydantic import model_validator
from pydantic_settings import BaseSettings

# Default (insecure) values that MUST be rotated in any non-sqlite environment.
_DEFAULT_API_KEY = "rec_dev_wiserecipes_local_testing_key"
_DEFAULT_SIGNING_SECRET = "wr-tarball-signing-secret-change-me"
_DEFAULT_JWT_SECRET = "wr-jwt-secret-change-me"
_DEFAULT_HEARTBEAT_PEPPER = "wr-fleet-pepper-change-me"


def _assert_production_secrets(settings: "Settings") -> None:
    """Raise RuntimeError if any default change-me secret is present in a non-sqlite env.

    Called from Settings.__init__ via model_validator so the process refuses
    to boot rather than silently running with exploitable defaults.

    Also enforces OAUTH_REDIRECT_BASE requirements in non-sqlite envs:
    - Must be non-empty
    - Must start with 'https://'

    SQLite envs (local dev) are exempt — default values are fine there.
    """
    if "sqlite" in settings.DATABASE_URL:
        return  # dev environment — allow defaults

    insecure: list[str] = []
    if settings.API_KEY == _DEFAULT_API_KEY:
        insecure.append("API_KEY")
    if settings.SIGNING_SECRET == _DEFAULT_SIGNING_SECRET:
        insecure.append("SIGNING_SECRET")
    if settings.JWT_SECRET == _DEFAULT_JWT_SECRET:
        insecure.append("JWT_SECRET")
    if settings.HEARTBEAT_PEPPER == _DEFAULT_HEARTBEAT_PEPPER:
        insecure.append("HEARTBEAT_PEPPER")

    if insecure:
        raise RuntimeError(
            f"Refusing to boot in production with default change-me secret(s): "
            f"{', '.join(insecure)}. "
            f"Set proper values via environment variables (WR_{{NAME}})."
        )

    # Issue #4 — OAUTH_REDIRECT_BASE must be non-empty and https:// in prod
    base = settings.OAUTH_REDIRECT_BASE
    if not base:
        raise RuntimeError(
            "Refusing to boot in production: OAUTH_REDIRECT_BASE is empty. "
            "Set WR_OAUTH_REDIRECT_BASE=https://your-domain.example.com"
        )
    if not base.startswith("https://"):
        raise RuntimeError(
            f"Refusing to boot in production: OAUTH_REDIRECT_BASE must start with 'https://' "
            f"(got {base!r}). Host-header-derived OAuth redirect URIs are a security risk."
        )

    # Issue #23 (secfix_1905/H) — Stripe price IDs must not both be empty in prod.
    # Canonical fields (STRIPE_PRICE_PRO / STRIPE_PRICE_PRO_PLUS) OR the legacy
    # aliases (STRIPE_PRICE_COOK / STRIPE_PRICE_OPERATOR / STRIPE_PRICE_STUDIO)
    # must be set for each paid tier.  If both are empty the checkout flow is
    # broken and users cannot subscribe.
    _price_pairs = [
        ("STRIPE_PRICE_PRO", settings.STRIPE_PRICE_PRO, "STRIPE_PRICE_COOK", settings.STRIPE_PRICE_COOK),
        (
            "STRIPE_PRICE_PRO_PLUS",
            settings.STRIPE_PRICE_PRO_PLUS,
            "STRIPE_PRICE_OPERATOR",
            settings.STRIPE_PRICE_OPERATOR,
        ),
    ]
    missing_prices: list[str] = []
    for canonical_name, canonical_val, legacy_name, legacy_val in _price_pairs:
        if not canonical_val and not legacy_val:
            missing_prices.append(f"{canonical_name} (or legacy {legacy_name})")
    if missing_prices:
        raise RuntimeError(
            f"Refusing to boot in production: Stripe price IDs are empty for paid tiers: "
            f"{', '.join(missing_prices)}. "
            f"Set the canonical env var (WR_{{NAME}}) in .env."
        )


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://wisechef@localhost/wiserecipes"
    API_KEY: str = "rec_dev_wiserecipes_local_testing_key"  # must start with rec_
    SIGNING_SECRET: str = "wr-tarball-signing-secret-change-me"
    RATE_LIMIT_PER_MINUTE: int = 60
    REDIS_URL: str = "redis://localhost:6379/0"
    HOST: str = "0.0.0.0"
    PORT: int = 8200

    # Stripe Connect
    STRIPE_SECRET_KEY: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    # Stripe Subscription price IDs.
    # Canonical env var names (RCP-INCIDENT-2026-05-11 Phase 6, 2026-05-11):
    #   WR_STRIPE_PRICE_PRO        (€20/mo)
    #   WR_STRIPE_PRICE_PRO_PLUS   (€100/mo)
    # CANONICAL FIELDS DEFAULT TO "" so an unset .env value doesn't get masked
    # by a stale-default constant. If the canonical env var is empty,
    # _load_tier_price_ids() falls back to price_id_env_legacy
    # (WR_STRIPE_PRICE_COOK / WR_STRIPE_PRICE_OPERATOR / WR_STRIPE_PRICE_STUDIO),
    # which the host's .env still defines until 2026-06-10.
    STRIPE_PRICE_PRO: str = ""
    STRIPE_PRICE_PRO_PLUS: str = ""
    # Legacy aliases — deprecated, remove after 2026-06-10
    STRIPE_PRICE_COOK: str = ""
    STRIPE_PRICE_OPERATOR: str = ""
    STRIPE_PRICE_STUDIO: str = ""

    # Founding Integrator SKU — one-time price ID (loopclose_3005 Phase D).
    # WR_STRIPE_PRICE_FOUNDING holds the Stripe one-time price for the $1000
    # lifetime-Pro+ founding seat. Empty when founding sales aren't wired up;
    # founding_service.founding_price_id() returns "" and the checkout path
    # raises a clean 503 rather than creating a broken session.
    STRIPE_PRICE_FOUNDING: str = ""

    # GitHub OAuth
    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # OAuth redirect base URL (used to build callback URLs)
    OAUTH_REDIRECT_BASE: str = ""  # e.g. https://recipes.wisechef.ai

    # JWT for creator auth
    JWT_SECRET: str = "wr-jwt-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_HOURS: int = 72

    # G.3 — Optional multi-key JWT rotation support.
    # JWT_KEYS: JSON string mapping kid → HMAC secret, e.g.
    #   WR_JWT_KEYS='{"v2":"<new-secret>","v1":"<old-secret>"}'
    # JWT_ACTIVE_KID: the kid used when signing new tokens, e.g. "v2"
    # When EITHER field is unset the signer/verifier fall back to JWT_SECRET
    # and behaviour is identical to pre-rotation.  Set both to activate
    # multi-key mode; omit both to keep legacy single-key behaviour.
    JWT_KEYS: str = ""
    JWT_ACTIVE_KID: str = ""

    # Payout rates (per recipes-plan-v4-locked.md)
    PAYOUT_RATE_COOK: float = 0.50
    PAYOUT_RATE_OPERATOR: float = 0.60
    PAYOUT_RATE_STUDIO_PRIVATE: float = 0.70
    PAYOUT_RATE_RECIPE_BUNDLE: float = 0.70
    PAYOUT_RATE_FOUNDER_BONUS: float = 0.75  # first-50 publishers

    # VAT MOSS
    VAT_MOSS_ENABLED: bool = True
    VAT_EU_RATE: float = 0.23  # Poland standard rate (default, overridden by buyer country)

    # Creator program
    FOUNDER_PUBLISHER_LIMIT: int = 50

    # Skill publisher — tarball storage root
    RECIPES_SKILLS_DIR: str = "/var/lib/recipes-skills"

    # Phase D — heartbeat anonymity pepper (rotate cautiously: rotation
    # invalidates idempotency joins for the rotation day).
    HEARTBEAT_PEPPER: str = "wr-fleet-pepper-change-me"

    # Phase D — Discord bot. When DISCORD_BOT_TOKEN is empty the bot lifespan
    # is a no-op (server doesn't exist yet at deploy time).
    DISCORD_BOT_TOKEN: str = ""
    DISCORD_GUILD_ID: str = ""
    DISCORD_AUTHOR_THRESHOLD: float = 80.0

    # Issue #11 — explicit COOKIES_SECURE flag replaces HOST-heuristic.
    # Default True (production safe). False only valid when DATABASE_URL
    # contains "sqlite" (local dev). Validated below.
    COOKIES_SECURE: bool = True

    # Issue #12 — trusted reverse-proxy CIDRs for real-client-IP extraction.
    # Only honour CF-Connecting-IP / X-Forwarded-For when the direct TCP peer
    # (request.client.host) falls inside one of these ranges.
    # Snapshot from https://www.cloudflare.com/ips-v4 on 2026-05-19.
    TRUSTED_PROXY_CIDRS: list[str] = [
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
    ]

    model_config = {"env_file": ".env", "env_prefix": "WR_", "extra": "ignore"}

    @model_validator(mode="after")
    def _run_production_checks(self) -> "Settings":
        """Run all production-safety checks after all fields are resolved."""
        # Issue #11 — COOKIES_SECURE=False only valid in sqlite (dev) env
        if not self.COOKIES_SECURE and "sqlite" not in self.DATABASE_URL:
            raise RuntimeError(
                "COOKIES_SECURE=False is only allowed when DATABASE_URL contains 'sqlite' "
                "(local dev). Set WR_COOKIES_SECURE=true in production."
            )
        # Issues #1 + #4 — secrets gate
        _assert_production_secrets(self)
        return self


settings = Settings()
