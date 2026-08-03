"""Tests for scripts/purge_fake_creator_payout.py (hub D-018 #6, M0 deletion 3).

Adam confirmed prod's lone ``creator_payouts`` row is not real money. The
script is dry-run by default and refuses to act unless the table has
EXACTLY one row — the count guard is the load-bearing safety property here,
since this is a one-off script the orchestrator will point at prod after
this PR merges.
"""

from __future__ import annotations

from uuid import uuid4

from scripts.purge_fake_creator_payout import ledger_row, purge


def _make_user(db, email="creator@t.com"):
    from app.models import User

    u = User(id=uuid4(), email=email, display_name="Creator")
    db.add(u)
    db.flush()
    return u


def _make_payout(db, creator_id, **overrides):
    from app.models import CreatorPayout

    defaults = dict(
        id=uuid4(),
        creator_id=creator_id,
        installs_count=0,
        gross_revenue_cents=0,
        creator_share_cents=0,
        status="pending",
    )
    defaults.update(overrides)
    row = CreatorPayout(**defaults)
    db.add(row)
    db.flush()
    return row


class TestGuardRefusesWhenCountIsNotOne:
    def test_refuses_on_zero_rows(self, db_session):
        code = purge(db_session, execute=True)
        assert code == 1

    def test_refuses_on_two_rows(self, db_session):
        u = _make_user(db_session)
        _make_payout(db_session, u.id)
        _make_payout(db_session, u.id)
        db_session.commit()

        code = purge(db_session, execute=True)

        assert code == 1
        from app.models import CreatorPayout

        assert db_session.query(CreatorPayout).count() == 2  # untouched


class TestDryRunDoesNotDelete:
    def test_dry_run_leaves_row_in_place(self, db_session):
        u = _make_user(db_session)
        row = _make_payout(db_session, u.id)
        db_session.commit()

        code = purge(db_session, execute=False)

        assert code == 0
        from app.models import CreatorPayout

        assert db_session.query(CreatorPayout).count() == 1
        assert db_session.query(CreatorPayout).first().id == row.id


class TestExecuteDeletesTheSingleRow:
    def test_execute_deletes_the_one_row(self, db_session):
        u = _make_user(db_session)
        _make_payout(db_session, u.id)
        db_session.commit()

        code = purge(db_session, execute=True)

        assert code == 0
        from app.models import CreatorPayout

        assert db_session.query(CreatorPayout).count() == 0


class TestLedgerRowTemplate:
    def test_ledger_row_is_tab_separated_with_four_fields(self, db_session):
        u = _make_user(db_session)
        row = _make_payout(db_session, u.id)
        db_session.commit()

        row_dict = {
            "id": str(row.id),
            "creator_id": str(u.id),
        }
        line = ledger_row(row_dict, reason="test reason")
        fields = line.split("\t")
        assert len(fields) == 4
        assert str(row.id) in fields[1]
        assert fields[3] == "test reason"
