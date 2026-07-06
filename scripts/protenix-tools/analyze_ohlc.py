#!/usr/bin/env python3
"""Analyze OHLC training loss data from training_monitor.py output.

Prints summary statistics and a text-mode sparkline of the loss trend,
broken into segments (e.g., per-run boundaries at 5000-step intervals).

Usage:
    python3 analyze_ohlc.py /path/to/training_ohlc.csv
    python3 analyze_ohlc.py /path/to/training_ohlc.csv --run-steps 5000
    python3 analyze_ohlc.py /path/to/training_ohlc.csv --metric distogram_loss
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path


SPARKLINE_CHARS = " ▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 60) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo if hi != lo else 1.0
    buckets: list[list[float]] = [[] for _ in range(width)]
    for i, v in enumerate(values):
        idx = min(int(i * width / len(values)), width - 1)
        buckets[idx].append(v)
    chars = []
    for bucket in buckets:
        if not bucket:
            chars.append(" ")
        else:
            avg = statistics.mean(bucket)
            level = int((avg - lo) / span * (len(SPARKLINE_CHARS) - 1))
            chars.append(SPARKLINE_CHARS[level])
    return "".join(chars)


def analyze(csv_path: Path, run_steps: int, metric: str):
    rows = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    if not rows:
        print("No data rows found.")
        return

    close_col = f"{metric}_close"
    high_col = f"{metric}_high"
    low_col = f"{metric}_low"
    open_col = f"{metric}_open"
    median_col = f"{metric}_median"

    if close_col not in rows[0]:
        available = [c.replace("_close", "") for c in rows[0] if c.endswith("_close")]
        print(f"Metric '{metric}' not found. Available: {', '.join(available)}")
        return

    runs: dict[int, list[dict]] = {}
    for row in rows:
        step_last = row.get("step_last", "")
        if not step_last:
            continue
        step = int(step_last)
        run_num = ((step - 1) // run_steps) + 1
        runs.setdefault(run_num, []).append(row)

    for run_num in sorted(runs.keys()):
        run_rows = runs[run_num]
        closes = []
        highs = []
        lows = []
        step_times = []
        total_steps = 0

        for r in run_rows:
            v = r.get(close_col, "")
            if v:
                closes.append(float(v))
            h = r.get(high_col, "")
            if h:
                highs.append(float(h))
            l = r.get(low_col, "")
            if l:
                lows.append(float(l))
            sc = r.get("step_count", "")
            if sc:
                total_steps += int(sc)
            st = r.get("step_time_avg_s", "")
            if st:
                step_times.append(float(st))

        if not closes:
            print(f"\n  Run {run_num}: no {metric} data")
            continue

        first_step = run_rows[0].get("step_first", "?")
        last_step = run_rows[-1].get("step_last", "?")
        first_time = run_rows[0].get("minute_start", "?")
        last_time = run_rows[-1].get("minute_start", "?")

        # Divide into quarters
        n = len(closes)
        q1 = closes[:n // 4] if n >= 4 else closes[:1]
        q4 = closes[-(n // 4):] if n >= 4 else closes[-1:]

        avg_step_time = statistics.mean(step_times) if step_times else 0

        print(f"\n{'='*70}")
        print(f"  Run {run_num}  |  Steps {first_step}–{last_step}  |  {total_steps} steps  |  {len(run_rows)} candles")
        print(f"  Time: {first_time} → {last_time}")
        print(f"  Avg step time: {avg_step_time:.2f}s")
        print(f"{'='*70}")
        print(f"  {metric} close:")
        print(f"    First candle:  {closes[0]:.4f}")
        print(f"    Last candle:   {closes[-1]:.4f}")
        print(f"    Q1 mean:       {statistics.mean(q1):.4f}  (candles 1–{len(q1)})")
        print(f"    Q4 mean:       {statistics.mean(q4):.4f}  (candles {n - len(q4) + 1}–{n})")
        print(f"    Overall mean:  {statistics.mean(closes):.4f}")
        print(f"    Min:           {min(closes):.4f}")
        print(f"    Max:           {max(closes):.4f}")
        print(f"    Median:        {statistics.median(closes):.4f}")
        if len(closes) >= 2:
            stdev = statistics.stdev(closes)
            print(f"    Stdev:         {stdev:.4f}")

        improvement = ((statistics.mean(q1) - statistics.mean(q4)) / statistics.mean(q1)) * 100
        print(f"    Q1→Q4 change:  {improvement:+.1f}%")

        print(f"\n  Sparkline (close, left=early, right=late):")
        print(f"    hi={max(closes):.3f} |{sparkline(closes)}|")
        print(f"    lo={min(closes):.3f}")

        if highs and lows:
            volatility = [h - l for h, l in zip(highs, lows)]
            print(f"\n  Volatility (high-low per candle):")
            print(f"    Mean:  {statistics.mean(volatility):.4f}")
            print(f"    Max:   {max(volatility):.4f}")
            print(f"    Min:   {min(volatility):.4f}")

    # Print all-run summary if multiple runs
    if len(runs) > 1:
        all_closes = []
        for run_rows in runs.values():
            for r in run_rows:
                v = r.get(close_col, "")
                if v:
                    all_closes.append(float(v))
        if all_closes:
            print(f"\n{'='*70}")
            print(f"  ALL RUNS combined sparkline ({len(all_closes)} candles):")
            print(f"    hi={max(all_closes):.3f} |{sparkline(all_closes)}|")
            print(f"    lo={min(all_closes):.3f}")


def main():
    ap = argparse.ArgumentParser(description="Analyze OHLC training loss data")
    ap.add_argument("csv_path", type=Path, help="Path to training_ohlc.csv")
    ap.add_argument("--run-steps", type=int, default=5000,
                    help="Steps per run for segmentation (default: 5000)")
    ap.add_argument("--metric", type=str, default="loss",
                    help="Metric to analyze (default: loss)")
    args = ap.parse_args()

    if not args.csv_path.exists():
        print(f"File not found: {args.csv_path}", file=sys.stderr)
        sys.exit(1)

    analyze(args.csv_path, args.run_steps, args.metric)


if __name__ == "__main__":
    main()
