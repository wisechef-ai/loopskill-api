"""LoopSkill API — configuration via env vars."""

import os

from typing import Annotated

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

# Default (insecure) values that MUST be rotated in any non-sqlite environment.
_DEFAULT_API_KEY = "rec_dev_wiserecipes_local_testing_key"
_DEFAULT_SIGNING_SECRET = "wr-tarball-signing-secret-change-me"
_DEFAULT_JWT_SECRET = "wr-jwt-secret-change-me"
_DEFAULT_HEARTBEAT_PEPPER = "wr-fleet-pepper-change-me"


def _assert_production_secrets(cfg: "Settings") -> None:
    """Raise RuntimeError if any default change-me secret is present in a non-sqlite env.

    Called from Settings.__init__ via the _run_production_checks model
    validator so the process refuses to construct a production Settings
    with exploitable defaults (secfix_1905 contract — direct Settings()
    construction in prod mode raises), while the module-level ``settings``
    singleton stays LAZY (issue #283) so importing app modules never
    constructs it and thus never fires the gate at import time.

    Also enforces OAUTH_REDIRECT_BASE requirements in non-sqlite envs:
    - Must be non-empty
    - Must start with 'https://'

    SQLite envs (local dev) are exempt — default values are fine there.
    """
    if "sqlite" in cfg.DATABASE_URL:
        return  # dev environment — allow defaults

    insecure: list[str] = []
    if cfg.API_KEY == _DEFAULT_API_KEY:
        insecure.append("API_KEY")
    if cfg.SIGNING_SECRET == _DEFAULT_SIGNING_SECRET:
        insecure.append("SIGNING_SECRET")
    if cfg.JWT_SECRET == _DEFAULT_JWT_SECRET:
        insecure.append("JWT_SECRET")
    if cfg.HEARTBEAT_PEPPER == _DEFAULT_HEARTBEAT_PEPPER:
        insecure.append("HEARTBEAT_PEPPER")

    if insecure:
        raise RuntimeError(
            f"Refusing to boot in production with default change-me secret(s): "
            f"{', '.join(insecure)}. "
            f"Set proper values via environment variables (WR_{{NAME}})."
        )

    # chef_0823 (t_4a38fed9) — install-integrity fail-closed gate: the server
    # must know its own public IPv4 so CI self-installs (deploy runner on this
    # host, anonymous installs from its own address) can be also excluded from
    # public install counts. Without it, /api/stats counts every deploy
    # verification as an organic install (118 of 432 in the 2026-08-23
    # analysis window). Refuse to boot rather than publish inflated numbers.
    if not cfg.SERVER_PUBLIC_IP:
        raise RuntimeError(
            "Refusing to boot in production: SERVER_PUBLIC_IP is empty. "
            "Set WR_SERVER_PUBLIC_IP to this host's public IPv4 so CI "
            "self-installs are excluded from public install stats."
        )

    # Issue #4 — OAUTH_REDIRECT_BASE must be non-empty and https:// in prod
    base = cfg.OAUTH_REDIRECT_BASE
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
        ("STRIPE_PRICE_PRO", cfg.STRIPE_PRICE_PRO, "STRIPE_PRICE_COOK", cfg.STRIPE_PRICE_COOK),
        (
            "STRIPE_PRICE_PRO_PLUS",
            cfg.STRIPE_PRICE_PRO_PLUS,
            "STRIPE_PRICE_OPERATOR",
            cfg.STRIPE_PRICE_OPERATOR,
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
    # TODO(rename): env var still uses legacy name for prod compatibility.
    #   WR_* prefix (env_prefix below) and RECIPES_API_KEY are LIVE in production.
    #   Renaming requires a coordinated cutover (see issue #63 proposal).
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
    #   WR_STRIPE_PRICE_PRO        ($9.95/mo)
    #   WR_STRIPE_PRICE_PRO_PLUS   ($100/mo)
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

    # GitHub OAuth
    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # OAuth redirect base URL (used to build callback URLs)
    OAUTH_REDIRECT_BASE: str = ""  # e.g. https://loopskill.io

    # Public origin used to build install / download URLs handed to agents.
    # Empty by default so config.public_origin() can apply the env-fallback
    # chain (LOOPSKILL_PUBLIC_ORIGIN -> RECIPES_PUBLIC_ORIGIN compat -> brand
    # default). Set WR_PUBLIC_ORIGIN to pin it explicitly for a self-host.
    PUBLIC_ORIGIN: str = ""

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
    # TODO(rename): env var still uses legacy name for prod compatibility.
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

    # mesh_0408 T0-D — SEPARATE Ed25519 mesh credential signing ring (spec §0).
    # This is NOT the session-JWT HMAC ring above (JWT_SECRET/JWT_KEYS) and
    # must never be merged with it — see app/mesh/keys.py module docstring.
    #
    # MESH_SIGNING_KEY_PATH: filesystem path to a PEM-encoded Ed25519 private
    # key, mode 0600, OUTSIDE the repo tree. No default — an unset path means
    # mesh minting is disabled (fails closed), which is correct for any
    # deployment that hasn't provisioned a key via scripts/mesh_keygen.py.
    MESH_SIGNING_KEY_PATH: str = ""
    # MESH_SIGNING_KID: the `kid` new tokens are signed with. Must match a
    # key present in MESH_JWKS_DIR (see keys.py) so verifiers can resolve it.
    MESH_SIGNING_KID: str = ""
    # MESH_JWKS_DIR: directory holding PUBLIC key material for every kid in
    # the ring (active + retired-but-not-yet-expired), used to build the
    # published JWKS. Each file is `<kid>.pub.pem`. Separate from the private
    # key path so the JWKS-serving code path never touches private material.
    MESH_JWKS_DIR: str = ""

    # spotify2607fix_2 — per-source deadline (seconds) for the LEGACY
    # /api/skills/external route's live (non-empty-query) federated fan-out.
    # Sources are queried CONCURRENTLY (app/services/metasearch_fanout.fan_out);
    # this deadline bounds each source individually, so the whole-gather
    # wall-clock is bounded by the slowest source, not the sum across sources.
    # A source exceeding this is abandoned and reported in `degraded_sources` —
    # partial results, never a hung/500 response. Configurable via
    # WR_EXTERNAL_FANOUT_PER_SOURCE_DEADLINE_S; default mirrors the metasearch
    # route's tuned budget (see metasearch_fanout._PER_SOURCE_DEADLINE_S notes).
    EXTERNAL_FANOUT_PER_SOURCE_DEADLINE_S: float = 2.5

    # agentreg_0819 — self-serve agent registration (POST /api/agents/register).
    # These are the ONLY abuse wall in front of an endpoint that mints a real
    # API key with no human in the loop, so they are deliberately conservative
    # and env-tunable (WR_AGENT_REGISTRATION_*) without a redeploy.
    #
    # Per-IP cap: a single source may enrol this many agents in a rolling 24h.
    AGENT_REGISTRATION_PER_IP_PER_DAY: int = 3
    # Global cap: total enrolments across ALL sources in a rolling 24h. Global
    # backstop against a distributed key-stuffing farm that defeats the per-IP
    # cap by spreading across addresses.
    AGENT_REGISTRATION_GLOBAL_PER_DAY: int = 20
    # Maximum accepted clock skew (seconds) between the signed `timestamp` and
    # server time. Also sets the nonce retention horizon (2x this), after which
    # a replay is already refused by the timestamp gate.
    AGENT_REGISTRATION_MAX_SKEW_SECONDS: int = 300

    # chef_0823 (t_4a38fed9) — install-integrity internal-IP set. The deploy
    # pipeline's self-hosted runner executes ON this host, so CI install
    # traffic arrives with client_ip == the server's own public IPv4 and no
    # API key — indistinguishable from organic anonymous installs unless the
    # server knows its own address. Boot-gated in production (fail-closed,
    # pitfall #24 posture): a non-sqlite deployment without WR_SERVER_PUBLIC_IP
    # refuses to boot rather than silently counting its own CI traffic as
    # organic marketplace installs.
    SERVER_PUBLIC_IP: str = ""
    # Additional known-internal (dogfood/office/VPN) source IPs to exclude
    # from public install counts at read time. Optional — self-hosters with
    # no internal dogfood leave it empty.
    KNOWN_INTERNAL_IPS: Annotated[list[str], NoDecode] = []

    @field_validator("KNOWN_INTERNAL_IPS", mode="before")
    @classmethod
    def _parse_internal_ips(cls, value: object) -> object:
        """Accept plain comma-separated strings from env (pydantic-settings
        would otherwise JSON-decode list fields, so ``WR_KNOWN_INTERNAL_IPS=
        195.128.172.227`` crashes the app at boot with SettingsError).

        Accepted forms: "", "a,b ,c", JSON arrays ('["a","b"]'). Normalized
        to a stripped, deduped list of non-empty strings.
        """
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            if value.startswith("["):
                import json

                value = json.loads(value)
            else:
                value = value.split(",")
        if isinstance(value, (list, tuple)):
            items = [str(item).strip() for item in value]
            items = [item for item in items if item]
            return list(dict.fromkeys(items))  # dedupe, preserve order
        return value

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


def get_settings() -> "Settings":
    """Construct the Settings singleton on FIRST access (issue #283).

    The module-level ``settings`` name is served via PEP 562 module
    ``__getattr__`` below, so ``import app.config`` (or importing any app
    module) no longer constructs Settings at import time — the production
    gate in Settings.__init__ fires only when something actually touches
    ``settings`` / calls ``get_settings()``. Bare library imports, ad-hoc
    scripts, and pure-function verification (the issue #283 acceptance
    case) stay side-effect free; constructing/caching happens on first
    attribute access instead.
    """
    return _get_settings_cached()


@lru_cache(maxsize=1)
def _get_settings_cached() -> "Settings":
    return Settings()


def __getattr__(name: str):
    """PEP 562 — lazy module-level ``settings`` singleton (issue #283)."""
    if name == "settings":
        return _get_settings_cached()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def run_production_boot_checks(config: "Settings | None" = None) -> "Settings":
    """Run all production-safety checks at SERVE time (issue #283).

    Belt-and-braces only: the gate itself still lives in
    Settings.__init__ (_run_production_checks model validator — the
    secfix_1905 contract that direct Settings() construction in prod mode
    raises). This wrapper exists so the FastAPI lifespan hook can force
    construction + validation at startup, guaranteeing the process refuses
    to SERVE misconfigured even if nothing touched ``settings`` earlier.
    """
    cfg = config if config is not None else get_settings()

    # Issue #11 — COOKIES_SECURE=False only valid in sqlite (dev) env
    if not cfg.COOKIES_SECURE and "sqlite" not in cfg.DATABASE_URL:
        raise RuntimeError(
            "COOKIES_SECURE=False is only allowed when DATABASE_URL contains 'sqlite' "
            "(local dev). Set WR_COOKIES_SECURE=true in production."
        )
    # Issues #1 + #4 — secrets gate
    _assert_production_secrets(cfg)
    return cfg


# Brand default for install / download URLs when nothing is configured.
LOOPSKILL_DEFAULT_ORIGIN = "https://loopskill.io"


def public_origin() -> str:
    """Resolve the public origin used to build install / download URLs.

    Single seam every URL builder must route through. Priority:
      1. WR_PUBLIC_ORIGIN  (Settings.PUBLIC_ORIGIN — pydantic env_prefix=WR_)
      2. LOOPSKILL_PUBLIC_ORIGIN  (primary standalone env var)
      3. RECIPES_PUBLIC_ORIGIN    (backward-compat with pre-rename deploys)
      4. https://loopskill.io     (brand default — never the old domain)

    Trailing slashes are stripped so callers can append paths directly.
    """
    origin = (
        (get_settings().PUBLIC_ORIGIN or "").strip()
        or os.environ.get("LOOPSKILL_PUBLIC_ORIGIN", "").strip()
        or os.environ.get("RECIPES_PUBLIC_ORIGIN", "").strip()
        or LOOPSKILL_DEFAULT_ORIGIN
    )
    return origin.rstrip("/")
