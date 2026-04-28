# Network Isolation Design for WiseRecipes Sandbox

## Problem

Skills may declare a `network_allow` allowlist in their `skill.toml`:

```toml
[sandbox]
network_allow = ["api.github.com", "registry.npmjs.org"]
```

Neither **Firejail** nor **bubblewrap** supports per-domain network filtering natively. They provide all-or-nothing isolation:
- Firejail: `--net=none` (complete isolation) or full network access
- bwrap: `--unshare-net` (complete isolation) or full network access

## Current Implementation

```python
# In SandboxProfile.to_bwrap_args():
if not self.network_allow:
    args.append("--unshare-net")

# In SandboxProfile.to_firejail_args():
if not self.network_allow:
    args.append("--net=none")
```

**Behavior:**
- `network_allow = []` → complete network isolation (provable)
- `network_allow = ["api.github.com"]` → full network access (any domain reachable)

## Production Solution: Squid Proxy

### Architecture

```
┌──────────────────────┐
│   Sandbox Process    │
│   http_proxy=squid   │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│   Squid (localhost)  │
│   ACL: allowlisted   │
│   domains only       │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│   Internet           │
└──────────────────────┘
```

### Implementation

1. **Squid config per skill execution:**
   ```conf
   # /etc/squid/sandbox-{sandbox_id}.conf
   http_port 127.0.0.1:{port}
   acl allowed_domains dstdomain api.github.com registry.npmjs.org
   http_access allow allowed_domains
   http_access deny all
   ```

2. **Runner injects proxy env vars:**
   ```python
   bwrap_args.extend(["--setenv", "http_proxy", f"http://127.0.0.1:{port}"])
   bwrap_args.extend(["--setenv", "https_proxy", f"http://127.0.0.1:{port}"])
   ```

3. **Skill processes route through Squid** which enforces the domain allowlist.

### Alternative: DNS Proxy (for non-HTTP protocols)

For skills that use non-HTTP protocols (e.g., SSH, custom TCP):
- Run `dnsmasq` with a config that only resolves allowlisted domains
- Point the sandbox's `/etc/resolv.conf` at the local dnsmasq
- Connections to unresolved domains will fail at DNS resolution

### Alternative: nftables Rules

```bash
# Allow DNS
nft add rule inet sandbox-out udp dport 53 accept
# Allow specific IPs (resolve allowlisted domains first)
nft add rule inet sandbox-out ip daddr {github_ip_set} accept
# Deny everything else
nft add rule inet sandbox-out reject
```

Complexity: requires resolving domain → IP mapping periodically, handling CDNs.

## Recommendation

**Phase 1 (current):** All-or-nothing network isolation. `network_allow=[]` is provably safe. Skills needing network get full access but are reviewed before publication.

**Phase 2:** Squid proxy for HTTP/HTTPS skills. Covers 95% of use cases (API calls, package installs).

**Phase 3:** DNS proxy for non-HTTP skills. Covers SSH, git, custom protocols.

## Test Coverage

Network isolation is verified by:
1. `TestNetworkEgressBlocking` — verifies `--unshare-net` / `--net=none` in generated args
2. `hello-sandbox/setup.sh` — attempts `curl https://example.com` and asserts failure
3. `file-transformer/setup.sh` — same network blocking test
4. `python-processor/setup.sh` — tests `urllib.request.urlopen()` failure

For skills with network allowlists (fetch-github-stats, npm-installer), the test scripts verify allowed domains are reachable and note that non-allowed domains would require the proxy layer for enforcement.
