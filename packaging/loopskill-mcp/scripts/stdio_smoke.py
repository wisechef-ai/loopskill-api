#!/usr/bin/env python3
"""Keyless stdio smoke for any loopskill-mcp launch command.

Usage:
    python3 stdio_smoke.py docker run -i --rm loopskill-mcp
    python3 stdio_smoke.py uvx --from dist/loopskill_mcp-0.9.43-py3-none-any.whl loopskill-mcp

Sends initialize -> notifications/initialized -> tools/list over stdio and
exits 0 only when tools/list returns at least MIN_TOOLS tools. This is the
exact handshake a directory (Glama, Smithery) runs to introspect a server, so
it is the release gate for both the PyPI wheel and the container image.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

MIN_TOOLS = int(os.environ.get("LOOPSKILL_MCP_SMOKE_MIN_TOOLS", "10"))
TIMEOUT_S = 90


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    # Keyless by design: strip EVERY *API_KEY* var. The bridge reads four
    # aliases (LOOPSKILL_/MCP_LOOPSKILL_/RECIPES_/MCP_RECIPES_API_KEY), and a
    # maintainer's shell key silently turns this into a keyed smoke that can
    # never catch a keyless bug (it hid exactly that bug on first run).
    env = {k: v for k, v in os.environ.items() if "API_KEY" not in k.upper()}
    msgs = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "loopskill-mcp-stdio-smoke", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdin and proc.stdout
    for m in msgs:
        proc.stdin.write(json.dumps(m) + "\n")
        proc.stdin.flush()

    tools = None
    server = None
    import selectors
    import time

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + TIMEOUT_S
    while tools is None and time.monotonic() < deadline:
        if not sel.select(timeout=max(0.0, deadline - time.monotonic())):
            break
        line = proc.stdout.readline()
        if not line:
            break
        try:
            resp = json.loads(line)
        except json.JSONDecodeError:
            continue
        if resp.get("id") == 1:
            server = (resp.get("result") or {}).get("serverInfo")
        elif resp.get("id") == 2:
            if "error" in resp:
                print(f"FAIL tools/list error: {resp['error']}", file=sys.stderr)
                return _reap(proc, 1)
            tools = (resp.get("result") or {}).get("tools") or []

    return _reap(proc, None, tools, server)


def _reap(proc, rc, tools=None, server=None) -> int:
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    stderr_tail = (proc.stderr.read() if proc.stderr else "")[-2000:]

    if rc is not None:
        print(stderr_tail, file=sys.stderr)
        return rc
    if tools is None:
        print(f"FAIL no tools/list response within {TIMEOUT_S}s", file=sys.stderr)
        print(stderr_tail, file=sys.stderr)
        return 1
    print(f"serverInfo: {server}")
    print(f"tools/list: {len(tools)} tools, e.g. {[t.get('name') for t in tools[:5]]}")
    if len(tools) < MIN_TOOLS:
        print(f"FAIL expected >= {MIN_TOOLS} tools", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
