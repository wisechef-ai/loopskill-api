# Sprint 4 D3 Report — Typed Telemetry Substrate

**Owner:** D3 subagent  
**Date:** 2026-04-28  
**Branch:** `agent/tori/recipes-api-sprint4-carousel-telemetry`

---

## Deliverables Checklist

- [x] `app/models.py` — TelemetryEvent model extended with typed columns
- [x] `app/schemas.py` — TelemetryIn Pydantic schema with typed fields + validators; TelemetryEventOut response schema
- [x] `app/routes.py` — POST /api/telemetry handler extended: typed mode, legacy mode, skill_slug→skill_id resolution
- [x] `tests/conftest.py` — Shared SQLite fixtures (engine_fixture, db_session, client, make_skill helper)
- [x] `tests/test_telemetry_typed.py` — 8 tests covering typed mode
- [x] `tests/test_telemetry_legacy.py` — 6 tests covering legacy payload mode
- [x] `tests/test_telemetry_validation.py` — 16 tests covering all validation rejection rules

---

## Files Touched

| File | Change |
|------|--------|
| `app/models.py` | Added 7 typed columns to `TelemetryEvent`: `skill_id` (FK→skills), `goal_class`, `duration_seconds`, `retry_count`, `user_intervention`, `agent_class_hash`, `install_event_id` |
| `app/schemas.py` | Extended `TelemetryIn` with typed optional fields + `@field_validator` for event_type, duration_seconds, retry_count, agent_class_hash. Added `TelemetryEventOut` response schema |
| `app/routes.py` | Extended `post_telemetry` to: resolve skill_slug→skill_id (404 if not found), populate typed columns, keep legacy payload behaviour, return `{status, event_id}` 201 response |
| `tests/conftest.py` | Created: session-scoped SQLite engine, per-test transactional db_session, TestClient fixture, `make_skill` helper |
| `tests/test_telemetry_typed.py` | Created: 8 typed-mode tests |
| `tests/test_telemetry_legacy.py` | Created: 6 legacy-mode tests |
| `tests/test_telemetry_validation.py` | Created: 16 validation tests |

---

## Tests Added

**Total: 30 tests** across 3 files.

### test_telemetry_typed.py (8 tests)
1. `test_full_typed_payload_201` — Full typed payload → HTTP 201 + UUID event_id
2. `test_typed_fields_stored_in_db` — All typed columns populated; payload column NULL
3. `test_skill_slug_resolved_to_skill_id` — slug→id resolved; both stored
4. `test_user_intervention_false_stored` — `False` stored as False (not NULL)
5. `test_duration_seconds_zero_stored` — boundary value 0 accepted and stored
6. `test_typed_and_legacy_combined` — typed + legacy payload simultaneously
7. `test_all_typed_fields_optional_absent_stored_null` — absent typed fields → NULL
8. `test_event_type_stored` — event_type column value correct

### test_telemetry_legacy.py (6 tests)
1. `test_legacy_payload_stored_as_json` — payload dict → JSON in payload column
2. `test_legacy_typed_columns_null` — typed columns NULL in legacy mode
3. `test_anonymous_telemetry_no_skill_slug` — no skill_slug accepted (skill_id=NULL)
4. `test_no_payload_no_typed_fields` — bare minimum request accepted
5. `test_all_valid_event_types_accepted` — all 5 event types return 201
6. `test_legacy_response_shape` — response has {status, event_id}

### test_telemetry_validation.py (16 tests)
1. `test_bad_event_type_rejected_422`
2. `test_event_type_empty_string_rejected`
3. `test_duration_over_86400_rejected_422`
4. `test_duration_negative_rejected_422`
5. `test_duration_86400_accepted` (boundary: valid)
6. `test_agent_hash_too_short_rejected`
7. `test_agent_hash_uppercase_rejected`
8. `test_agent_hash_non_hex_rejected`
9. `test_agent_hash_min_length_accepted` (boundary: 8 chars valid)
10. `test_agent_hash_max_length_accepted` (boundary: 64 chars valid)
11. `test_agent_hash_over_max_rejected` (65 chars)
12. `test_unknown_skill_slug_returns_404` — detail: "unknown skill_slug"
13. `test_known_skill_slug_accepted`
14. `test_retry_count_negative_rejected`
15. `test_retry_count_zero_accepted`
16. `test_unknown_goal_class_accepted_stored_as_is` — open enum, no rejection

---

## Test Pass Output

```
============================= test session starts ==============================
platform linux -- Python 3.11.14, pytest-9.0.3

collected 30 items

tests/test_telemetry_typed.py::TestTypedTelemetry::test_full_typed_payload_201 PASSED
tests/test_telemetry_typed.py::TestTypedTelemetry::test_typed_fields_stored_in_db PASSED
tests/test_telemetry_typed.py::TestTypedTelemetry::test_skill_slug_resolved_to_skill_id PASSED
tests/test_telemetry_typed.py::TestTypedTelemetry::test_user_intervention_false_stored PASSED
tests/test_telemetry_typed.py::TestTypedTelemetry::test_duration_seconds_zero_stored PASSED
tests/test_telemetry_typed.py::TestTypedTelemetry::test_typed_and_legacy_combined PASSED
tests/test_telemetry_typed.py::TestTypedTelemetry::test_all_typed_fields_optional_absent_stored_null PASSED
tests/test_telemetry_typed.py::TestTypedTelemetry::test_event_type_stored PASSED
tests/test_telemetry_legacy.py::TestLegacyTelemetry::test_legacy_payload_stored_as_json PASSED
tests/test_telemetry_legacy.py::TestLegacyTelemetry::test_legacy_typed_columns_null PASSED
tests/test_telemetry_legacy.py::TestLegacyTelemetry::test_anonymous_telemetry_no_skill_slug PASSED
tests/test_telemetry_legacy.py::TestLegacyTelemetry::test_no_payload_no_typed_fields PASSED
tests/test_telemetry_legacy.py::TestLegacyTelemetry::test_all_valid_event_types_accepted PASSED
tests/test_telemetry_legacy.py::TestLegacyTelemetry::test_legacy_response_shape PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_bad_event_type_rejected_422 PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_event_type_empty_string_rejected PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_duration_over_86400_rejected_422 PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_duration_negative_rejected_422 PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_duration_86400_accepted PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_agent_hash_too_short_rejected PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_agent_hash_uppercase_rejected PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_agent_hash_non_hex_rejected PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_agent_hash_min_length_accepted PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_agent_hash_max_length_accepted PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_agent_hash_over_max_rejected PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_unknown_skill_slug_returns_404 PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_known_skill_slug_accepted PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_retry_count_negative_rejected PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_retry_count_zero_accepted PASSED
tests/test_telemetry_validation.py::TestTelemetryValidation::test_unknown_goal_class_accepted_stored_as_is PASSED

============================== 30 passed in 2.13s ==============================
```

---

## Known Issues / TODOs

1. **Pre-existing `pytest -x -k telemetry` blocked by `test_auth.py`**: `ModuleNotFoundError: No module named 'jwt'` in the existing `test_auth.py` causes collection to abort when running from the root with `-k telemetry`. The three D3 test files pass fully when invoked directly (`pytest -x tests/test_telemetry_*.py`). Root cause: `pyjwt` not installed in `.venv`. Fix: `pip install pyjwt` — out of scope for D3.

2. **Concurrent subagent edit to models.py**: The D2 subagent added carousel scoring columns to `models.py` while D3 was also editing it. This created duplicate TelemetryEvent columns that were cleaned up before commit. Final state has a single clean definition per column.

3. **D1 Alembic migration**: The typed columns are present in the SQLAlchemy model (so `Base.metadata.create_all` picks them up for tests) but the production Alembic migration is D1's responsibility. D3 unblocks itself via the model definition per the contract's "unblock yourself" note.

4. **`install_event_id` FK**: Column added to model as per contract. No tests for this FK path as no install_events rows are typically present in pure telemetry tests. Integration tested via D1's migration tests.
