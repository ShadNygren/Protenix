#!/usr/bin/env python3
"""Real-time OHLC aggregation daemon for Protenix training logs.

Tails a training log (stdout file), parses per-step metrics, and writes
1-minute OHLC candles to a CSV — like stock-market candlestick charts but
for training loss.

Each row = one completed minute. Columns per tracked metric:
  <metric>_open, <metric>_high, <metric>_low, <metric>_close, <metric>_median

Plus timing columns:
  minute_start, step_first, step_last, step_count, step_time_avg_s

Plus provenance:
  run_name (identifies which training run produced this candle)

Crash-resilient: persists file_pos to --state-file so restarts resume from
where they left off instead of re-parsing the entire log (which produces
duplicate candles). Touches a --heartbeat file on every candle write so
chain_runner can verify this process is alive AND producing output.

Usage:
    python3 training_monitor.py --log /path/to/run.stdout --out /path/to/ohlc.csv

    # With crash recovery and heartbeat:
    python3 training_monitor.py \
        --log /path/to/run.stdout \
        --out /path/to/ohlc.csv \
        --run-name run001_idp_seed1_step5000_20260519T223513Z \
        --heartbeat /path/to/ohlc.heartbeat \
        --state-file /path/to/monitor.state
"""

from __future__ import annotations

VERSION = "2.0.0-20260528"

import argparse
import csv
import hashlib
import json
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
    cols = ["minute_start", "run_name", "step_first", "step_last", "step_count",
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
    def __init__(self, minute_start: str, run_name: str):
        self.minute_start = minute_start
        self.run_name = run_name
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
            "run_name": self.run_name,
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


def load_state(state_file: Path | None) -> int:
    if state_file and state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            pos = state.get("file_pos", 0)
            print(f"[training_monitor] resuming from file_pos={pos} "
                  f"(state file: {state_file})", flush=True)
            return pos
        except Exception as e:
            print(f"[training_monitor] state file corrupt, starting from 0: {e}",
                  flush=True)
    return 0


def save_state(state_file: Path | None, file_pos: int, steps_written: int) -> None:
    if state_file:
        state = {"file_pos": file_pos, "steps_written": steps_written,
                 "updated_at": datetime.now(timezone.utc).isoformat()}
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.rename(state_file)


def touch_heartbeat(heartbeat_path: Path | None) -> None:
    if heartbeat_path:
        heartbeat_path.touch()


def tail_log(log_path: Path, out_path: Path, poll_interval: float,
             run_name: str, heartbeat_path: Path | None,
             state_file: Path | None):
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
    print(f"[training_monitor] run_name: {run_name}", flush=True)
    print(f"[training_monitor] poll interval: {poll_interval}s", flush=True)
    if heartbeat_path:
        print(f"[training_monitor] heartbeat: {heartbeat_path}", flush=True)
    if state_file:
        print(f"[training_monitor] state file: {state_file}", flush=True)

    current_candle: MinuteCandle | None = None
    file_pos = load_state(state_file)
    partial_line = ""
    total_steps_written = 0
    last_known_wall_time: float | None = None

    # Touch heartbeat on startup to signal we're alive
    touch_heartbeat(heartbeat_path)

    while not SHUTDOWN:
        if not log_path.exists():
            time.sleep(poll_interval)
            continue

        try:
            file_size = log_path.stat().st_size
        except OSError:
            time.sleep(poll_interval)
            continue

        # Handle log file truncation (training restarted and overwrote file)
        if file_size < file_pos:
            print(f"[training_monitor] log file truncated "
                  f"(size={file_size} < pos={file_pos}), resetting to 0",
                  flush=True)
            file_pos = 0
            partial_line = ""

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
                    total_steps_written += len(current_candle.steps)
                    _print_candle(current_candle)
                    touch_heartbeat(heartbeat_path)
                    save_state(state_file, file_pos, total_steps_written)
                    current_candle = None
            time.sleep(poll_interval)
            continue

        text = partial_line + raw
        lines = text.split("\n")
        partial_line = lines.pop()

        for line in lines:
            parsed = parse_step_line(line)
            if parsed is None:
                # Still try to extract wall time for the last_known_wall_time
                wt = parse_wall_time(line)
                if wt is not None:
                    last_known_wall_time = wt
                continue
            step, metrics = parsed

            wall = parse_wall_time(line)
            if wall is not None:
                last_known_wall_time = wall
            elif last_known_wall_time is not None:
                # Use last known wall time + small offset instead of time.time()
                wall = last_known_wall_time + 0.1
            else:
                # Only use time.time() as absolute last resort on first line
                wall = time.time()

            now_min = minute_key(datetime.fromtimestamp(wall, tz=timezone.utc))

            if current_candle is None:
                current_candle = MinuteCandle(now_min, run_name)
            elif now_min != current_candle.minute_start:
                writer.writerow(current_candle.to_row())
                out_fh.flush()
                total_steps_written += len(current_candle.steps)
                _print_candle(current_candle)
                touch_heartbeat(heartbeat_path)
                save_state(state_file, file_pos, total_steps_written)
                current_candle = MinuteCandle(now_min, run_name)

            current_candle.add_step(step, metrics, wall)

    # Flush final candle on shutdown
    if current_candle and current_candle.steps:
        writer.writerow(current_candle.to_row())
        out_fh.flush()
        total_steps_written += len(current_candle.steps)
        _print_candle(current_candle, final=True)
        touch_heartbeat(heartbeat_path)
        save_state(state_file, file_pos, total_steps_written)

    out_fh.close()
    print(f"[training_monitor] shutdown complete, {total_steps_written} total steps written",
          flush=True)


def _print_candle(candle: MinuteCandle, final: bool = False) -> None:
    loss_vals = candle.buckets["loss.avg"].values
    prefix = "final candle" if final else "candle"
    loss_str = f", loss close={loss_vals[-1]:.4f}" if loss_vals else ""
    print(f"[training_monitor] {prefix} {candle.minute_start}: "
          f"{len(candle.steps)} steps{loss_str}", flush=True)


def main() -> int:
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Version banner
    _me = __file__
    _sha = hashlib.sha256(open(_me, "rb").read()).hexdigest()[:16]
    print(f"  training_monitor.py  v{VERSION}  sha256:{_sha}", flush=True)

    ap = argparse.ArgumentParser(
        description="OHLC aggregation daemon for Protenix training logs")
    ap.add_argument("--log", type=Path, required=True,
                    help="Path to training stdout/log file to tail")
    ap.add_argument("--out", type=Path, required=True,
                    help="Path to write OHLC CSV output")
    ap.add_argument("--poll-interval", type=float, default=5.0,
                    help="Seconds between log file polls (default: 5)")
    ap.add_argument("--run-name", type=str, default="unknown",
                    help="Run name written to every OHLC row for provenance")
    ap.add_argument("--heartbeat", type=Path, default=None,
                    help="File touched on every candle write (chain_runner checks freshness)")
    ap.add_argument("--state-file", type=Path, default=None,
                    help="Persists file_pos for crash-resilient restart")
    args = ap.parse_args()

    tail_log(args.log, args.out, args.poll_interval, args.run_name,
             args.heartbeat, args.state_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
