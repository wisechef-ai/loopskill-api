#!/usr/bin/env python3
"""Drive `uvx --from <wheel> loopskill-mcp` over stdio: initialize + tools/list.

Prints a clearly-delimited transcript of what was sent and what came back,
suitable for pasting into a PR body as proof of a real live handshake.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

WHEEL_GLOB_DIR = sys.argv[1] if len(sys.argv) > 1 else "dist"


def find_wheel(d: str) -> str:
    import glob

    matches = sorted(glob.glob(os.path.join(d, "loopskill_mcp-*.whl")))
    if not matches:
        raise SystemExit(f"no wheel found in {d}")
    return matches[-1]


def main() -> None:
    wheel = find_wheel(WHEEL_GLOB_DIR)
    api_key = os.environ.get("LOOPSKILL_MCP_LIVE_TEST_KEY")
    env = dict(os.environ)
    if api_key:
        env["LOOPSKILL_API_KEY"] = api_key

    cmd = ["uvx", "--from", wheel, "loopskill-mcp"]
    print("=== COMMAND ===")
    print(" ".join(cmd))
    print()

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "transcript-probe", "version": "0.1"},
        },
    }
    initialized_notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    list_tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

    def send(msg: dict) -> None:
        line = json.dumps(msg)
        print("=== SENT ===")
        print(line)
        proc.stdin.write(line + "\n")
        proc.stdin.flush()

    def recv() -> dict:
        line = proc.stdout.readline()
        print("=== RECEIVED ===")
        print(line.strip())
        return json.loads(line)

    send(init_req)
    init_resp = recv()

    send(initialized_notif)  # no response expected for a notification

    send(list_tools_req)
    list_resp = recv()

    proc.stdin.close()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()

    stderr_out = proc.stderr.read()
    if stderr_out.strip():
        print("=== STDERR (bridge logs) ===")
        print(stderr_out.strip())

    tools = list_resp.get("result", {}).get("tools", [])
    names = sorted(t["name"] for t in tools)

    print()
    print("=== SUMMARY ===")
    print(f"server: {init_resp.get('result', {}).get('serverInfo')}")
    print(f"tool count: {len(tools)}")
    print(f"has loopskill_bundle_install: {'loopskill_bundle_install' in names}")
    print(f"has loopskill_search: {'loopskill_search' in names}")
    print("tool names:")
    for n in names:
        print(f"  - {n}")


if __name__ == "__main__":
    main()
