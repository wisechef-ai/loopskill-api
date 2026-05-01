"""End-to-end install-verification runner (F-VERIFY).

Drives the F.1–F.7 modules in the order a real install would walk them so
publishers (and Tori, on a fresh CX22) can confirm a recipe.yaml installs
cleanly before broadcast.

Stage order (each is timed):
  1. parse + validate the recipe.yaml          (F.1)
  2. probe the host + compatibility comparison (F.7)
  3. resolve + (mock) install binaries         (F.2)
  4. provision + health-check services         (F.3 / F.5)
  5. register crons                            (F.4)
  6. aggregate health                          (F.5)

This module never actually runs the install on the worker — pass
``dry_run=True`` (the default for tests) and every adapter / service /
cron call is short-circuited and timed only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from runtime import probe as probe_mod
from runtime.adapters import linux as linux_adapter
from runtime.adapters import macos as macos_adapter
from runtime.cron.base import CronHandle
from runtime.health import aggregate as aggregate_health
from runtime.recipe_validator import validate as validate_recipe
from runtime.services.base import ServiceHandle


@dataclass
class StageTiming:
    name: str
    ok: bool
    ms: float
    detail: str = ""


@dataclass
class VerifyReport:
    skill_slug: str
    ok: bool
    total_ms: float
    stages: list[StageTiming] = field(default_factory=list)
    incompat: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill_slug": self.skill_slug,
            "ok": self.ok,
            "total_ms": self.total_ms,
            "stages": [asdict(s) for s in self.stages],
            "incompat": self.incompat,
        }


def _adapter_for(os_name: str):
    if os_name == "macos":
        return macos_adapter
    return linux_adapter  # default linux for tests on the worker box


def _now_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


def verify(recipe_path: Path, *, skill_slug: str | None = None,
           dry_run: bool = True, fp: probe_mod.HostFingerprint | None = None,
           _now=time.monotonic) -> VerifyReport:
    slug = skill_slug or recipe_path.parent.name
    started_total = _now()
    stages: list[StageTiming] = []
    incompat_payload: dict[str, Any] | None = None

    # Stage 1 — schema validate
    s1 = _now()
    yaml_text = recipe_path.read_text(encoding="utf-8")
    val = validate_recipe(yaml_text)
    stages.append(StageTiming(name="validate", ok=val["ok"], ms=_now_ms(s1),
                              detail="; ".join(val["errors"])))
    if not val["ok"]:
        return VerifyReport(skill_slug=slug, ok=False,
                            total_ms=_now_ms(started_total), stages=stages)

    import yaml
    doc = yaml.safe_load(yaml_text)
    runtime_block = doc.get("runtime") or {}

    # Stage 2 — probe + compat
    s2 = _now()
    fp = fp or probe_mod.load_or_probe()
    decision = probe_mod.compare(fp, runtime_block.get("compatibility") or {})
    stages.append(StageTiming(name="probe", ok=decision.decision != "hard_incompat",
                              ms=_now_ms(s2), detail=decision.decision))
    if decision.decision == "hard_incompat":
        incompat_payload = decision.as_dict()
        return VerifyReport(skill_slug=slug, ok=False,
                            total_ms=_now_ms(started_total),
                            stages=stages, incompat=incompat_payload)

    # Stage 3 — adapter resolve (no install on dry_run)
    s3 = _now()
    adapter = _adapter_for(fp.os)
    binary_specs = list(runtime_block.get("binaries") or [])
    plans = []
    for b in binary_specs:
        b = {**b, "_skill_slug": slug}
        plans.append(adapter.resolve(b))
    stages.append(StageTiming(name="resolve_binaries", ok=True, ms=_now_ms(s3),
                              detail=f"{len(plans)} plan(s)"))

    # Stage 4 — service provision (skipped in dry_run; we just construct handles)
    s4 = _now()
    service_handles: list[ServiceHandle] = []
    for s in runtime_block.get("services") or []:
        service_handles.append(ServiceHandle(
            name=s["name"], backend=s["type"],
            workdir=str(Path.home() / ".recipes" / "runtime" / slug),
            health_url=s.get("health"),
            extra={"compose": s.get("compose"), "port": s.get("port")},
        ))
    stages.append(StageTiming(name="provision_services", ok=True, ms=_now_ms(s4),
                              detail=f"{len(service_handles)} handle(s) (dry_run)"))

    # Stage 5 — cron register (dry_run constructs handles only)
    s5 = _now()
    cron_handles: list[CronHandle] = []
    for c in runtime_block.get("cron") or []:
        cron_handles.append(CronHandle(name=c["name"], backend="dry-run",
                                       schedule=c["schedule"], cmd=c["cmd"]))
    stages.append(StageTiming(name="register_crons", ok=True, ms=_now_ms(s5),
                              detail=f"{len(cron_handles)} cron(s) (dry_run)"))

    # Stage 6 — aggregate health (skipped in dry_run since handles are pretend)
    s6 = _now()
    if not dry_run:
        report = aggregate_health(service_handles, binary_specs, cron_handles)
        ok = report.ok
        detail = f"{sum(1 for c in report.checks if c.status == 'ok')}/{len(report.checks)} ok"
    else:
        ok = True
        detail = "skipped (dry_run)"
    stages.append(StageTiming(name="health", ok=ok, ms=_now_ms(s6), detail=detail))

    overall_ok = all(s.ok for s in stages)
    return VerifyReport(skill_slug=slug, ok=overall_ok,
                        total_ms=_now_ms(started_total),
                        stages=stages, incompat=incompat_payload)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="verify_install",
                                 description="F-VERIFY: end-to-end install-verification.")
    ap.add_argument("slug", help="recipe slug under recipes/<slug>/")
    ap.add_argument("--catalog", type=Path,
                    default=Path(__file__).resolve().parents[1] / "recipes")
    ap.add_argument("--no-dry-run", action="store_true",
                    help="Actually run the installer (NOT for CI workers).")
    args = ap.parse_args(argv)

    recipe = args.catalog / args.slug / "recipe.yaml"
    if not recipe.exists():
        print(f"recipe.yaml not found: {recipe}", file=sys.stderr)
        return 2

    report = verify(recipe, skill_slug=args.slug, dry_run=not args.no_dry_run)
    print(json.dumps(report.as_dict(), indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
