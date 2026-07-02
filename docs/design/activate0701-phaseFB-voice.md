# Phase FB+VOICE — AGENT VOICE WIRING + FLEET OWNER INBOX — design contract (loopskill_activate_0701)

Author: Tori. Implementer: subagent (needs Phase 1 FleetMember + Phase T LoopRun/SkillErrorReport
for the routing targets). Reviewer: glm-5.2 council (per plan — if Z.AI capped, codex covers).
Branch: loopskill_activate_0701/phaseFB.

## Product (D6)
Agents DEPLOYED via LoopSkill "have a voice": feedback, skill-error reports, upgrade proposals,
feature requests flow from deployed agents to the fleet owner + skill maintainer. The platform and
skills improve from real usage — never blindly after release.

## What's ALREADY built (verified — §1 ground truth)
- FeedbackSubmission model (models.py:1074) + POST /api/v1/feedback + recipes_feedback MCP tool.
- RecipifyRequest model (models.py:1047) + POST /api/v1/recipify-request + recipes_request_recipe.
- recipes_report_skill_error MCP tool (skill_error.py).
- recipes_propose_skill_patch MCP tool (skill_patch.py).
- github_dispatch.py: repository_dispatch to maintainer repo (default + user-routable PAT path).
- app/services/provenance.py: provenance minting + route_targets_for_provenance (the routing seam).

## What this phase WIRES (not rebuilds)

### FB1 — Deploy-time provenance minting -> voice routing
- When a FleetMember reconciles a skill (Phase 1's reconcile-report path), mint/attach a
  provenance_id if the member doesn't already have one for that skill. The provenance seam
  (provenance.py) already resolves install_event -> skill maintainer repo. Wire it into the
  member reconcile flow: the member's report carries provenance_id -> voice routes to the
  RIGHT maintainer (not a generic inbox).
- The SkillErrorReport rows from Phase T (sync-report ingestion) with feedback_status='pending'
  are the INPUT queue. This phase processes them: each pending row with a provenance_id gets
  routed via github_dispatch to the skill maintainer's repo as a GitHub issue (same mechanic
  as recipes_report_skill_error, but triggered server-side from the aggregated sync-report
  instead of per-event MCP call — D9 data efficiency: batched, not chatty).

### FB2 — Voice tools wired by default on deploy
- When a bundle deploys skills to a member, the deploy also ensures the voice MCP tools are
  available to that agent. In practice: the deploy writes a voice-config snippet to the
  member's skills dir (~/.hermes/loopskill/voice.json) that the agent's sync script reads and
  surfaces the tools. The tools themselves (recipes_feedback etc.) are already registered —
  this is about DISCOVERY + DEFAULT-AVAILABILITY, not new code.
- Per-fleet auto-issue toggle (N4 default: OFF for clients, ON for own fleet):
  Fleet.auto_issue_feedback Boolean default=False. When True, pending SkillErrorReports +
  failing LoopRuns auto-file GitHub issues. When False, they aggregate in the fleet owner inbox
  (below) but don't auto-file. Own fleet (Tori/Chef/Varys) = True.

### FB3 — Fleet owner inbox API (aggregated voice stream)
NEW route module app/voice_routes.py:
- GET /api/fleets/{fleet_id}/voice-inbox (auth: fleet owner or master) -> paginated aggregated stream:
  ```json
  {"items": [
    {"type": "skill_error", "id": "...", "slug": "...", "summary": "...", "member_host": "...",
     "created_at": "...", "status": "pending|filed|resolved"},
    {"type": "loop_run_failure", "id": "...", "loop_slug": "...", "detail": "...", ...},
    {"type": "recipify_request", "id": "...", "target_name": "...", ...},
    {"type": "feedback", "id": "...", "category": "...", "message": "...", ...}
  ], "next_after": "..."}
  ```
  Reads from: SkillErrorReport (Phase T), LoopRun where outcome='failure' (Phase T),
  RecipifyRequest, FeedbackSubmission — UNION'd by created_at, keyset-paginated.
  Filters: status (default: pending+filed), type (optional filter), since (timestamp).
- POST /api/fleets/{fleet_id}/voice-inbox/{id}/resolve -> mark item resolved (fleet owner action).
- MCP tool loopskill_voice_inbox_read -> same data, agent-callable (the fleet owner agent reads
  its own inbox).

## Gates (plan §2 Phase FB+VOICE)
1. Killed verifier on Tori -> routed issue w/ loop identity + run evidence (inject a failing
   LoopRun, verify a GitHub issue is filed in the maintainer repo with the loop_slug + run detail).
2. An agent-filed recipify_request lands in the fleet owner inbox (POST via MCP -> appears in
   GET /api/fleets/{id}/voice-inbox).
3. Per-fleet auto-issue toggle works: toggle OFF -> no auto-file (items stay pending in inbox);
   toggle ON -> auto-file.

## Tests (tests/test_activate0701_voice.py)
1. provenance minting on member reconcile-report -> provenance_id non-null.
2. pending SkillErrorReport with provenance -> auto-filed (auto_issue=True) GitHub issue
   (mock github_dispatch, assert called with correct repo + payload).
3. auto_issue=False -> not filed, stays pending.
4. voice inbox UNION query: items from all 4 sources, ordered by created_at.
5. keyset pagination on inbox.
6. recipify_request via MCP -> appears in inbox.
7. resolve endpoint -> status updated.
8. MCP voice_inbox_read returns same data as HTTP.
