"""Analyze a ram_monitor.py CSV: peak summary + top-process breakdown at peak +
timing correlation to a training log if provided.

Usage:
    python analyze_ram_monitor.py --csv /workspace/ram_monitor_<run>.csv
    python analyze_ram_monitor.py --csv ... --train-log /workspace/training_output/<run>/training.log
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, required=True)
    ap.add_argument("--train-log", type=Path, default=None,
                    help="Optional training log to correlate peak timing to step events")
    ap.add_argument("--top-n", type=int, default=5)
    args = ap.parse_args()

    if not args.csv.is_file():
        print(f"ERROR: {args.csv} not found", file=sys.stderr)
        return 2

    rows: list[dict[str, str]] = []
    with args.csv.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            rows.append(r)

    if not rows:
        print("ERROR: CSV is empty", file=sys.stderr)
        return 2

    def to_float(v: str) -> float:
        try:
            return float(v) if v else 0.0
        except ValueError:
            return 0.0

    # Peak by sum-of-RSS (most direct measure of training memory)
    rows_with_sum = [(i, to_float(r["train_total_rss_mb"])) for i, r in enumerate(rows)]
    peak_idx_sum, peak_sum = max(rows_with_sum, key=lambda x: x[1])
    peak_row_sum = rows[peak_idx_sum]

    # Peak by system mem_used
    rows_with_sys = [(i, to_float(r["mem_used_mb"])) for i, r in enumerate(rows)]
    peak_idx_sys, peak_sys = max(rows_with_sys, key=lambda x: x[1])
    peak_row_sys = rows[peak_idx_sys]

    print(f"=== RAM monitor analysis: {args.csv} ===")
    print(f"Rows captured: {len(rows)}")
    print(f"Time span: {rows[0]['t']} -> {rows[-1]['t']}")
    print()
    print(f"Total memory: {rows[0]['mem_total_mb']} MB ({float(rows[0]['mem_total_mb'])/1024:.1f} GiB)")
    print()
    print(f"PEAK BY SUM-OF-PROCESS-RSS (sum can over-count COW shared pages):")
    print(f"  time: {peak_row_sum['t']}")
    print(f"  train_total_rss_mb: {peak_sum:.1f}  ({peak_sum/1024:.1f} GiB)")
    print(f"  system mem_used_mb at that moment: {peak_row_sum['mem_used_mb']}")
    print(f"  top processes:")
    for i in range(1, args.top_n + 1):
        rss = peak_row_sum.get(f"top{i}_rss_mb", "")
        pid = peak_row_sum.get(f"top{i}_pid", "")
        cmd = peak_row_sum.get(f"top{i}_cmd", "")
        if rss:
            cmd_short = cmd[:80] if cmd else ""
            print(f"    {i}. pid={pid} rss={rss}MB  {cmd_short}")
    print()
    print(f"PEAK BY SYSTEM MEM USED (mem_total - MemAvailable, includes kernel + caches):")
    print(f"  time: {peak_row_sys['t']}")
    print(f"  mem_used_mb: {peak_sys:.1f}  ({peak_sys/1024:.1f} GiB)")
    print(f"  fraction of total: {peak_sys/float(rows[0]['mem_total_mb'])*100:.1f}%")
    print(f"  train_total_rss_mb at that moment: {peak_row_sys['train_total_rss_mb']}")
    print()

    # cgroup peaks (if available)
    cg_currents = [to_float(r["cgroup_current_mb"]) for r in rows]
    cg_peak = max(cg_currents) if cg_currents else 0
    if cg_peak > 0:
        print(f"CGROUP MEMORY: peak current = {cg_peak:.1f} MB ({cg_peak/1024:.1f} GiB)")
        cg_limit = next((to_float(r["cgroup_limit_mb"]) for r in rows
                          if to_float(r["cgroup_limit_mb"]) > 0), 0)
        if cg_limit:
            print(f"  cgroup limit = {cg_limit:.1f} MB ({cg_limit/1024:.1f} GiB)")
    else:
        print("CGROUP MEMORY: not measurable (no cgroup paths readable / not in a cgroup)")

    # Timing correlation to training log
    if args.train_log and args.train_log.is_file():
        print()
        print(f"=== Correlation to {args.train_log} ===")
        # Look for step markers in the log
        step_re = re.compile(r"\[step (\d+): (\d+)/\d+\]")
        save_re = re.compile(r"(checkpoint|save|saved|Saving)", re.IGNORECASE)
        oom_re = re.compile(r"(Killed|OOM|out of memory|MemoryError|killed by signal)", re.IGNORECASE)
        step_events: list[tuple[str, str, str]] = []  # (timestamp, kind, text)
        ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        last_ts = ""
        with args.train_log.open() as fh:
            for line in fh:
                line = line.rstrip()
                m_ts = ts_re.match(line)
                if m_ts:
                    last_ts = m_ts.group(1)
                if step_re.search(line):
                    step_events.append((last_ts, "step", line[:200]))
                elif save_re.search(line):
                    step_events.append((last_ts, "save", line[:200]))
                elif oom_re.search(line):
                    step_events.append((last_ts, "oom", line[:200]))
        for ts, kind, text in step_events[-15:]:
            print(f"  [{ts}] {kind:5s}  {text[:150]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
