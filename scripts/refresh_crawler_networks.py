#!/usr/bin/env python3
"""scripts/refresh_crawler_networks.py — regenerate config/crawler_networks.txt.

t_af0935a6: 30 of 38 ``installed:stranger`` rows were anonymous crawler hits
(Baidu spider in China Unicom ISP space, plus Meta and China Mobile singles).
The funnel ledger classifies an IP-only subject at a VERIFIED crawler address
as ``unknown`` (see app/services/crawler_networks.py). This script is the only
place that touches the network for that rule; ``classify()`` only reads the file.

The file has two parts:

1. Published ranges, regenerated on every run: exactly the prefixes a vendor
   publishes for its AUTONOMOUS crawlers, labelled ``<vendor>:<source-url>``.
   User-triggered fetchers (ChatGPT-User, Google user-triggered fetchers,
   Meta-ExternalFetcher) are deliberately NOT listed: they act for a person,
   and that person may be a real stranger.
   Meta publishes no crawler range list (developers.facebook.com
   web-crawlers page, checked 2026-10-10: UA strings only), so Meta has no
   published entries; adding its whole ASN would be an ISP-style block.

2. Forward-confirmed reverse DNS (FCrDNS) entries, append-only: an exact
   /32 (or /128) is admitted only when its PTR hostname ends in one of the
   vendor's crawler domains AND that hostname resolves back to the same IP.
   Labelled ``<vendor>:fcrdns:<ptr-hostname>:<YYYY-MM-DD>``. Existing FCrDNS
   lines are kept across runs. Never a /24, never an ASN.

Usage:
  python3 scripts/refresh_crawler_networks.py                       # regenerate published part
  python3 scripts/refresh_crawler_networks.py --ips 1.2.3.4 5.6.7.8 # + FCrDNS-check these
  python3 scripts/refresh_crawler_networks.py --ips-file ips.txt
  python3 scripts/refresh_crawler_networks.py --from-db             # IP-only installed:stranger IPs
  python3 scripts/refresh_crawler_networks.py --check               # exit 1 if published part is stale
  add --dry-run to print verdicts without writing; --json for a machine-readable report.

Exit codes:
  0  written (or --check / --dry-run found nothing to do)
  1  --check: the published part is stale
  2  fetch/parse failure (the existing file is left untouched)
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time
import urllib.request
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "config" / "crawler_networks.txt"

# Vendor-published AUTONOMOUS crawler range lists (all share the
# {"prefixes": [{"ipv4Prefix"|"ipv6Prefix": cidr}]} shape).
PUBLISHED_SOURCES: list[tuple[str, str]] = [
    ("Google", "https://developers.google.com/static/crawling/ipranges/common-crawlers.json"),
    ("Bing", "https://www.bing.com/toolbox/bingbot.json"),
    ("OpenAI-GPTBot", "https://openai.com/gptbot.json"),
    ("OpenAI-SearchBot", "https://openai.com/searchbot.json"),
]

# Vendors with no published list: PTR domain suffixes their crawlers resolve
# under, per the vendor's own verification guidance. Leading dot is required
# so "evilbaidu.com" never matches.
FCRDNS_DOMAINS: dict[str, tuple[str, ...]] = {
    "Baidu": (".baidu.com", ".baidu.jp"),
}

DNS_TIMEOUT_S = 5.0
_UA = "loopskill-crawler-networks-refresh/1 (+https://app.loopskill.io)"


# ── published ranges ─────────────────────────────────────────────────────


def _fetch_json(url: str, attempts: int = 3) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})  # noqa: S310 — fixed https sources
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                return json.load(resp)
        except (OSError, ValueError):
            if attempt == attempts:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def published_lines(fetch: Callable[[str], dict] = _fetch_json) -> list[str]:
    """``<cidr> <vendor>:<url>`` for every published crawler prefix, collapsed per source."""
    lines: list[str] = []
    for vendor, url in PUBLISHED_SOURCES:
        data = fetch(url)
        nets = []
        for p in data["prefixes"]:
            cidr = p.get("ipv4Prefix") or p.get("ipv6Prefix")
            if cidr:
                nets.append(ipaddress.ip_network(cidr.strip(), strict=True))
        if not nets:
            raise ValueError(f"{url}: no prefixes")
        for version in (4, 6):
            for net in ipaddress.collapse_addresses(n for n in nets if n.version == version):
                lines.append(f"{net} {vendor}:{url}")
    return lines


# ── FCrDNS ───────────────────────────────────────────────────────────────


@dataclass
class Verdict:
    ip: str
    admitted: bool
    vendor: str | None = None
    ptr: str | None = None
    reason: str = ""


def _ptr(ip: str) -> str:
    return socket.gethostbyaddr(ip)[0]


def _forward(host: str) -> set[str]:
    return {str(info[4][0]) for info in socket.getaddrinfo(host, None)}


def _with_timeout(fn: Callable, arg: str, timeout: float):
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn, arg).result(timeout=timeout)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def fcrdns_verify(
    ip: str,
    *,
    ptr_lookup: Callable[[str], str] = _ptr,
    forward_lookup: Callable[[str], set[str]] = _forward,
    timeout: float = DNS_TIMEOUT_S,
) -> Verdict:
    """Admit *ip* only if PTR is under a vendor crawler domain AND resolves back to *ip*."""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return Verdict(ip, False, reason="not an IP address")
    canon = str(addr)
    try:
        host = _with_timeout(ptr_lookup, canon, timeout)
    except FutureTimeout:
        return Verdict(canon, False, reason="PTR lookup timed out")
    except (OSError, UnicodeError) as exc:
        return Verdict(canon, False, reason=f"no PTR ({exc.__class__.__name__})")
    host = (host or "").strip().rstrip(".").lower()
    if not host:
        return Verdict(canon, False, reason="empty PTR")
    vendor = next(
        (v for v, suffixes in FCRDNS_DOMAINS.items() if any(host.endswith(s) for s in suffixes)),
        None,
    )
    if vendor is None:
        return Verdict(canon, False, ptr=host, reason="PTR not under a known crawler domain")
    try:
        forward = _with_timeout(forward_lookup, host, timeout)
    except FutureTimeout:
        return Verdict(canon, False, vendor=vendor, ptr=host, reason="forward lookup timed out")
    except (OSError, UnicodeError) as exc:
        return Verdict(
            canon, False, vendor=vendor, ptr=host, reason=f"forward lookup failed ({exc.__class__.__name__})"
        )
    resolved = set()
    for a in forward or ():
        try:
            resolved.add(str(ipaddress.ip_address(a)))
        except ValueError:
            continue
    if canon not in resolved:
        return Verdict(canon, False, vendor=vendor, ptr=host, reason="forward lookup does not return the IP")
    return Verdict(canon, True, vendor=vendor, ptr=host, reason="FCrDNS confirmed")


def fcrdns_line(v: Verdict, day: str) -> str:
    addr = ipaddress.ip_address(v.ip)
    prefix = 32 if addr.version == 4 else 128
    return f"{addr}/{prefix} {v.vendor}:fcrdns:{v.ptr}:{day}"


# ── file assembly ────────────────────────────────────────────────────────


def existing_fcrdns_lines(text: str) -> list[str]:
    return [
        ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#") and ":fcrdns:" in ln
    ]


def _covered(ip: str, lines: Iterable[str]) -> bool:
    addr = ipaddress.ip_address(ip)
    for ln in lines:
        cidr = ln.split(" ", 1)[0]
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except (ValueError, TypeError):
            continue
    return False


def render(published: list[str], fcrdns: list[str], day: str) -> str:
    header = (
        "# Generated by scripts/refresh_crawler_networks.py — do not edit by hand.\n"
        "# Format: <cidr> <vendor>:<source>. Read by app/services/crawler_networks.py.\n"
        f"# Published part regenerated {day} from: "
        + ", ".join(f"{v}={u}" for v, u in PUBLISHED_SOURCES)
        + "\n"
        "# Meta: no published crawler range list exists (UA strings only), so none listed.\n"
        "# FCrDNS part: exact /32 or /128 only, PTR under "
        + ", ".join(f"{v}{list(s)}" for v, s in FCRDNS_DOMAINS.items())
        + " AND forward-confirmed. Append-only.\n"
    )
    body = "# --- published ---\n" + "\n".join(published) + "\n# --- fcrdns ---\n"
    if fcrdns:
        body += "\n".join(fcrdns) + "\n"
    return header + body


def _body(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def stranger_ips_from_db() -> list[str]:
    """Distinct client IPs of IP-only (no api key) installed:stranger ledger rows."""
    sys.path.insert(0, str(ROOT))
    from uuid import UUID

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import FunnelEvent, InstallEvent

    with SessionLocal() as db:
        ids = []
        for sid in db.execute(
            select(FunnelEvent.source_event_id).where(
                FunnelEvent.stage == "installed", FunnelEvent.classification == "stranger"
            )
        ).scalars():
            try:
                ids.append(UUID(sid))
            except (ValueError, TypeError):
                continue
        rows = db.execute(
            select(InstallEvent.client_ip).where(InstallEvent.id.in_(ids), InstallEvent.api_key_id.is_(None))
        ).scalars()
        return sorted({(ip or "").strip() for ip in rows if (ip or "").strip()})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ips", nargs="*", default=[], help="IPs to FCrDNS-check")
    ap.add_argument("--ips-file", type=Path, help="file with one IP per line")
    ap.add_argument(
        "--from-db", action="store_true", help="check IP-only installed:stranger IPs from the app DB"
    )
    ap.add_argument("--check", action="store_true", help="exit 1 if the published part would change")
    ap.add_argument("--dry-run", action="store_true", help="print verdicts, do not write")
    ap.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    ap.add_argument("--out", type=Path, default=OUT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    day = datetime.now(UTC).date().isoformat()
    current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
    try:
        published = published_lines()
    except Exception as exc:  # noqa: BLE001 — any fetch/parse failure keeps the old file
        print(f"refresh failed, file untouched: {exc}", file=sys.stderr)
        return 2

    if args.check:
        old_pub = [ln for ln in _body(current) if ":fcrdns:" not in ln]
        return 0 if old_pub == published else 1

    candidates = list(args.ips)
    if args.ips_file:
        candidates += [
            ln.strip() for ln in args.ips_file.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
    if args.from_db:
        candidates += stranger_ips_from_db()

    fcrdns = existing_fcrdns_lines(current)
    verdicts: list[Verdict] = []
    for ip in dict.fromkeys(candidates):
        try:
            canon = str(ipaddress.ip_address(ip.strip()))
        except ValueError:
            verdicts.append(Verdict(ip, False, reason="not an IP address"))
            continue
        if _covered(canon, published) or _covered(canon, fcrdns):
            verdicts.append(Verdict(canon, False, reason="already listed"))
            continue
        v = fcrdns_verify(canon)
        verdicts.append(v)
        if v.admitted:
            fcrdns.append(fcrdns_line(v, day))

    text = render(published, fcrdns, day)
    changed = _body(text) != _body(current)
    if not args.dry_run and changed:
        args.out.write_text(text, encoding="utf-8")

    admitted = [v for v in verdicts if v.admitted]
    if args.json:
        print(
            json.dumps(
                {
                    "published": len(published),
                    "fcrdns": len(fcrdns),
                    "admitted": [asdict(v) for v in admitted],
                    "verdicts": [asdict(v) for v in verdicts],
                    "written": bool(changed and not args.dry_run),
                },
                indent=2,
            )
        )
    else:
        for v in verdicts:
            print(f"{'ADMIT ' if v.admitted else 'reject'} {v.ip:<40} {v.ptr or '-':<50} {v.reason}")
        state = "dry-run, not written" if args.dry_run else ("written" if changed else "unchanged")
        print(f"{len(published)} published + {len(fcrdns)} fcrdns entries ({state}) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
