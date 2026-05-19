#!/usr/bin/env python3
"""Real-time OHLC aggregation daemon for Protenix training logs.

Tails a training log (stdout file), parses per-step metrics, and writes
1-minute OHLC candles to a CSV — like stock-market candlestick charts but
for training loss.

Each row = one completed minute. Columns per tracked metric:
  <metric>_open, <metric>_high, <metric>_low, <metric>_close, <metric>_median

Plus timing columns:
  minute_start, step_first, step_last, step_count, step_time_avg_s

Designed to run as a daemon alongside ram_monitor.py and vram_monitor.sh.
The sidecar_log_mirror.sh picks up the output CSV and mirrors it to R2.

Usage:
    python3 training_monitor.py --log /path/to/run.stdout --out /path/to/ohlc.csv

    # As a background daemon:
    nohup python3 training_monitor.py \
        --log /workspace/logs/training_output/run001/training.log \
        --out /workspace/logs/training_ohlc.csv \
        </dev/null >/workspace/logs/training_monitor.stdout 2>&1 &
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import signal
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STEP_RE = re.compile(r"Step (\d+) train metrics:\s*(\{.*\})")
FIELD_RE = re.compile(r"'train/([^']+)':\s*np\.float64\(([0-9.eE+-]+)\)")
TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")

TRACKED_METRICS = [
    "loss.avg",
    "distogram_loss.avg",
    "pae_loss.avg",
    "plddt_loss.avg",
    "smooth_lddt_loss.avg",
    "mse_loss.avg",
]

OHLC_SUFFIXES = ["open", "high", "low", "close", "median"]

SHUTDOWN = False


def handle_signal(signum, frame):
    global SHUTDOWN
    SHUTDOWN = True


def build_header() -> list[str]:
    cols = ["minute_start", "step_first", "step_last", "step_count",
            "step_time_avg_s"]
    for m in TRACKED_METRICS:
        short = m.replace(".avg", "")
        for s in OHLC_SUFFIXES:
            cols.append(f"{short}_{s}")
    return cols


def minute_key(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:00Z")


def minute_key_now() -> str:
    return minute_key(datetime.now(timezone.utc))


class OHLCBucket:
    """Accumulates values for one metric within a single minute."""
    __slots__ = ("values",)

    def __init__(self):
        self.values: list[float] = []

    def add(self, v: float):
        self.values.append(v)

    def ohlc(self) -> dict[str, float | None]:
        if not self.values:
            return {s: None for s in OHLC_SUFFIXES}
        return {
            "open": self.values[0],
            "high": max(self.values),
            "low": min(self.values),
            "close": self.values[-1],
            "median": statistics.median(self.values),
        }


class MinuteCandle:
    """Collects all metrics for a single minute window."""

    def __init__(self, minute_start: str):
        self.minute_start = minute_start
        self.buckets: dict[str, OHLCBucket] = {m: OHLCBucket() for m in TRACKED_METRICS}
        self.steps: list[int] = []
        self.step_times: list[float] = []
        self._last_step_ts: float | None = None

    def add_step(self, step: int, metrics: dict[str, float], wall_time: float):
        self.steps.append(step)
        if self._last_step_ts is not None:
            dt = wall_time - self._last_step_ts
            if 0 < dt < 300:
                self.step_times.append(dt)
        self._last_step_ts = wall_time
        for m in TRACKED_METRICS:
            if m in metrics:
                self.buckets[m].add(metrics[m])

    def to_row(self) -> dict[str, str]:
        row: dict[str, str] = {
            "minute_start": self.minute_start,
            "step_first": str(self.steps[0]) if self.steps else "",
            "step_last": str(self.steps[-1]) if self.steps else "",
            "step_count": str(len(self.steps)),
            "step_time_avg_s": f"{statistics.mean(self.step_times):.2f}" if self.step_times else "",
        }
        for m in TRACKED_METRICS:
            short = m.replace(".avg", "")
            ohlc = self.buckets[m].ohlc()
            for s in OHLC_SUFFIXES:
                v = ohlc[s]
                row[f"{short}_{s}"] = f"{v:.6f}" if v is not None else ""
        return row


def parse_step_line(line: str) -> tuple[int, dict[str, float]] | None:
    m = STEP_RE.search(line)
    if not m:
        return None
    step = int(m.group(1))
    body = m.group(2)
    metrics = {name: float(val) for name, val in FIELD_RE.findall(body)}
    if not metrics:
        return None
    return step, metrics


def parse_wall_time(line: str) -> float | None:
    m = TIMESTAMP_RE.match(line)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def tail_log(log_path: Path, out_path: Path, poll_interval: float):
    """Main loop: tail the log, aggregate into OHLC candles, write CSV."""
    header = build_header()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not out_path.exists() or out_path.stat().st_size == 0
    out_fh = open(out_path, "a", newline="")
    writer = csv.DictWriter(out_fh, fieldnames=header)
    if write_header:
        writer.writeheader()
        out_fh.flush()

    print(f"[training_monitor] output: {out_path}", flush=True)
    print(f"[training_monitor] watching: {log_path}", flush=True)
    print(f"[training_monitor] poll interval: {poll_interval}s", flush=True)

    current_candle: MinuteCandle | None = None
    file_pos = 0
    partial_line = ""

    while not SHUTDOWN:
        if not log_path.exists():
            time.sleep(poll_interval)
            continue

        try:
            with open(log_path, "r", errors="replace") as f:
                f.seek(file_pos)
                raw = f.read()
                file_pos = f.tell()
        except (OSError, IOError):
            time.sleep(poll_interval)
            continue

        if not raw:
            if current_candle and current_candle.steps:
                now_min = minute_key_now()
                if now_min != current_candle.minute_start:
                    writer.writerow(current_candle.to_row())
                    out_fh.flush()
                    print(f"[training_monitor] candle {current_candle.minute_start}: "
                          f"{len(current_candle.steps)} steps, "
                          f"loss close={current_candle.buckets['loss.avg'].values[-1]:.4f}"
                          if current_candle.buckets["loss.avg"].values else
                          f"[training_monitor] candle {current_candle.minute_start}: "
                          f"{len(current_candle.steps)} steps",
                          flush=True)
                    current_candle = None
            time.sleep(poll_interval)
            continue

        text = partial_line + raw
        lines = text.split("\n")
        partial_line = lines.pop()

        for line in lines:
            parsed = parse_step_line(line)
            if parsed is None:
                continue
            step, metrics = parsed

            wall = parse_wall_time(line) or time.time()
            now_min = minute_key(datetime.fromtimestamp(wall, tz=timezone.utc))

            if current_candle is None:
                current_candle = MinuteCandle(now_min)
            elif now_min != current_candle.minute_start:
                writer.writerow(current_candle.to_row())
                out_fh.flush()
                loss_vals = current_candle.buckets["loss.avg"].values
                print(f"[training_monitor] candle {current_candle.minute_start}: "
                      f"{len(current_candle.steps)} steps"
                      + (f", loss close={loss_vals[-1]:.4f}" if loss_vals else ""),
                      flush=True)
                current_candle = MinuteCandle(now_min)

            current_candle.add_step(step, metrics, wall)

    if current_candle and current_candle.steps:
        writer.writerow(current_candle.to_row())
        out_fh.flush()
        print(f"[training_monitor] final candle {current_candle.minute_start}: "
              f"{len(current_candle.steps)} steps", flush=True)

    out_fh.close()
    print("[training_monitor] shutdown complete", flush=True)


def main() -> int:
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    ap = argparse.ArgumentParser(
        description="OHLC aggregation daemon for Protenix training logs")
    ap.add_argument("--log", type=Path, required=True,
                    help="Path to training stdout/log file to tail")
    ap.add_argument("--out", type=Path, required=True,
                    help="Path to write OHLC CSV output")
    ap.add_argument("--poll-interval", type=float, default=5.0,
                    help="Seconds between log file polls (default: 5)")
    args = ap.parse_args()

    tail_log(args.log, args.out, args.poll_interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
