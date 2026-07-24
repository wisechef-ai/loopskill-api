"""W0.4 (integrator_2905) — dependency security floor regression.

pip-audit on 2026-05-30 flagged two runtime transitive deps resolving into the
venv with known CVEs:

  starlette 1.0.0 → PYSEC-2026-161 (fixed 1.0.1)
  idna      3.13  → CVE-2026-45409 (fixed 3.15)

W0.4 pinned secure floors in requirements.txt (`starlette>=1.0.1`, `idna>=3.15`)
and bumped fastapi's floor to >=0.115.0. This test pins two things so a future
requirements edit can't silently regress:

  1. The installed starlette/idna are at-or-above the patched releases.
  2. requirements.txt still carries the explicit security-floor pins (a grep
     guard, so deleting the pin line trips CI even on a host that happens to
     have a newer transitive resolved).

Stripe webhook idempotency (the other W0.4 item) is already shipped + covered by
tests/test_subscription.py::test_webhook_replay_is_no_op (Gate 8) — not
re-pinned here.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _ver_tuple(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in v.split(".")[:3]:
        num = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


class TestInstalledSecurityFloors:
    def test_starlette_at_or_above_patch(self):
        import starlette

        assert _ver_tuple(starlette.__version__) >= (1, 0, 1), (
            f"starlette {starlette.__version__} is below the PYSEC-2026-161 "
            "patch (1.0.1) — security regression."
        )

    def test_idna_at_or_above_patch(self):
        import idna

        assert _ver_tuple(idna.__version__) >= (3, 15), (
            f"idna {idna.__version__} is below the CVE-2026-45409 patch (3.15) "
            "— security regression."
        )

    def test_urllib3_at_or_above_patch(self):
        import urllib3

        assert _ver_tuple(urllib3.__version__) >= (2, 7, 0), (
            f"urllib3 {urllib3.__version__} is below the PYSEC-2026-141/142 "
            "patch (2.7.0) — security regression."
        )


class TestRequirementsCarriesSecurityPins:
    def test_security_floor_pins_present(self):
        req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        # The pins must remain explicit so a fresh install cannot resolve a
        # vulnerable transitive. Match the package + a >= floor, version-agnostic.
        assert "starlette>=" in req, (
            "requirements.txt lost its explicit `starlette>=` security floor "
            "(W0.4 / PYSEC-2026-161). Restore it."
        )
        assert "idna>=" in req, (
            "requirements.txt lost its explicit `idna>=` security floor "
            "(W0.4 / CVE-2026-45409). Restore it."
        )
        assert "urllib3>=" in req, (
            "requirements.txt lost its explicit `urllib3>=` security floor "
            "(W0.4 / PYSEC-2026-141/142). Restore it."
        )

    def test_fastapi_floor_not_below_115(self):
        req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
        for line in req.splitlines():
            line = line.strip()
            if line.startswith("fastapi>="):
                floor = line.split(">=", 1)[1].split(",")[0].strip()
                assert _ver_tuple(floor) >= (0, 115, 0), (
                    f"fastapi floor {floor} regressed below 0.115.0 (W0.4)."
                )
                return
        raise AssertionError("requirements.txt has no `fastapi>=` floor line.")
