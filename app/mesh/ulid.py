"""Dependency-free ULID generator. Spec §2.3.3 — "`jti` is a ULID, unique per mint."

We do not add `python-ulid` as a new dependency for a 20-line primitive.
Format: 48-bit millisecond timestamp + 80 bits of CSPRNG, Crockford base32,
26 characters, matching the standard ULID spec (https://github.com/ulid/spec).
"""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """Return a new 26-character Crockford-base32 ULID string."""
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")  # 80 random bits
    value = (ts_ms << 80) | rand  # 128-bit total

    chars = []
    for _ in range(26):
        value, rem = divmod(value, 32)
        chars.append(_CROCKFORD[rem])
    return "".join(reversed(chars))
