#!/usr/bin/env python
"""
scripts/certify.py — AEOS certification harness CLI.

Run the SAME command anywhere; the environment decides what's legitimate:

    python scripts/certify.py bronze            # dev-scale operational validation
    python scripts/certify.py silver
    python scripts/certify.py all               # every tier, sequentially
    python scripts/certify.py bronze --allow-full-scale   # attempt real cert
                                                          # (production infra only)

On a developer workstation this always produces a dev-scale *operational
validation* (certified=False), with real measurements. On a production-grade
host (Linux server / Kubernetes / cloud) with --allow-full-scale it runs the
full-scale load and may emit certified=True if every threshold is met.

Outputs, per run, into --output-dir (default: reports/certification/):
  * <run_id>.json  — machine-readable record (every stat + the gating decision)
  * <run_id>.md    — human-readable certification / validation report

Exit code is 0 when every requested tier produced a report; it does NOT fail on
a not-certified dev-scale run (that is the expected, honest outcome locally).
Use --require-certified to make the process exit non-zero unless every tier was
actually certified (for CI on production infra).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure the repo root is importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to cp1252; make stdout tolerant of any UTF-8 output.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

from app.certification import (  # noqa: E402
    CertificationHarness, Tier, classify, write_json, write_markdown,
)

_TIERS = [Tier.BRONZE, Tier.SILVER, Tier.GOLD, Tier.PLATINUM]


def _parse_tiers(arg: str) -> list[Tier]:
    if arg.lower() == "all":
        return list(_TIERS)
    try:
        return [Tier(arg.lower())]
    except ValueError:
        raise SystemExit(f"unknown tier {arg!r}; choose from "
                         f"{[t.value for t in _TIERS]} or 'all'")


async def _run(args) -> int:
    env = classify()
    harness = CertificationHarness(allow_full_scale=args.allow_full_scale,
                                   environment=env)

    print(f"AEOS certification harness")
    print(f"  environment : {env.environment_class.value} "
          f"({env.os_name} {env.os_release}, {env.cpu_count} CPUs)")
    print(f"  production-grade: {env.is_production_grade}  "
          f"allow_full_scale: {args.allow_full_scale}")
    print(f"  output      : {args.output_dir}")
    print()

    all_certified = True
    for tier in _parse_tiers(args.tier):
        print(f">> running {tier.value} ...", flush=True)
        result = await harness.run_tier(tier)
        json_path = write_json(result, args.output_dir)
        md_path = write_markdown(result, args.output_dir)

        verdict = "CERTIFIED" if result.certified else "NOT CERTIFIED (dev-scale)"
        tput = result.measurements["throughput_latency"]
        fo = result.measurements["failover"]
        fed = result.measurements["federation_overhead"]
        print(f"   verdict     : {verdict}")
        print(f"   throughput  : {tput['scalar'].get('throughput_tps', 0):.1f} tps "
              f"(P99 {tput['stats'].get('p99', 0)} ms)")
        print(f"   failover    : P99 {fo['stats'].get('p99', 0)} ms")
        print(f"   federation  : P50 {fed['stats'].get('p50', 0)} ms")
        print(f"   thresholds  : {'met' if result.thresholds_met else 'NOT met'} "
              f"at {result.scale.label}")
        print(f"   reports     : {json_path.name}, {md_path.name}")
        print()

        all_certified = all_certified and result.certified

    if args.require_certified and not all_certified:
        print("require-certified set and not every tier was certified → exit 1")
        return 1
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="AEOS certification harness")
    p.add_argument("tier", help="bronze | silver | gold | platinum | all")
    p.add_argument("--output-dir", default="reports/certification",
                   help="where to write JSON + markdown reports")
    p.add_argument("--allow-full-scale", action="store_true",
                   help="attempt a full-scale run (only meaningful on "
                        "production-grade infra; ignored elsewhere)")
    p.add_argument("--require-certified", action="store_true",
                   help="exit non-zero unless every requested tier was certified")
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
