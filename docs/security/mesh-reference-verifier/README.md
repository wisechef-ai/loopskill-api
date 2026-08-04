# LoopSkill mesh credential — reference verifier

Spec: `../../../../../obsidian-vault/projects/loopskill/plans/2026-08-04-mesh0408-T0C-credential-trust-spec.md`
(also referenced from the PR body of `feat(mesh0408-T0-D)`).

This directory is a **standalone, runnable, zero-LoopSkill-dependency**
reference implementation of the mesh credential verification path. It is
deliberately importable from outside the `loopskill-api` repo entirely —
proven by `test_verify_mesh_credential.py`'s import style (`sys.path.insert`
against this directory only) and by manually running the whole suite from
`/tmp` with no LoopSkill package on `sys.path` (see the PR body for the
transcript).

## Files

| File | What it is |
|---|---|
| `verify_mesh_credential.py` | The spec §8 reference verifier. `verify(token, my_aud, snapshot, seen_jti) -> dict`. Raises on any failure. |
| `jwks_snapshot.py` | The spec §3 JWKS snapshot state machine — `JWKSStateMachine`. Fresh/aging/stale/cold states, rate-limited unknown-kid refresh, never a network call during verification. Replaces `PyJWKClient`, which the council rejected for two contradictory caching defects (see its module docstring). |
| `replay_store.py` | `InMemoryReplayStore` — the spec §5 atomic insert-if-absent `jti` store. **NOT multi-process safe** — a production receiver MUST swap this for Redis (or equivalent) with the same `insert_if_absent(jti, ttl) -> bool` contract. |
| `receiver_demo.py` | `MeshReceiver` — a minimal, conformant receiver implementing spec §2.2's normative org→profile mapping. Runnable directly (`python receiver_demo.py`) for a human-readable trace of a legitimate call, a cross-tenant rejection, and an unmapped-org rejection. |
| `test_verify_mesh_credential.py` | Pure verifier tests: happy path, forged signature, unknown kid, wrong audience, expired, TTL-over-class-max, array audience, org null/missing/empty, class confusion, replay, no-network-call-during-verify. |
| `test_jwks_snapshot.py` | The state-machine tests: cold start, fresh/aging/stale/hard-expiry, fetch-failure retains previous snapshot, rate-limited unknown-kid refresh, atomic swap. |
| `test_receiver_cross_tenant_rejection.py` | **The real lock #17 proof.** A receiver wired with ONLY these files rejects a cross-tenant call from a peer holding a perfectly valid, correctly-signed, correct-audience credential for its OWN (different) org. |

## Running standalone

```
cd docs/security/mesh-reference-verifier
/path/to/any/python/with/pyjwt/and/cryptography -m pytest -v
python receiver_demo.py
```

No `loopskill-api` checkout, no `app` package, no Hermes plugin required —
only `pyjwt[crypto]` (PyJWT + the `cryptography` extra) and pytest.

## What this does NOT cover

Say this plainly, per spec §8's closing requirement: `verify()` tells you
WHO is calling and that the credential is well-formed and unexpired. It
does not tell you WHAT they may reach. `receiver_demo.py`'s `MeshReceiver`
class is the worked example of the authorization layer spec §2.2 requires
on top of it — a real production receiver still has to:

- Manage the JWKS snapshot lifecycle (this ships `JWKSStateMachine`, but a
  real deployment wires its `fetch_jwks_fn` to an actual HTTP client with a
  real background refresh loop — the reference version's refresh is
  synchronous for simplicity).
- Run a shared (Redis) replay store across every worker process/host —
  `InMemoryReplayStore` is single-process only, by design, and says so.
- Monitor its own clock sync (NTP/chrony) and refuse to verify anything if
  the clock is known-unsynchronised (spec §4.8) — outside the scope of any
  file in this directory.
