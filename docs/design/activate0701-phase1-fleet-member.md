# Phase 1 — FLEET-UNIT + AGENT KEYS — design contract (loopskill_activate_0701)

Author: Tori (opus-role design). Implementer: subagent (TDD RED->GREEN). Reviewer: codex gpt-5.5.
Branch: loopskill_activate_0701/phase1 (worktree /home/adam/repos/wt-activate0701-phase1).

## Product locks this phase implements
- D2 / lock #13: the per-agent API key IS the member identity. One key = one agent = one FleetMember.
- ReconcileEvent (and all future reports, Phase T) must carry member identity.

## 1. Model: FleetMember (app/models.py, near Fleet)

```python
class FleetMember(Base):
    """One enrolled agent in a fleet — identified by its dedicated API key.

    lock #13 (activate_0701): the agent API key is the billable + identity
    primitive. api_key_id is UNIQUE — a key can identify at most one member.
    (fleet_id, host, profile) is UNIQUE — one member per agent profile per host.
    """
    __tablename__ = "fleet_members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    fleet_id = Column(UUID(as_uuid=True), ForeignKey("fleets.id", ondelete="CASCADE"), nullable=False, index=True)
    host = Column(String(255), nullable=False)          # e.g. "adam-xps"
    profile = Column(String(100), nullable=False, default="default", server_default="default")
    skills_dir = Column(Text, nullable=False)           # e.g. "~/.hermes/loopskill"
    api_key_id = Column(UUID(as_uuid=True), ForeignKey("api_keys.id"), nullable=False, unique=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("fleet_id", "host", "profile", name="uq_fleet_members_fleet_host_profile"),)
```

## 2. ReconcileEvent gains member identity

Add to ReconcileEvent: `member_id = Column(UUID(as_uuid=True), nullable=True, index=True)` (no FK constraint —
events must survive member deletion for telemetry history; nullable for pre-Phase-1 rows and anonymous self-test).

`record_reconcile_event(...)` (app/services/promotion.py) gains `member_id: UUID | None = None` param, stamped through.

`promotion_routes.reconcile_report` resolves member BEFORE recording:
```python
member = resolve_member_for_key(db, api_key_id)   # None if key is not a member key
record_reconcile_event(..., member_id=member.id if member else None)
```

Helper `resolve_member_for_key(db, api_key_id) -> FleetMember | None` lives in a NEW service module
`app/services/fleet_members.py` (keeps route modules under the 600-line gate; helpers belong in services).

## 3. Routes: NEW module app/fleet_member_routes.py (mounted in main.py next to fleet_routes)

Auth: reuse `resolve_fleet_ctx` from fleet_routes; enrollment/removal are FLEET-OWNER actions →
scope must be `user` (fleet owner) or `master`. A `fleet`-scope ctx (rec_fleet_ key) may NOT mint member keys.
Non-owner → 404 (existence never leaks — parity with reconcile-contract §7).

### POST /api/fleets/{fleet_id}/members  (201)
Body: `{host: str (1-255), profile: str = "default", skills_dir: str}`
- Validates fleet exists + caller owns it (or master).
- 409 `member_exists` if (fleet_id, host, profile) already active.
- Mints a dedicated APIKey via api_key_routes._generate_key():
  - user_id = fleet.owner_user_id
  - label/name = `member:{host}/{profile}` (truncate to 100)
  - NOTE: member-key mint deliberately BYPASSES api_key_routes.KEY_CAP — tier key caps
    (free=1/pro=200) arrive in Phase TEN as the unified meter. Leave a `# activate_0701/TEN`
    comment marking the seam.
- Creates FleetMember bound to the new key.
Response (plaintext key shown ONCE):
```json
{"member_id": "...", "fleet_id": "...", "host": "...", "profile": "...", "skills_dir": "...",
 "api_key": "rec_live_...", "api_key_id": "...", "key_prefix": "rec_live_xxx",
 "warning": "Save this key now — it will not be shown again."}
```

### GET /api/fleets/{fleet_id}/members  (200) — KEYSET paginated (§B rule 3: no unbounded SELECTs)
Query: `limit` (default 50, max 200), `after` (member id cursor).
Order: (created_at, id) ascending. Response:
```json
{"members": [{"member_id","host","profile","skills_dir","key_prefix","is_active","created_at",
              "last_event_at"}], "next_after": "<id or null>"}
```
`last_event_at` = max(ReconcileEvent.created_at) for that member_id, fetched in ONE grouped query
for the page's ids (no N+1).

### DELETE /api/fleets/{fleet_id}/members/{member_id}  (200, idempotent)
Deactivates member (is_active=False) AND revokes its APIKey (is_active=False). Returns
`{"removed": true, "member_id": ...}`. Missing/already-inactive → same 200 (idempotent).

## 4. Migration (alembic, single head discipline)
One new revision, down_revision = "lsk0627_loop_feedback":
- create_table fleet_members (+ both unique constraints, + index on fleet_id)
- add_column reconcile_events.member_id (UUID, nullable) + index ix_reconcile_events_member_id
Postgres-only SQL is fine (prod is PG; tests run SQLite via ORM create_all — keep types portable:
use the same UUID TypeDecorator pattern the codebase already uses; inspect how other tables do it).

## 5. Tests (TDD — write RED first, in tests/test_activate0701_fleet_members.py)
1. enroll happy path: 201, plaintext key returned once, key row bound, member row correct.
2. THE PHASE GATE: enroll TWO members on ONE host (profiles "default" + "worker"); POST
   /api/bundles/{id}/reconcile-report authenticated with EACH member key → two ReconcileEvents
   with DISTINCT non-null member_id matching each member.
3. duplicate (fleet,host,profile) → 409.
4. keyset pagination: 3 members, limit=2 → 2 + next_after; follow cursor → 1 + next_after null.
5. authz: non-owner user → 404; fleet-scope (rec_fleet_) ctx → 403; anonymous → 401.
6. delete idempotency: delete twice → 200 both; key revoked (is_active False); member excluded
   from default list (is_active filter) but events remain queryable.
7. reconcile poll with a member key still works (member key is a normal APIKey — regression).
8. non-member key (plain user key) reconcile-report → member_id NULL (backward compat).

## 6. Discipline
- ruff format on touched files; no module crosses 600 lines; NEVER `git add -A` (graphify artifacts).
- Full suite must stay green (baseline: /tmp/baseline_suite_0702.log).
- Bump app/version.py 0.6.0 -> 0.7.0 (behavioral deploy; contract test rejects literals elsewhere).
- Commit style: `feat(fleet): FleetMember = per-agent-key identity (activate_0701 Phase 1)`.
