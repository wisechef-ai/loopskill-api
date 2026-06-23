#!/usr/bin/env python3
"""Standalone domain proxy runner — spawned as a subprocess by SandboxRunner.

Usage: python _run_proxy.py api.github.com registry.npmjs.org

Prints the listening port to stdout on the first line, then runs until killed.
"""

import asyncio
import os
import sys

# Add parent to path so we can import the proxy module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from domain_proxy import DomainProxy


async def main():
    allowed_domains = sys.argv[1:]
    if not allowed_domains:
        print("ERROR: no domains provided", file=sys.stderr)
        sys.exit(1)

    proxy = DomainProxy(allowed_domains)
    port = await proxy.start()

    # Signal ready by printing port (flushed)
    print(port, flush=True)

    # Run until killed
    try:
        await asyncio.Future()  # blocks forever
    except asyncio.CancelledError:
        await proxy.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
