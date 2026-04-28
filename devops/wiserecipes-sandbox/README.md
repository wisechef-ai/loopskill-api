# WiseRecipes Sandbox Runner

## Overview

The WiseRecipes sandbox runner executes skill scripts inside an isolated environment using either **Firejail** (preferred, SUID binary) or **bubblewrap** (fallback). It is triggered when a skill's `skill.toml` declares a `[sandbox]` block.

## Architecture

```
┌─────────────────────────────────────────────┐
│  API Request: POST /api/skills/{slug}/sandbox/run  │
├─────────────────────────────────────────────┤
│  SandboxRunner                              │
│  ├── _detect_backend() → firejail | bwrap   │
│  ├── SandboxProfile.from_manifest(toml)     │
│  │   ├── network_allow → --net=none / pass  │
│  │   ├── fs_write → --bind writable dirs    │
│  │   ├── exec_allow → seccomp (TODO)        │
│  │   ├── memory_mb → --rlimit-as            │
│  │   └── timeout_seconds → --timeout        │
│  ├── _run_firejail() or _run_bwrap()        │
│  └── TelemetryEvent → telemetry_events table│
└─────────────────────────────────────────────┘
```

## skill.toml [sandbox] Block

```toml
[sandbox]
network_allow = ["api.github.com", "registry.npmjs.org"]  # empty = no network
fs_write = ["/tmp", "/home/skill/.cache"]                  # dirs the skill can write to
exec_allow = ["python3", "node", "bash", "sh"]             # allowed executables
memory_mb = 512           # memory limit (max 4096)
timeout_seconds = 120     # execution timeout (max 600)
env_pass = ["PATH", "LANG", "HOME"]  # host env vars to pass through
```

## Backend Selection

| Backend | Requirements | Network Isolation | Notes |
|---------|-------------|-------------------|-------|
| **Firejail** | SUID binary installed | `--net=none` (works without CAP_NET_ADMIN) | Preferred. Works in containers. |
| **bubblewrap** | User namespace support | `--unshare-net` (requires CAP_NET_ADMIN for loopback) | Fallback. May fail in restricted containers. |

Detection happens at `SandboxRunner.__init__()` time. If neither is available, `run()` returns an error result with `exit_code=-1`.

## Network Isolation

### Current Implementation
- **No allowlist** (`network_allow = []`): Complete network isolation via `--net=none` (firejail) or `--unshare-net` (bwrap). All outbound connections fail.
- **With allowlist** (`network_allow = ["api.github.com"]`): Network namespace is shared with host. All domains are reachable.

### Production Enhancement (TODO)
Neither firejail nor bwrap supports per-domain filtering natively. For production, layer one of:
1. **Squid proxy** with domain allowlist — point `http_proxy` inside the sandbox
2. **nftables rules** on the host filtering by DNS-resolved IPs
3. **DNS proxy** that only resolves allowlisted domains

The `network_allow` list is stored in the profile for the proxy layer to consume.

## Filesystem Isolation

- System dirs (`/usr`, `/lib`, `/lib64`, `/bin`, `/sbin`) are mounted **read-only**
- `/tmp` is always writable (private mount)
- Additional writable dirs from `fs_write` are bind-mounted from a temporary location
- Skill directory is bind-mounted as `/skill` (the working directory)

## Resource Limits

- Memory: enforced via `--rlimit-as` (firejail) or `preexec_fn` (bwrap)
- Timeout: enforced by both firejail's `--timeout` and Python's `subprocess.TimeoutExpired`
- Max output: 1MB per stream (stdout + stderr), truncated in API response to 5000 chars

## Launch-Grade Skills (DoD)

Five test skills with sandbox profiles:

| Skill | Network | Key Features |
|-------|---------|-------------|
| `hello-sandbox` | Isolated | Basic test, network blocking verification |
| `fetch-github-stats` | api.github.com | Network allowlist, curl usage |
| `python-processor` | Isolated | Python3 execution, JSON output |
| `npm-installer` | registry.npmjs.org | Node.js, npm package install |
| `file-transformer` | Isolated | Shell utilities (awk, sort, grep), CSV processing |

All profiles validate cleanly (no dangerous binary warnings, reasonable resource limits).

## API Endpoints

```
GET  /api/skills/{slug}/sandbox/status   → Check sandbox support + profile
POST /api/skills/{slug}/sandbox/run      → Execute in sandbox
```

The `POST /run` endpoint records execution results as `telemetry_events` with `event_type="sandbox_run"`.

## Running Tests

```bash
cd /home/wisechef/wiserecipes-api
python3 -m pytest tests/test_sandbox.py -v
```

- **Unit tests** (49): Profile parsing, validation, arg generation, runner error handling — always pass
- **Integration tests** (3): Actual sandbox execution — require functional bwrap or firejail on the host

## Pitfalls & Lessons Learned

### 1. bubblewrap in containers fails with "setting up uid map: Permission denied"
bwrap needs user namespace support and working `newuidmap`/`newgidmap`. Even with `unprivileged_userns_clone=1`, containers often restrict this. **Firejail with SUID is the solution.**

### 2. `--unshare-net` requires CAP_NET_ADMIN
When bwrap tries to configure the loopback interface in a new network namespace, it needs `CAP_NET_ADMIN`. Without it: `loopback: Failed RTM_NEWADDR: Operation not permitted`. Workaround: use firejail's `--net=none` which doesn't need this capability.

### 3. No per-domain network filtering in either backend
Both firejail and bwrap provide all-or-nothing network isolation. Domain-level filtering requires an external proxy (Squid, tinyproxy) or nftables rules.

### 4. bwrap reads /etc/resolv.conf only when network is allowed
The profile code only binds `/etc/resolv.conf` when `network_allow` is non-empty. Skills with no network don't get DNS — which is correct since they have no network.

### 5. Firejail output includes boilerplate
Firejail prepends status lines like "Parent pid ..." and "Child process initialized ...". The runner strips these via `_parse_firejail_output()`.

### 6. Seccomp for exec binary filtering is not yet implemented
The `exec_allow` list is validated (dangerous binaries rejected) but not enforced at runtime. TODO: generate seccomp BPF filter from the allowlist using libseccomp-python.

## File Map

```
wiserecipes-api/
├── app/sandbox/
│   ├── __init__.py          # Public API exports
│   ├── profile.py           # SandboxProfile: TOML parsing + arg generation
│   ├── runner.py            # SandboxRunner: execution engine (firejail + bwrap)
│   └── routes.py            # FastAPI endpoints for sandbox operations
├── dev-skills/
│   ├── hello-sandbox/       # Basic test skill
│   ├── fetch-github-stats/  # Network allowlist test
│   ├── python-processor/    # Python execution test
│   ├── npm-installer/       # Node.js/npm test
│   └── file-transformer/    # Shell utilities test
├── devops/
│   └── wiserecipes-sandbox/
│       ├── README.md        # This file
│       └── NETWORK_ISOLATION.md  # Network filtering design
└── tests/
    └── test_sandbox.py      # 52 tests (49 unit + 3 integration)
```
