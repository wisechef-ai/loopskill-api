"""Recipes API — configuration via env vars."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # TODO: Rename DB from 'wiserecipes' to 'recipes' in a future migration (pg_dump + createdb + pg_restore)
    DATABASE_URL: str = "postgresql://wisechef@localhost/wiserecipes"
    API_KEY: str = "rec_dev_wiserecipes_local_testing_key"  # must start with rec_
    SIGNING_SECRET: str = "wr-tarball-signing-secret-change-me"
    RATE_LIMIT_PER_MINUTE: int = 60
    HOST: str = "0.0.0.0"
    PORT: int = 8200

    model_config = {"env_file": ".env", "env_prefix": "WR_"}


settings = Settings()
