"""Domain-filtering HTTPS proxy for sandbox network egress control.

Runs a local CONNECT proxy that only allows connections to domains
listed in the skill's `network_allow` manifest field. All other
domains are rejected with 403 Forbidden.

This proxy is started per sandbox execution when network_allow is
non-empty. It listens on a random high port on 127.0.0.1 and the
sandbox is configured with http_proxy/https_proxy env vars pointing
to it.

For HTTP (non-CONNECT) requests, the Host header is checked against
the allowlist. For HTTPS (CONNECT), the hostname in the CONNECT
request is checked.

Usage:
    proxy = DomainProxy(allowed_domains=["api.github.com"])
    await proxy.start()
    # proxy.port now has the listening port
    # inject http_proxy=http://127.0.0.1:{proxy.port} into sandbox
    await proxy.wait()  # or proxy.stop()
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Maximum bytes to read for initial request line
MAX_REQUEST_LINE = 8192

# Proxy response for denied requests
DENY_RESPONSE = b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nDomain not in allowlist\r\n"

# Proxy response for connect errors
CONNECT_ERROR_RESPONSE = b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nUpstream connection failed\r\n"

# Proxy response for successful CONNECT
CONNECT_OK_RESPONSE = b"HTTP/1.1 200 Connection Established\r\n\r\n"


def _domain_matches(hostname: str, allowed: list[str]) -> bool:
    """Check if hostname matches any allowed domain pattern.

    Supports exact match and wildcard subdomain matching:
      - "api.github.com" matches exactly "api.github.com"
      - "github.com" matches "github.com" AND "*.github.com"
    """
    hostname_lower = hostname.lower()
    for pattern in allowed:
        pattern_lower = pattern.lower()
        if hostname_lower == pattern_lower:
            return True
        # github.com also matches sub.github.com
        if hostname_lower.endswith("." + pattern_lower):
            return True
    return False


class DomainProxy:
    """Async CONNECT proxy with domain allowlist filtering."""

    def __init__(self, allowed_domains: list[str], bind_host: str = "127.0.0.1"):
        self.allowed_domains = allowed_domains
        self.bind_host = bind_host
        self.port: Optional[int] = None
        self._server: Optional[asyncio.Server] = None
        self._connections: set[asyncio.Task] = set()

    async def start(self) -> int:
        """Start the proxy server. Returns the port it's listening on."""
        self._server = await asyncio.start_server(
            self._handle_client,
            self.bind_host,
            0,  # random available port
        )
        # Extract the assigned port
        addr = self._server.sockets[0].getsockname()
        self.port = addr[1]
        logger.info(f"Domain proxy started on {self.bind_host}:{self.port} allowing {self.allowed_domains}")
        return self.port

    async def stop(self):
        """Stop the proxy server and cancel all active connections."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Cancel active connection tasks
        for task in self._connections:
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        logger.info("Domain proxy stopped")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle an incoming proxy connection."""
        task = asyncio.current_task()
        self._connections.add(task)
        try:
            await self._proxy_connection(reader, writer)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug(f"Proxy connection error: {exc}")
        finally:
            self._connections.discard(task)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _proxy_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Process a single proxy connection."""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=30.0)
        except asyncio.TimeoutError:
            return

        if not request_line:
            return

        request_str = request_line.decode("utf-8", errors="replace").strip()

        # Parse the request line: METHOD URI HTTP/VERSION
        parts = request_str.split()
        if len(parts) < 2:
            return

        method = parts[0].upper()

        if method == "CONNECT":
            await self._handle_connect(parts[1], reader, writer)
        else:
            # For plain HTTP requests, check the Host header
            await self._handle_http(request_str, parts[1], reader, writer)

    async def _handle_connect(self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a CONNECT (HTTPS) request."""
        # Parse host:port
        if ":" in target:
            hostname, port_str = target.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                writer.write(DENY_RESPONSE)
                await writer.drain()
                return
        else:
            hostname = target
            port = 443

        # Consume remaining headers (Host:, User-Agent:, etc.) until empty line
        while True:
            try:
                header_line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            except asyncio.TimeoutError:
                break
            if header_line == b"\r\n" or header_line == b"\n" or not header_line:
                break

        # Check domain allowlist
        if not _domain_matches(hostname, self.allowed_domains):
            logger.info(f"Proxy DENIED CONNECT to {hostname}:{port}")
            writer.write(DENY_RESPONSE)
            await writer.drain()
            return

        # Connect to upstream
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(hostname, port),
                timeout=10.0,
            )
        except (asyncio.TimeoutError, OSError, socket.gaierror) as exc:
            logger.info(f"Proxy CONNECT to {hostname}:{port} failed: {exc}")
            writer.write(CONNECT_ERROR_RESPONSE)
            await writer.drain()
            return

        # Send 200 to client
        writer.write(CONNECT_OK_RESPONSE)
        await writer.drain()

        logger.debug(f"Proxy CONNECT {hostname}:{port} established")

        # Bidirectional pipe
        await self._pipe(reader, upstream_writer, upstream_reader, writer)

    async def _handle_http(self, request_line: str, uri: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a plain HTTP request through the proxy."""
        # Read remaining headers to find Host
        headers = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                break
            if ":" in line_str:
                key, val = line_str.split(":", 1)
                headers[key.strip().lower()] = val.strip()

        # Extract hostname from URI or Host header
        hostname = headers.get("host", "")
        if hostname and ":" in hostname:
            hostname = hostname.split(":")[0]

        if not hostname:
            # Try to extract from URI
            if "://" in uri:
                from urllib.parse import urlparse
                parsed = urlparse(uri)
                hostname = parsed.hostname or ""

        if not _domain_matches(hostname, self.allowed_domains):
            logger.info(f"Proxy DENIED HTTP to {hostname}")
            writer.write(DENY_RESPONSE)
            await writer.drain()
            return

        # Forward the request
        port = 80
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(hostname, port),
                timeout=10.0,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            writer.write(CONNECT_ERROR_RESPONSE)
            await writer.drain()
            return

        # Reconstruct and forward the request
        upstream_writer.write(f"{request_line}\r\n".encode())
        for key, val in headers.items():
            upstream_writer.write(f"{key}: {val}\r\n".encode())
        upstream_writer.write(b"\r\n")
        await upstream_writer.drain()

        await self._pipe(reader, upstream_writer, upstream_reader, writer)

    async def _pipe(
        self,
        client_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ):
        """Bidirectional data pipe between client and upstream."""
        async def forward(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
                pass
            finally:
                try:
                    dst.close()
                    await dst.wait_closed()
                except Exception:
                    pass

        await asyncio.gather(
            forward(client_reader, upstream_writer),
            forward(upstream_reader, client_writer),
            return_exceptions=True,
        )


async def run_domain_proxy(allowed_domains: list[str]) -> DomainProxy:
    """Create and start a domain proxy. Returns the proxy instance."""
    proxy = DomainProxy(allowed_domains)
    await proxy.start()
    return proxy
