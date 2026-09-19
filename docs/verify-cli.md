# Deterministic verification CLI

`feature-map.yaml` describes the agent-reachable product surface: route, auth,
entry point, flows, and body invariants. `cli/verify.py` executes those flows
without a live server or network, using the production `create_app()` factory
and FastAPI `TestClient` against a disposable SQLite database.

Run the complete loop:

```sh
make verify
```

Run one named flow in a freshly seeded database:

```sh
make verify-flow FLOW=skill_search
```

For machine output use `python -m cli.verify --json run all`. To add a flow,
edit `feature-map.yaml`, add its function to `FLOW_RUNNERS`, and add body-level
assertions; the parity test rejects missing or orphaned flow IDs.

Fuzzing and swarms point at a disposable database, never production.
