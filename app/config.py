"""WiseRecipes API — configuration via env vars."""

from pydantic_settings import BaseSettings


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

    # Stripe Subscription price IDs (Cook / Operator / Studio tiers)
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
    OAUTH_REDIRECT_BASE: str = ""  # e.g. https://recipes.wisechef.ai 

    # JWT for creator auth
    JWT_SECRET: str = "wr-jwt-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_HOURS: int = 72

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

    model_config = {"env_file": ".env", "env_prefix": "WR_"}


settings = Settings()
