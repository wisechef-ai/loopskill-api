"""agentreg_0819 — the downgrade must take the CREDENTIALS with it.

Review round 2, F2 (BLOCKER). The shipped ``downgrade()`` dropped
``agent_identities`` and ``agent_registration_nonces`` and stopped there. But a
``rec_agent_`` key is not stored in either of those tables — it is an ordinary
``api_keys`` row hanging off an ordinary (shadow) ``users`` row. The ONLY things
that mark it as an agent credential are the ``agent_identities`` join the
middleware consults on every request and the ``users.is_agent`` column.

So dropping those tables did not disable the keys. It PROMOTED them:

  1. agent registers, gets ``rec_agent_…``, backed by shadow user U;
  2. operator rolls the migration back for an unrelated reason;
  3. ``agent_identities`` is gone, so ``_agent_identity.agent_key_is_blocked``
     no longer exists to be consulted and the revocation gate is gone with it;
  4. the key still hashes to a live ``api_keys`` row owned by U, so it keeps
     validating — now as an indistinguishable ordinary user key;
  5. there is no ``agent_identities`` row left to point an admin at, and
     ``POST /api/admin/agent-identities/{id}/revoke`` no longer exists.

A rollback would have silently converted every enrolled agent into an
unrevocable user account. That is the opposite of what a rollback is for.

These tests run the REAL migration against a real (SQLite) database — upgrade,
plant an enrolled agent exactly as ``register_agent`` would, downgrade, and
assert the credential is gone.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).parent.parent.parent

REVISION = "agentreg0819_agent_identities"
PREVIOUS = "bundles0811_p1_slug_backfill"


def _alembic_cfg(db_url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _table_names(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        return set(sa.inspect(conn).get_table_names())


def _plant_an_enrolled_agent(engine: sa.Engine) -> tuple[str, str]:
    """Insert the exact row trio ``register_agent`` produces. Returns (user_id, key_id).

    Written as raw SQL against the migrated schema rather than through the ORM
    on purpose: this test is about what the MIGRATION leaves behind, so it must
    not depend on the models happening to agree with it.
    """
    user_id = str(uuid.uuid4())
    identity_id = str(uuid.uuid4())
    key_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, display_name, is_agent) "
                "VALUES (:id, :name, :is_agent)"
            ),
            {"id": user_id, "name": "agent:rollback-probe", "is_agent": True},
        )
        conn.execute(
            sa.text(
                "INSERT INTO agent_identities "
                "(id, pubkey, pubkey_sha256, agent_name, user_id, revoked) "
                "VALUES (:id, :pubkey, :fp, :name, :user_id, :revoked)"
            ),
            {
                "id": identity_id,
                "pubkey": "A" * 43 + "=",
                "fp": "f" * 64,
                "name": "rollback-probe",
                "user_id": user_id,
                "revoked": False,
            },
        )
        conn.execute(
            sa.text(
                "INSERT INTO api_keys (id, user_id, key_prefix, key_hash, name, is_active) "
                "VALUES (:id, :user_id, :prefix, :hash, :name, :active)"
            ),
            {
                "id": key_id,
                "user_id": user_id,
                "prefix": "rec_agent_ab",
                "hash": "a" * 64,
                "name": "agent:rollback-probe",
                "active": True,
            },
        )
    return user_id, key_id


class TestDowngradeTakesTheCredentialsWithIt:
    def test_upgrade_creates_the_round_two_schema(self, tmp_path: Path, monkeypatch) -> None:
        """Control: without this, every assertion below could pass vacuously."""
        db_url = f"sqlite:///{tmp_path / 'agentreg.db'}"
        engine = sa.create_engine(db_url)
        monkeypatch.setenv("WR_DATABASE_URL", db_url)
        command.upgrade(_alembic_cfg(db_url), REVISION)

        tables = _table_names(engine)
        for expected in (
            "agent_identities",
            "agent_registration_nonces",
            "agent_registration_quota",
        ):
            assert expected in tables, f"{expected} missing after upgrade"

        with engine.connect() as conn:
            user_cols = {c["name"] for c in sa.inspect(conn).get_columns("users")}
        assert "is_agent" in user_cols, "the durable agent marker (F5) must be added by the migration"
        engine.dispose()

    def test_downgrade_deletes_the_agent_api_keys(self, tmp_path: Path, monkeypatch) -> None:
        """THE finding. After rollback the key must not resolve to anything."""
        db_url = f"sqlite:///{tmp_path / 'agentreg.db'}"
        engine = sa.create_engine(db_url)
        monkeypatch.setenv("WR_DATABASE_URL", db_url)
        cfg = _alembic_cfg(db_url)

        command.upgrade(cfg, REVISION)
        user_id, key_id = _plant_an_enrolled_agent(engine)

        # Control: before the downgrade the key is live. If it were not, the
        # post-downgrade assertion would be meaningless.
        with engine.connect() as conn:
            live = conn.execute(
                sa.text("SELECT is_active FROM api_keys WHERE id = :id"), {"id": key_id}
            ).fetchone()
        assert live is not None and live[0], "control: the planted agent key must start out live"

        command.downgrade(cfg, PREVIOUS)

        with engine.connect() as conn:
            surviving_key = conn.execute(
                sa.text("SELECT id FROM api_keys WHERE id = :id"), {"id": key_id}
            ).fetchone()
            surviving_user = conn.execute(
                sa.text("SELECT id FROM users WHERE id = :id"), {"id": user_id}
            ).fetchone()

        assert surviving_key is None, (
            "the agent's API key survived the rollback — with agent_identities dropped "
            "it now validates as an ordinary rec_ user key with no revocation path"
        )
        assert surviving_user is None, "the shadow user survived the rollback"
        engine.dispose()

    def test_downgrade_drops_the_feature_tables_and_the_marker_column(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        db_url = f"sqlite:///{tmp_path / 'agentreg.db'}"
        engine = sa.create_engine(db_url)
        monkeypatch.setenv("WR_DATABASE_URL", db_url)
        cfg = _alembic_cfg(db_url)

        command.upgrade(cfg, REVISION)
        _plant_an_enrolled_agent(engine)
        command.downgrade(cfg, PREVIOUS)

        tables = _table_names(engine)
        for gone in (
            "agent_identities",
            "agent_registration_nonces",
            "agent_registration_quota",
        ):
            assert gone not in tables, f"{gone} survived the downgrade"

        with engine.connect() as conn:
            user_cols = {c["name"] for c in sa.inspect(conn).get_columns("users")}
        assert "is_agent" not in user_cols
        engine.dispose()

    def test_a_human_users_keys_are_untouched_by_the_downgrade(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The blast radius is exactly the agents — nobody else loses a credential."""
        db_url = f"sqlite:///{tmp_path / 'agentreg.db'}"
        engine = sa.create_engine(db_url)
        monkeypatch.setenv("WR_DATABASE_URL", db_url)
        cfg = _alembic_cfg(db_url)

        command.upgrade(cfg, REVISION)
        _plant_an_enrolled_agent(engine)

        human_id = str(uuid.uuid4())
        human_key_id = str(uuid.uuid4())
        with engine.begin() as conn:
            conn.execute(
                sa.text("INSERT INTO users (id, display_name, is_agent) VALUES (:id, :n, :a)"),
                {"id": human_id, "n": "a real person", "a": False},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO api_keys (id, user_id, key_prefix, key_hash, name, is_active) "
                    "VALUES (:id, :u, :p, :h, :n, :act)"
                ),
                {
                    "id": human_key_id,
                    "u": human_id,
                    "p": "rec_live_zz",
                    "h": "b" * 64,
                    "n": "human",
                    "act": True,
                },
            )

        command.downgrade(cfg, PREVIOUS)

        with engine.connect() as conn:
            assert conn.execute(
                sa.text("SELECT id FROM users WHERE id = :id"), {"id": human_id}
            ).fetchone() is not None, "a human user was deleted by the rollback"
            assert conn.execute(
                sa.text("SELECT id FROM api_keys WHERE id = :id"), {"id": human_key_id}
            ).fetchone() is not None, "a human API key was deleted by the rollback"
        engine.dispose()

    def test_downgrade_is_a_no_op_when_no_agent_ever_registered(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The common case: rolling back a feature nobody used must not error."""
        db_url = f"sqlite:///{tmp_path / 'agentreg.db'}"
        engine = sa.create_engine(db_url)
        monkeypatch.setenv("WR_DATABASE_URL", db_url)
        cfg = _alembic_cfg(db_url)

        command.upgrade(cfg, REVISION)
        command.downgrade(cfg, PREVIOUS)
        assert "agent_identities" not in _table_names(engine)
        engine.dispose()

    def test_upgrade_downgrade_upgrade_round_trips(self, tmp_path: Path, monkeypatch) -> None:
        """A downgrade that cannot be re-applied is a one-way door, not a rollback."""
        db_url = f"sqlite:///{tmp_path / 'agentreg.db'}"
        engine = sa.create_engine(db_url)
        monkeypatch.setenv("WR_DATABASE_URL", db_url)
        cfg = _alembic_cfg(db_url)

        command.upgrade(cfg, REVISION)
        _plant_an_enrolled_agent(engine)
        command.downgrade(cfg, PREVIOUS)
        command.upgrade(cfg, REVISION)

        tables = _table_names(engine)
        assert {"agent_identities", "agent_registration_quota"} <= tables
        engine.dispose()
