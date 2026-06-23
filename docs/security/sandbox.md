# Sandbox Security Documentation

## Platform Support Matrix

| Platform | Backend | Status | Notes |
|----------|---------|--------|-------|
| Linux | firejail | ✅ Supported (primary) | SUID binary; works without user namespaces |
| Linux | bubblewrap (bwrap) | ✅ Supported (fallback) | Requires user namespace support (`CONFIG_USER_NS=y`) |
| macOS (darwin) | — | ❌ **Unsupported — fails loud** | See [macOS behaviour](#macos-behaviour) |
| Windows | — | ❌ Unsupported | Not planned |

### macOS Behaviour

On macOS, neither firejail nor bubblewrap is available.  The sandbox **fails
loudly** at startup (`SandboxBackendUnavailable`) rather than silently running
skill scripts without confinement.

```
SandboxBackendUnavailable: Sandbox backend unavailable on macOS: neither
firejail nor bwrap is installed.  The WiseRecipes sandbox is Linux-only
(firejail / bubblewrap).  macOS does not support these backends — running
without confinement would silently expose the host to untrusted skill scripts.
Use a Linux host to run the sandbox.
```

**Why fail-loud instead of a passthrough?**  A silent pass-through would mean
skill scripts run with full host access.  The test contract would lie — tests
would pass while the security invariant (confinement) is completely absent.
Fail-loud ensures:

1. Developers on macOS know immediately that the sandbox cannot be tested locally.
2. No code path exists that accidentally deploys un-sandboxed skill execution.
3. CI/CD on macOS will fail clearly rather than produce false-positive green runs.

---

## Backend Selection

`SandboxRunner._detect_backend()` probes `PATH` at startup:

```
firejail on PATH?  →  backend = "firejail"   (primary)
bwrap on PATH?     →  backend = "bwrap"      (fallback)
Neither, macOS?    →  raise SandboxBackendUnavailable
Neither, Linux?    →  backend = "none"       (run returns error SandboxResult)
```

The `backend = "none"` path on Linux exists for CI runners that have neither
tool installed.  In that case `runner.run()` returns a `SandboxResult` with
`exit_code=-1` and `error="No sandbox backend available"` rather than raising,
so callers always get a structured result object.

---

## Threat Model

### Assets

| Asset | Description |
|-------|-------------|
| Host filesystem | All files accessible by the worker process |
| Host network | Internal services, cloud metadata endpoints (IMDS), private subnets |
| Worker process credentials | API keys, database credentials, secrets in env |
| Other tenants' data | Isolation between concurrent skill executions |

### Threats Mitigated

| Threat | Mitigation |
|--------|------------|
| Malicious skill reads host files | firejail/bwrap: read-only bind-mount of `/`, skill dir only writeable path |
| Malicious skill writes to host | Only explicitly declared `fs_write` paths are bind-mounted writable |
| Unrestricted outbound network | `--unshare-net` (no `network_allow`) or domain-filtering proxy (with `network_allow`) |
| SSRF to IMDS / internal services | Network isolation + `SandboxProfile.validate()` rejects private IP literals |
| Skill spawning arbitrary subprocesses | `exec_allow` list controls permitted binaries |
| Resource exhaustion (CPU, RAM) | `memory_mb` + `timeout_seconds` limits enforced |
| Sandbox escape via proxy fallback | Proxy startup failure → fail CLOSED (Issue #8); never falls back to unrestricted |

### Residual Risks

- **Kernel exploits**: firejail/bwrap cannot protect against kernel-level
  vulnerabilities.  Skills should be reviewed before execution on sensitive hosts.
- **Side-channel attacks**: Memory/CPU side channels (Spectre, Meltdown) are not
  mitigated at the sandbox layer.
- **DNS rebinding**: The domain proxy validates on connect; DNS rebinding after
  the initial connection is not prevented (use a full-featured proxy for
  stricter enforcement).

---

## Allowlist Contract

### Network Allowlist (`network_allow`)

Declared in `skill.toml`:

```toml
[sandbox]
network_allow = ["api.github.com", "registry.npmjs.org"]
```

**Enforcement**:

1. `SandboxProfile.validate()` rejects:
   - Private/loopback IP literals (`127.x`, `10.x`, `192.168.x`, `172.16–31.x`, etc.)
   - Link-local addresses (`169.254.x.x`, IMDS endpoint)
   - IPv6 loopback (`::1`)
   - Multicast / unspecified addresses
   - Malformed hostnames (spaces, shell metacharacters, RFC 1035 violations)
   - `localhost` by name

2. When `network_allow` is empty: `--unshare-net` isolates the sandbox
   completely.

3. When `network_allow` is non-empty: a domain-filtering CONNECT proxy starts
   on a random local port; `http_proxy`/`https_proxy` env vars are injected
   into the sandbox.  The proxy enforces exact-match or subdomain-match against
   the allowlist.

4. **Proxy startup failure → fail CLOSED** (Issue #8 fix): if the domain proxy
   cannot start, the sandbox run is aborted with `error="proxy_failed"`.  The
   skill is never executed with unrestricted network access.

### Filesystem Allowlist (`fs_write`)

```toml
[sandbox]
fs_write = ["/tmp", "/home/skill/.cache"]
```

Only explicitly listed absolute paths are bind-mounted as writable.  The rest
of the filesystem is mounted read-only.  Relative paths produce a validation
warning.

### Exec Allowlist (`exec_allow`)

```toml
[sandbox]
exec_allow = ["python3", "node", "bash"]
```

Dangerous binaries (`rm`, `sudo`, `su`, `mkfs`, `dd`) produce validation
warnings.  The list is enforced by firejail's `--whitelist` / bwrap's
namespace restrictions.

---

## Running Sandbox Tests

Sandbox tests are marked `sandbox_linux_only` and **only run on Linux**:

```bash
# Linux — tests run normally
source .venv/bin/activate
python -m pytest tests/ -k sandbox -q

# macOS — tests are automatically skipped with a clear reason
# (no false-positive pass-throughs)
```

The `sandbox_linux_only` marker is registered in `pyproject.toml` and the
skip logic lives in the root `conftest.py::pytest_collection_modifyitems` hook.

---

## See Also

- `app/sandbox/runner.py` — `SandboxRunner`, `SandboxBackendUnavailable`
- `app/sandbox/profile.py` — `SandboxProfile`, `validate()`
- `app/sandbox/domain_proxy.py` — `DomainProxy`
- `docs/security/incident-response.md` — incident response playbook
