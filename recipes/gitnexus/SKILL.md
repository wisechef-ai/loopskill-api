---
name: gitnexus
description: Analyze codebases using GitNexus — a knowledge graph engine that indexes every dependency, call chain, cluster, and execution flow. Use for bug hunting, architecture reviews, and understanding unfamiliar codebases.
tags: [codebase, analysis, knowledge-graph, architecture]
unhappy_paths:
  - condition: "gitnexus wiki exits non-zero with missing API key"
    recovery: "Set OPENAI_API_KEY or GITNEXUS_API_KEY in env, or fall back to the Direct KuzuDB Query pattern documented below (no LLM key required)."
  - condition: "gitnexus analyze removes existing semantic vectors when --embeddings is omitted"
    recovery: "Re-run with --embeddings to opt back in. There is no --skip-embeddings flag; omitting --embeddings is the opt-out and erases prior vectors."
  - condition: "Cypher query fails with 'desc is a reserved word' error"
    recovery: "Rename the column alias to 'direction' or drop the AS clause. Kuzu reserves 'desc' even when used outside ORDER BY."
  - condition: "Node script cannot resolve the kuzu package across installs"
    recovery: "Use the createRequire + npm-root-resolution pattern shown in Direct KuzuDB Query section. Do not hardcode npm prefix paths — they differ across Hermes/Codex/Claude installs and macOS/Linux."
  - condition: "MCP server hangs or never responds to agent calls"
    recovery: "MCP is stdio-only — do not pipe its output or run it as HTTP. Confirm the agent is invoking via stdio (e.g. claude mcp add gitnexus -- npx -y gitnexus@latest mcp)."
---

# GitNexus — Codebase Knowledge Graph

**Binary:** `gitnexus` on PATH (install with `npm install -g gitnexus`)
**Install:** `npm install -g gitnexus` (v1.3.6+)
**Languages:** JS/TS/Python/Rust/Go/Java (Tree-sitter based)

## When to Use
- Debugging complex bugs across multiple files
- Understanding unfamiliar codebases quickly
- Finding all callers/dependencies of a function
- Tracing execution flows end-to-end
- Code review with full architectural context
- Catching dead code, missing imports, circular deps

## Quick Reference

### Index a repo
```bash
gitnexus analyze [path]              # Index or update (incremental)
gitnexus analyze --force             # Full re-index (after major refactors)
gitnexus analyze --embeddings        # Enable embeddings for semantic queries
```

By default, omit `--embeddings` for faster indexing without semantic vectors.

### Query via MCP (preferred for agent use)
```bash
gitnexus mcp                         # Start MCP server (stdio)
```

MCP tools available:
- `get_nodes` — List all indexed nodes (functions, classes, files)
- `get_edges` — List all relationships (calls, imports, exports)
- `get_clusters` — Show module clusters
- `get_flows` — Trace execution flows
- `search_code` — Full-text search across indexed code
- `get_dependencies` — Find all deps of a node
- `get_dependents` — Find all callers of a node

### CLI management
```bash
gitnexus list                        # All indexed repos
gitnexus status                      # Index status for current repo
gitnexus clean                       # Delete index
gitnexus serve                       # HTTP server for web UI bridge
```

### Web UI
- Source: https://github.com/abhigyanpatwari/GitNexus
- Local bridge: `gitnexus serve` then open web UI

## Workflow for Bug Hunting

1. **Index the repo:**
   ```bash
   cd /path/to/repo && gitnexus analyze
   ```

2. **Start MCP and query** — or read generated context files:
   - `AGENTS.md` / `CLAUDE.md` — auto-generated codebase context when present
   - Generated skill/context files under the agent-specific skill directory, if `gitnexus setup` created them

3. **For quick checks without MCP**, read generated context files directly.

## Editor Integration
```bash
gitnexus setup                       # Auto-configure MCP for detected editors
# Or manual Claude Code example:
claude mcp add gitnexus -- npx -y gitnexus@latest mcp
```

## Tips
- Re-run `gitnexus analyze` after significant code changes (incremental)
- Use `--force` after major refactors
- Index stored in `.gitnexus/` in repo root
- For large repos (>5k files), CLI mode is better than web UI
- Use `command -v gitnexus` to verify the binary location instead of assuming an npm prefix

## Pitfalls
- Don't run on repos with massive `node_modules` unless `.gitignore` excludes it
- MCP server is stdio-based, meant for agent integration not HTTP
- `gitnexus wiki` requires `OPENAI_API_KEY` or `GITNEXUS_API_KEY` — fails hard otherwise. For agent use without an LLM key, query KuzuDB directly (see below).
- `--skip-embeddings` is **not** a valid flag (that's the default). Use `--embeddings` to opt IN, omit to opt out. Running `analyze` without `--embeddings` can remove existing embeddings.

## Direct KuzuDB Query (No MCP, No LLM Key)

When the MCP server isn't wired and `wiki` is unavailable, query the graph directly via Node. Resolve the installed `kuzu` package dynamically instead of hardcoding an npm prefix:

```javascript
// /tmp/gn_dump.js — run with `cd <repo> && node /tmp/gn_dump.js`
const { createRequire } = require("module");
const path = require("path");
const { execFileSync } = require("child_process");

function requireFromGitNexus(pkg) {
  const bin = execFileSync("command", ["-v", "gitnexus"], { shell: true, encoding: "utf8" }).trim();
  const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
  const candidates = [
    path.join(globalRoot, "gitnexus", "package.json"),
    path.join(path.dirname(path.dirname(bin)), "lib", "node_modules", "gitnexus", "package.json"),
  ];
  for (const pkgJson of candidates) {
    try {
      return createRequire(pkgJson)(pkg);
    } catch (_) {}
  }
  throw new Error(`Could not resolve ${pkg}; is gitnexus installed globally?`);
}

const { Database, Connection } = requireFromGitNexus("kuzu");
const db = new Database(".gitnexus/kuzu", 0, true, true);  // read-only
const conn = new Connection(db);

async function run(q, label, max=50) {
  console.log("\n=== " + label + " ===");
  const r = await conn.query(q);
  const rows = await r.getAll();
  rows.slice(0, max).forEach(row => console.log(JSON.stringify(row)));
  if (rows.length > max) console.log(`... +${rows.length - max} more`);
}

(async () => {
  await run("CALL show_tables() RETURN *", "tables");
  await run("MATCH (p:Process) RETURN p.* LIMIT 1", "process schema");
  await run("MATCH (p:Process) RETURN p.label, p.stepCount, p.entryPointId, p.terminalId ORDER BY p.stepCount DESC", "PROCESSES");
  await run("MATCH (c:Community) RETURN c.label, c.symbolCount, c.cohesion ORDER BY c.symbolCount DESC", "CLUSTERS");
  await run(`MATCH (n)-[r:CodeRelation]-() WHERE label(n) IN ['Function','Class','Method','Interface']
             RETURN n.id as id, count(r) as deg ORDER BY deg DESC LIMIT 25`, "TOP DEGREE NODES");
})();
```

### Cypher gotchas (Kuzu dialect)
- `desc` is a reserved word — never use it as a column alias. Use `direction` or skip the alias.
- Property access: `c.label` is fine, but to discover properties first use `RETURN c.* LIMIT 1`.
- Relationship table is `CodeRelation` (single table, types stored as property), not `:CALLS` / `:IMPORTS`.
- Read-only mode: pass 4th arg `true` to `Database()` constructor.

### Useful node tables
| Node | What | Key properties |
|---|---|---|
| `File` | source files | `path` |
| `Function` / `Method` / `Class` / `Interface` | code symbols | `id` (format: `Function:path:name:line`) |
| `Community` | clusters | `label`, `symbolCount`, `cohesion` |
| `Process` | execution flows | `label`, `stepCount`, `entryPointId`, `terminalId` |
| `CodeEmbedding` | semantic vectors (only if `--embeddings`) | — |

### When to use this fallback
- Cron jobs / sub-agents without MCP
- Quick architecture audit when LLM budget matters
- "Big picture" reports: clusters + processes + top-degree nodes give a complete topology in one Node script
- Combine with language-aware LOC tools for LoC-per-cluster sizing

## Verification
```bash
gitnexus --version                          # Confirm v1.3.6+ installed
cd /tmp && git init test-repo && cd test-repo && printf 'fn main(){}\n' > main.rs
gitnexus analyze .                          # Should index 1 file, exit 0
gitnexus status                             # Should show indexed file count
```
