# Phase E — RESIDENCY GATE — design contract (loopskill_activate_0701)

Author: Tori. Implementer: subagent (independent — needs Phase B's Connector residency_tag + Phase A2's
composite Loop residency derivation to exist, but the GATE logic itself is standalone). Reviewer: codex.
Branch: loopskill_activate_0701/phaseE.

## Product
Fail-closed server-side residency enforcement. EU data residency: certain fleets (Astrovita = EU)
can only receive non-personal EU-resident artifacts; non-EU-tagged connectors (zai_websearch) are
BLOCKED for EU fleets. Praga (non-EU) is NOT onboarded this sprint — the gate ships ahead of it.

## Residency model
- Artifact residency tags: Connector.residency_tag (Phase B adds this column) = "eu" | "non-eu" | null.
  Composite Loop (Phase A2) derives residency = most-restrictive of referenced connectors.
  Skills: residency_tag = null by default (skills are code, not personal data — the gate concerns
  connectors that route personal data to external APIs). A skill CAN carry an explicit residency_tag
  if it embeds a non-EU API endpoint, but this is edge-case; default null = residency-agnostic.
- Fleet residency: Fleet.residency = "eu" | "row" | null (null = unrestricted, the default for own fleet).
  Astrovita fleet: residency="eu". Tori/Chef/Varys: residency=null (own fleet, no restriction).

## The gate (server-side, fail-closed)
In the reconcile engine (app/services/reconcile.py), BEFORE returning a diff to a member:
1. Resolve the fleet's residency.
2. If fleet.residency == "eu": filter OUT any connector in the desired state with residency_tag="non-eu".
   The diff's `connectors.remove` section includes the filtered connectors with reason="residency_blocked".
   The member never receives the non-EU connector config.
3. If fleet.residency == null or "row": no filtering (unrestricted).
4. Composite Loops with derived residency="non-eu": also filtered for EU fleets (the loop is blocked
   entirely — not just its connectors — because the loop orchestrates non-EU data flow).

## Where the gate lives
app/services/residency_gate.py:
```python
def filter_diff_by_residency(diff: dict, fleet_residency: str | None) -> tuple[dict, list[str]]:
    """Return (filtered_diff, blocked_reasons). Fail-closed: unknown residency = block."""
```
Called from recipes_reconcile() AFTER computing the diff, BEFORE returning. The blocked_reasons
are logged (ReconcileEvent? or a new ResidencyBlockEvent — keep it simple: log to the diff response
as `connectors.blocked: [{slug, reason}]`).

## Tests (tests/test_activate0701_residency.py)
1. EU fleet + non-EU connector in desired state -> connector filtered out, blocked reason present.
2. EU fleet + EU connector -> connector included.
3. EU fleet + null-residency skill -> skill included (skills are residency-agnostic by default).
4. EU fleet + composite loop w/ derived non-eu -> loop blocked.
5. ROW fleet + non-EU connector -> included (no restriction).
6. Null-residency fleet + anything -> included (unrestricted).
7. Unknown residency value -> fail-closed (block non-null-tagged artifacts).
