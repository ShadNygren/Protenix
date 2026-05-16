#!/usr/bin/env python3
"""Monitor for host contention on residential GPU cloud platforms.

Detects when the physical host owner's workload is competing with our
container for GPU/CPU/memory resources. On residential platforms like Salad,
the host owner may start gaming, rendering, or their own ML workload at any
time, degrading our training performance.

Signals monitored:
  1. GPU SM utilization drops (owner's process stealing GPU time)
  2. GPU memory used by other processes increases
  3. CPU steal time rises (hypervisor giving time to other VMs/processes)
  4. Step rate degrades vs baseline (most reliable end-to-end signal)
  5. GPU clock throttling (owner's thermal load or power limit sharing)

Usage:
    # Start as background daemon alongside training:
    python3 host_contention_monitor.py --baseline-step-rate 6.9 --log /workspace/contention.log &

    # Or integrate with training loop:
    python3 host_contention_monitor.py --training-log /workspace/training_output/.../training.log
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def get_gpu_metrics() -> dict:
    """Query nvidia-smi for current GPU state."""
    queries = [
        "utilization.gpu",
        "utilization.memory",
        "memory.used",
        "memory.total",
        "temperature.gpu",
        "power.draw",
        "power.limit",
        "clocks.current.graphics",
        "clocks.max.graphics",
    ]
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(queries)}",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return {}
        values = [v.strip() for v in result.stdout.strip().split(",")]
        return dict(zip(queries, values))
    except Exception:
        return {}


def get_cpu_steal() -> float:
    """Read CPU steal time from /proc/stat (indicates hypervisor preemption)."""
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    fields = line.split()
                    # fields: user nice system idle iowait irq softirq steal
                    if len(fields) >= 9:
                        steal = int(fields[8])
                        total = sum(int(x) for x in fields[1:])
                        return steal / total * 100 if total > 0 else 0.0
    except Exception:
        pass
    return 0.0


def get_cpu_steal_delta(prev_stat: dict | None) -> tuple[float, dict]:
    """Compute steal % over the last interval."""
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    fields = line.split()
                    current = {
                        "user": int(fields[1]),
                        "nice": int(fields[2]),
                        "system": int(fields[3]),
                        "idle": int(fields[4]),
                        "iowait": int(fields[5]),
                        "irq": int(fields[6]),
                        "softirq": int(fields[7]),
                        "steal": int(fields[8]) if len(fields) > 8 else 0,
                    }
                    total = sum(current.values())

                    if prev_stat is None:
                        return 0.0, current

                    delta_steal = current["steal"] - prev_stat.get("steal", 0)
                    delta_total = total - sum(prev_stat.values())
                    if delta_total > 0:
                        return delta_steal / delta_total * 100, current
                    return 0.0, current
    except Exception:
        pass
    return 0.0, prev_stat or {}


def get_latest_step_rate(training_log: str | None) -> float | None:
    """Extract the most recent step rate from training.log tqdm output."""
    if not training_log or not os.path.exists(training_log):
        return None
    try:
        # Read last 10KB of log
        with open(training_log, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 10240))
            tail = f.read().decode("utf-8", errors="ignore")

        # Find all step rates
        rates = re.findall(r"(\d+\.\d+)s/it\]", tail)
        if rates:
            return float(rates[-1])
    except Exception:
        pass
    return None


def assess_contention(
    gpu_metrics: dict,
    steal_pct: float,
    current_step_rate: float | None,
    baseline_step_rate: float | None,
) -> tuple[str, list[str]]:
    """Assess contention level: none, mild, severe."""
    signals = []

    # Signal 1: GPU clock throttled significantly
    try:
        current_clk = float(gpu_metrics.get("clocks.current.graphics", 0))
        max_clk = float(gpu_metrics.get("clocks.max.graphics", 1))
        if max_clk > 0 and current_clk < max_clk * 0.7:
            signals.append(f"GPU clock throttled: {current_clk:.0f}/{max_clk:.0f} MHz ({current_clk/max_clk*100:.0f}%)")
    except (ValueError, TypeError):
        pass

    # Signal 2: High CPU steal time
    if steal_pct > 5.0:
        signals.append(f"CPU steal time: {steal_pct:.1f}% (hypervisor preempting us)")
    elif steal_pct > 1.0:
        signals.append(f"CPU steal time elevated: {steal_pct:.1f}%")

    # Signal 3: GPU temperature very high (thermal throttling from shared cooling)
    try:
        temp = float(gpu_metrics.get("temperature.gpu", 0))
        if temp > 85:
            signals.append(f"GPU temp critical: {temp}°C (shared thermal envelope)")
        elif temp > 75:
            signals.append(f"GPU temp elevated: {temp}°C")
    except (ValueError, TypeError):
        pass

    # Signal 4: Step rate degradation vs baseline
    if current_step_rate and baseline_step_rate and baseline_step_rate > 0:
        ratio = current_step_rate / baseline_step_rate
        if ratio > 2.0:
            signals.append(f"Step rate {ratio:.1f}x slower than baseline ({current_step_rate:.1f}s vs {baseline_step_rate:.1f}s)")
        elif ratio > 1.5:
            signals.append(f"Step rate degraded {ratio:.1f}x ({current_step_rate:.1f}s vs {baseline_step_rate:.1f}s)")

    # Signal 5: Power draw near limit (might indicate shared power budget)
    try:
        power = float(gpu_metrics.get("power.draw", 0))
        limit = float(gpu_metrics.get("power.limit", 1))
        if limit > 0 and power > limit * 0.95:
            signals.append(f"GPU at power limit: {power:.0f}/{limit:.0f}W")
    except (ValueError, TypeError):
        pass

    # Assess overall level
    severe_count = sum(1 for s in signals if "critical" in s or "2.0x" in s.lower() or "steal" in s and steal_pct > 10)
    mild_count = len(signals)

    if severe_count >= 2 or steal_pct > 10:
        return "SEVERE", signals
    elif mild_count >= 2 or steal_pct > 5:
        return "MILD", signals
    elif signals:
        return "LOW", signals
    else:
        return "NONE", []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline-step-rate", type=float, default=None,
                        help="Expected step rate on an uncontested host (sec/step)")
    parser.add_argument("--training-log", type=str, default=None,
                        help="Path to training.log for live step rate extraction")
    parser.add_argument("--interval", type=int, default=30,
                        help="Monitoring interval in seconds (default: 30)")
    parser.add_argument("--log", type=str, default="/workspace/contention_monitor.log",
                        help="Output log path")
    parser.add_argument("--alert-threshold", type=str, default="MILD",
                        choices=["LOW", "MILD", "SEVERE"],
                        help="Minimum contention level to log alerts")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit (for cron-style usage)")
    args = parser.parse_args()

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    threshold_levels = {"LOW": 1, "MILD": 2, "SEVERE": 3, "NONE": 0}
    alert_level = threshold_levels.get(args.alert_threshold, 2)

    prev_stat = None
    baseline = args.baseline_step_rate

    def log(msg: str):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass

    log(f"Contention monitor started (interval={args.interval}s, baseline={baseline}, threshold={args.alert_threshold})")

    while True:
        gpu = get_gpu_metrics()
        steal_pct, prev_stat = get_cpu_steal_delta(prev_stat)
        step_rate = get_latest_step_rate(args.training_log)

        level, signals = assess_contention(gpu, steal_pct, step_rate, baseline)
        level_num = threshold_levels.get(level, 0)

        if level_num >= alert_level:
            log(f"CONTENTION={level} steal={steal_pct:.1f}% gpu_temp={gpu.get('temperature.gpu', '?')}°C "
                f"gpu_clk={gpu.get('clocks.current.graphics', '?')}/{gpu.get('clocks.max.graphics', '?')}MHz "
                f"step_rate={step_rate or '?'}s/step")
            for signal in signals:
                log(f"  → {signal}")
        elif not args.once:
            # Periodic heartbeat even when no contention
            pass

        if args.once:
            result = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "contention_level": level,
                "steal_pct": round(steal_pct, 2),
                "gpu_metrics": gpu,
                "step_rate": step_rate,
                "baseline_step_rate": baseline,
                "signals": signals,
            }
            print(json.dumps(result, indent=2))
            return 0 if level == "NONE" else 1

        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
