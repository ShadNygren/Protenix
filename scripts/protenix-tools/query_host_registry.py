#!/usr/bin/env python3
"""Query the host registry on R2 and report Linux vs Windows statistics.

The host quality gate logs every host attempt (pass or fail) to:
  s3://vh-protenix-training/host_registry/<timestamp>_<container_id>_<status>.json

This script downloads all records and reports:
  - Total attempts
  - Pass/fail ratio
  - Rejection reasons breakdown
  - CPU model distribution
  - Geographic distribution

Usage:
    python3 query_host_registry.py
    python3 query_host_registry.py --since 2026-05-16
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


def list_registry_objects(since: str | None = None) -> list[str]:
    ep = os.environ.get("CLOUDFLARE_R2_ENDPOINT", "")
    key_id = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", "")
    secret = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "")

    if not all([ep, key_id, secret]):
        print("ERROR: R2 credentials not set. Need CLOUDFLARE_R2_* env vars.", file=sys.stderr)
        sys.exit(1)

    cmd = [
        "aws", "--endpoint-url", ep, "--region", "auto",
        "s3", "ls", "s3://vh-protenix-training/host_registry/",
        "--no-paginate",
    ]
    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = key_id
    env["AWS_SECRET_ACCESS_KEY"] = secret

    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        print(f"ERROR: aws s3 ls failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    objects = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            key = parts[-1]
            if since:
                if key >= since:
                    objects.append(key)
            else:
                objects.append(key)

    return objects


def download_records(objects: list[str]) -> list[dict]:
    ep = os.environ.get("CLOUDFLARE_R2_ENDPOINT", "")
    key_id = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID", "")
    secret = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "")

    records = []
    tmp_dir = Path("/tmp/host_registry")
    tmp_dir.mkdir(exist_ok=True)

    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = key_id
    env["AWS_SECRET_ACCESS_KEY"] = secret

    for obj in objects:
        local_path = tmp_dir / obj
        if not local_path.exists():
            cmd = [
                "aws", "--endpoint-url", ep, "--region", "auto",
                "s3", "cp",
                f"s3://vh-protenix-training/host_registry/{obj}",
                str(local_path),
                "--quiet",
            ]
            subprocess.run(cmd, capture_output=True, env=env)

        try:
            data = json.loads(local_path.read_text())
            records.append(data)
        except Exception:
            pass

    return records


def report(records: list[dict]) -> None:
    if not records:
        print("No host registry records found.")
        return

    total = len(records)
    passed = [r for r in records if r.get("status") == "passed"]
    rejected = [r for r in records if r.get("status") == "rejected"]

    print(f"{'='*60}")
    print(f"HOST REGISTRY REPORT — {total} total attempts")
    print(f"{'='*60}")
    print()
    print(f"  PASSED:   {len(passed):3d} ({100*len(passed)/total:.0f}%)")
    print(f"  REJECTED: {len(rejected):3d} ({100*len(rejected)/total:.0f}%)")
    print()

    # Rejection reasons
    if rejected:
        print("REJECTION REASONS:")
        reasons = Counter()
        for r in rejected:
            reason = r.get("reason", "unknown")
            if "WSL2" in reason or "microsoft" in reason.lower():
                reasons["WSL2 / Windows host"] += 1
            elif "clock locked" in reason.lower():
                reasons["CPU clock locked (VM)"] += 1
            elif "too slow" in reason.lower():
                reasons["CPU benchmark too slow"] += 1
            elif "bandwidth" in reason.lower():
                reasons["Memory bandwidth too low"] += 1
            elif "PCIe" in reason.lower():
                reasons["PCIe link too narrow"] += 1
            else:
                reasons[reason[:50]] += 1

        for reason, count in reasons.most_common():
            print(f"  {count:3d}  {reason}")
        print()

    # CPU models
    print("CPU MODEL DISTRIBUTION:")
    cpu_models = Counter()
    for r in records:
        model = r.get("cpu_model", "unknown")
        if model:
            cpu_models[model] += 1
    for model, count in cpu_models.most_common(10):
        status_tag = ""
        matching = [r for r in records if r.get("cpu_model") == model]
        pass_count = sum(1 for r in matching if r.get("status") == "passed")
        status_tag = f" ({pass_count}/{count} passed)"
        print(f"  {count:3d}  {model}{status_tag}")
    print()

    # Kernel distribution (Linux vs WSL2)
    print("KERNEL DISTRIBUTION:")
    kernels = Counter()
    for r in records:
        kernel = r.get("kernel", "unknown")
        if "microsoft" in kernel.lower() or "wsl" in kernel.lower():
            kernels["Windows (WSL2)"] += 1
        elif "linux" in kernel.lower() or kernel.startswith(("5.", "6.")):
            kernels["Native Linux"] += 1
        else:
            kernels[kernel[:30]] += 1
    for kernel, count in kernels.most_common():
        print(f"  {count:3d} ({100*count/total:.0f}%)  {kernel}")
    print()

    # Performance of passed hosts
    if passed:
        print("PASSED HOST PERFORMANCE:")
        gflops_vals = []
        for r in passed:
            details = r.get("details", {})
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except Exception:
                    details = {}
            gf = details.get("gflops")
            if gf:
                try:
                    gflops_vals.append(float(gf))
                except (ValueError, TypeError):
                    pass

        if gflops_vals:
            print(f"  CPU GFLOPS: min={min(gflops_vals):.0f}, median={sorted(gflops_vals)[len(gflops_vals)//2]:.0f}, max={max(gflops_vals):.0f}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", type=str, default=None,
                        help="Only include records from this date (YYYY-MM-DD)")
    args = parser.parse_args()

    objects = list_registry_objects(since=args.since)
    print(f"Found {len(objects)} host registry records on R2", file=sys.stderr)

    if not objects:
        print("No records found. The host quality gate has not uploaded any data yet.")
        return 0

    records = download_records(objects)
    report(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
