"""mesh_0408 T0-D — LoopSkill mesh credential issuance + trust protocol.

Implements the spec at
projects/loopskill/plans/2026-08-04-mesh0408-T0C-credential-trust-spec.md
(v2, post-council). See that document for the normative decisions; this
package implements them, it does not re-derive them.

Submodules:
  constants  — wire-format constants (classes, TTLs, audiences, claim URIs)
  ulid       — dependency-free ULID generator for `jti` (§2.3.3)
  keys       — the separate Ed25519 signing key ring (§0) — NO legacy fallback
  mint       — scoped, audience-bound, transactional credential minting (§1, §4.9)
  errors     — domain exceptions (mesh_tenant_unassigned, etc.)
"""
