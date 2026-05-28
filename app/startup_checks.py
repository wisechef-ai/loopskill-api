"""Boot-time startup checks for WiseRecipes API.

These checks run during the FastAPI lifespan startup phase and are intentionally
fail-soft — any exception is caught so the service always starts.

Phase 4: Stripe webhook endpoint smoke test.
"""

from __future__ import annotations

import logging
import os

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Phase 4: boot-time Stripe webhook endpoint smoke test ─────────────────────

# The expected production URL for our Stripe webhook endpoint.
EXPECTED_WEBHOOK_URL = "https://recipes.wisechef.ai/api/stripe/webhook"

# Env var holding the Discord #tori webhook URL for ops alerts.
_TORI_DISCORD_WEBHOOK_URL_VAR = "TORI_DISCORD_WEBHOOK_URL"

# Stripe SDK call timeout (seconds) for the boot smoke test.
WEBHOOK_CHECK_TIMEOUT_S = 5


def post_tori_alert(message: str) -> None:
    """Post a plain-text alert to the #tori Discord webhook.

    Fire-and-forget, synchronous (called only at startup). Never raises.
    """
    url = os.environ.get(_TORI_DISCORD_WEBHOOK_URL_VAR, "").strip()
    if not url:
        logger.debug("post_tori_alert: %s not set, skipping", _TORI_DISCORD_WEBHOOK_URL_VAR)
        return
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(url, json={"content": message})
            if resp.status_code >= 400:
                logger.warning(
                    "tori alert webhook returned %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
    # Rationale: alert delivery is best-effort; any HTTP/network error must not crash boot
    except Exception:  # noqa: BLE001
        logger.exception("tori alert delivery failed")


async def verify_stripe_webhook_endpoint() -> None:
    """Boot-time smoke test: assert the Stripe webhook endpoint is registered correctly.

    Checks:
    1. Exactly one endpoint at ``EXPECTED_WEBHOOK_URL`` with status == 'enabled'.
    2. ``WR_STRIPE_WEBHOOK_SECRET`` env var starts with ``whsec_``.

    On any mismatch: logs CRITICAL and posts a Discord #tori alert.
    Never raises — fail-soft so the service always starts.

    Skips entirely when running under tests (sqlite DB).
    """
    # Skip in test environments.
    db_url = settings.DATABASE_URL or ""
    if "sqlite" in db_url.lower():
        logger.debug("verify_stripe_webhook_endpoint: skipped (sqlite DB → test env)")
        return

    try:
        import stripe

        stripe.api_key = settings.STRIPE_SECRET_KEY
        stripe.api_version = "2026-01-28.clover"

        # ── 1. Check WR_STRIPE_WEBHOOK_SECRET format ──────────────────────────
        webhook_secret = os.environ.get("WR_STRIPE_WEBHOOK_SECRET", "")
        if not webhook_secret.startswith("whsec_"):
            msg = (
                "🚨 **CRITICAL** `WR_STRIPE_WEBHOOK_SECRET` does not start with `whsec_` "
                f"on `{EXPECTED_WEBHOOK_URL}` — webhooks may fail to verify. "
                "Check .env and rotate the secret if needed."
            )
            logger.critical("verify_stripe_webhook_endpoint: WR_STRIPE_WEBHOOK_SECRET invalid format")
            post_tori_alert(msg)

        # ── 2. Check registered endpoints ─────────────────────────────────────
        # NOTE: stripe SDK rejects `timeout=` as a per-call kwarg on resource
        # list methods (InvalidRequestError: Received unknown parameter: timeout).
        # The default SDK timeout (~80s) is fine here; we're inside a try/except
        # and this only runs once per boot.
        endpoints = stripe.WebhookEndpoint.list(limit=20)
        # stripe ListObject: `.data` is the attribute, `.get("data")` raises
        # AttributeError because ListObject inherits from StripeObject and
        # "data"/`get` aren't stored keys. Defensive access pattern:
        endpoint_data = getattr(endpoints, "data", None)
        if endpoint_data is None:
            try:
                endpoint_data = endpoints["data"]  # type: ignore[index]
            except (KeyError, TypeError):
                endpoint_data = []

        def _field(ep, name):
            """Read a field whether ep is a stripe object or a dict."""
            v = getattr(ep, name, None)
            if v is None and isinstance(ep, dict):
                v = ep.get(name)
            return v

        matching = [
            ep
            for ep in (endpoint_data or [])
            if _field(ep, "url") == EXPECTED_WEBHOOK_URL and _field(ep, "status") == "enabled"
        ]
        count = len(matching)
        if count == 1:
            logger.info(
                "verify_stripe_webhook_endpoint: OK — exactly one enabled endpoint at %s",
                EXPECTED_WEBHOOK_URL,
            )
        elif count == 0:
            msg = (
                f"🚨 **CRITICAL** No enabled Stripe webhook endpoint found at "
                f"`{EXPECTED_WEBHOOK_URL}`. Payments will NOT be processed. "
                "Re-register the endpoint in the Stripe dashboard immediately."
            )
            logger.critical(
                "verify_stripe_webhook_endpoint: zero enabled endpoints at %s",
                EXPECTED_WEBHOOK_URL,
            )
            post_tori_alert(msg)
        else:
            msg = (
                f"🚨 **CRITICAL** {count} enabled Stripe webhook endpoints found at "
                f"`{EXPECTED_WEBHOOK_URL}` (expected exactly 1). "
                "Duplicate endpoints may cause double-processing. Audit the Stripe dashboard."
            )
            logger.critical(
                "verify_stripe_webhook_endpoint: %d endpoints at %s (expected 1)",
                count,
                EXPECTED_WEBHOOK_URL,
            )
            post_tori_alert(msg)

    # Rationale: Stripe API check is fail-soft; any SDK/network error logs warning and continues
    except Exception:  # noqa: BLE001
        logger.warning(
            "verify_stripe_webhook_endpoint: check failed (non-fatal) — service will continue",
            exc_info=True,
        )


# ── Issue #21: alembic heads alignment check ──────────────────────────────────


def check_alembic_heads(database_url: str | None = None) -> None:
    """Raise RuntimeError if the database is not at alembic head in non-sqlite envs.

    Runs ``alembic check`` via the Python API (no subprocess) to confirm the
    target database has all migrations applied.  In sqlite environments (local
    dev / CI) the check is skipped entirely so the test suite boots instantly
    without needing a real database.

    Called from ``create_app()`` (synchronous context, before any request is
    served) so the process refuses to start rather than silently running
    against a schema that doesn't match the codebase.
    """
    db_url = database_url or settings.DATABASE_URL
    if "sqlite" in db_url:
        logger.debug("check_alembic_heads: skipped (sqlite env)")
        return

    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
        from sqlalchemy import create_engine

        alembic_cfg = Config()
        alembic_cfg.set_main_option("script_location", "alembic")
        alembic_cfg.set_main_option("sqlalchemy.url", db_url)

        script = ScriptDirectory.from_config(alembic_cfg)
        head_revisions = {s.revision for s in script.get_revisions("heads")}

        engine = create_engine(db_url)
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            current_heads = set(ctx.get_current_heads())

        if current_heads != head_revisions:
            missing = head_revisions - current_heads
            raise RuntimeError(
                f"Database is NOT at alembic head. "
                f"Current: {current_heads or '{}'}, "
                f"Head: {head_revisions}. "
                f"Missing revisions: {missing}. "
                f"Run `alembic upgrade head` before starting the service."
            )
        logger.info(
            "check_alembic_heads: OK — database at head (%s)",
            head_revisions,
        )
    # Rationale: any DB connectivity error at boot is re-raised to prevent silent degraded start
    except RuntimeError:
        raise
    # Rationale: wrap unexpected DB/driver errors as RuntimeError to block start
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"check_alembic_heads: unable to verify migration state — {exc}") from exc
