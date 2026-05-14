"""1-Hz RAM usage monitor — counterpart to vram_monitor.sh.

Samples system + per-process memory every 1 second and writes a CSV log so we
can compute peak RSS, identify the process responsible, and correlate RAM spikes
to training steps / checkpoint saves.

What we capture each second:
  * t                        wall time (UTC ISO8601)
  * mem_total_mb             /proc/meminfo MemTotal
  * mem_available_mb         /proc/meminfo MemAvailable (kernel's "free + reclaimable" estimate)
  * mem_used_mb              total - available
  * cached_mb                /proc/meminfo Cached (page cache, evictable under pressure)
  * shmem_mb                 /proc/meminfo Shmem (tmpfs + SysV shm + anon shared mappings)
  * swap_used_mb             /proc/meminfo SwapTotal - SwapFree
  * shm_size_mb              df /dev/shm total
  * shm_used_mb              df /dev/shm used
  * shm_avail_mb             df /dev/shm available
  * cgroup_limit_mb          /sys/fs/cgroup/memory.max (cgroup v2) or memory.limit_in_bytes (v1)
  * cgroup_current_mb        /sys/fs/cgroup/memory.current (v2) or memory.usage_in_bytes (v1)
  * cgroup_peak_mb           /sys/fs/cgroup/memory.peak when available (v2 kernel ≥ 5.19)
  * top1_rss_mb / top1_pid / top1_cmd   RSS of the largest matching process
  * top2_rss_mb / top2_pid / top2_cmd   second-largest
  * top3_rss_mb / top3_pid / top3_cmd   third-largest
  * train_workers_n          count of processes matching `runner/train.py` (incl. dataloader forks)
  * train_total_rss_mb       sum of RSS across those processes (most useful number)
  * kiddie_pool_mb           train_total_rss + shmem (collision risk metric — see below)

The "kiddie pool" metric: PyTorch DataLoader workers communicate prefetched
batches to the main process via /dev/shm (tmpfs, RAM-backed). The container's
cgroup memory cap covers BOTH process RSS AND tmpfs usage out of the same pool.
If `train_total_rss + shmem` ever approaches `cgroup_limit`, OOM-killer fires
even though no single value looks alarming. Tracking the sum is the right
signal — not either operand alone.

Filter for processes is configurable via --pattern (regex on /proc/<pid>/cmdline).
Default matches Protenix training + watcher.

Per CLAUDE.md "GPU VRAM Monitoring" rule, this is the equivalent for RAM. Always
start it BEFORE launching any training run.

Usage:
    python ram_monitor.py --out /workspace/ram_monitor.csv --pattern 'runner/train.py|checkpoint_watcher'
    # Background:
    nohup python ram_monitor.py --out /workspace/ram_monitor.csv </dev/null >/workspace/ram_monitor.stdout 2>&1 &
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path


def read_shm_usage() -> tuple[int | None, int | None, int | None]:
    """Return (size_bytes, used_bytes, avail_bytes) for /dev/shm, or all None.

    Uses shutil.disk_usage which calls statvfs under the hood. The tmpfs at
    /dev/shm is RAM-backed so its "used" is real RAM committed to shared
    memory (separate accounting from process RSS).
    """
    try:
        usage = shutil.disk_usage("/dev/shm")
        return usage.total, usage.used, usage.free
    except (FileNotFoundError, OSError):
        return None, None, None


def read_meminfo() -> dict[str, int]:
    """Return key /proc/meminfo values in KB."""
    out: dict[str, int] = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(":"):
                key = parts[0].rstrip(":")
                try:
                    out[key] = int(parts[1])
                except ValueError:
                    pass
    return out


def read_cgroup_memory() -> tuple[int | None, int | None, int | None]:
    """Return (limit_bytes, current_bytes, peak_bytes) from cgroup v1 or v2.

    Any value returns None if not available.
    """
    # cgroup v2 (unified)
    base_v2 = Path("/sys/fs/cgroup")
    if (base_v2 / "memory.max").is_file():
        def _read(name: str) -> int | None:
            p = base_v2 / name
            if not p.is_file():
                return None
            try:
                val = p.read_text().strip()
                if val == "max":
                    return None
                return int(val)
            except (ValueError, OSError):
                return None
        return _read("memory.max"), _read("memory.current"), _read("memory.peak")

    # cgroup v1 (memory subsystem)
    base_v1 = Path("/sys/fs/cgroup/memory")
    if (base_v1 / "memory.limit_in_bytes").is_file():
        def _read1(name: str) -> int | None:
            p = base_v1 / name
            if not p.is_file():
                return None
            try:
                return int(p.read_text().strip())
            except (ValueError, OSError):
                return None
        return _read1("memory.limit_in_bytes"), _read1("memory.usage_in_bytes"), _read1("memory.max_usage_in_bytes")

    return None, None, None


def read_proc_stats(pattern: re.Pattern) -> list[tuple[int, int, str]]:
    """Return list of (pid, rss_kb, cmdline) for processes matching pattern."""
    out: list[tuple[int, int, str]] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            cmdline_raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            cmdline = cmdline_raw.replace(b"\x00", b" ").decode(errors="replace").strip()
            if not pattern.search(cmdline):
                continue
            # VmRSS from /proc/<pid>/status
            status_text = Path(f"/proc/{pid}/status").read_text()
            rss_kb = 0
            for line in status_text.splitlines():
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break
            out.append((pid, rss_kb, cmdline[:120]))
        except (FileNotFoundError, PermissionError, OSError):
            # Process may have exited mid-scan; ignore
            continue
    return out


def kb_to_mb(kb: int | None) -> float | None:
    if kb is None:
        return None
    return round(kb / 1024, 1)


def bytes_to_mb(b: int | None) -> float | None:
    if b is None:
        return None
    return round(b / 1024 / 1024, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True,
                    help="CSV output path")
    ap.add_argument("--pattern",
                    default=r"runner/train\.py|checkpoint_watcher\.py|nohup.*train",
                    help="Regex matched against /proc/<pid>/cmdline")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="Sample interval in seconds (default: 1.0)")
    ap.add_argument("--top-n", type=int, default=3,
                    help="Track top N largest matching processes individually")
    args = ap.parse_args()

    pattern = re.compile(args.pattern)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    # Write CSV header
    header = [
        "t", "mem_total_mb", "mem_available_mb", "mem_used_mb",
        "cached_mb", "shmem_mb", "swap_used_mb",
        "shm_size_mb", "shm_used_mb", "shm_avail_mb",
        "cgroup_limit_mb", "cgroup_current_mb", "cgroup_peak_mb",
        "train_workers_n", "train_total_rss_mb", "kiddie_pool_mb",
    ]
    for i in range(1, args.top_n + 1):
        header.extend([f"top{i}_rss_mb", f"top{i}_pid", f"top{i}_cmd"])

    # Open file in line-buffered mode so each row hits disk immediately
    fh = open(args.out, "w", buffering=1, newline="")
    writer = csv.writer(fh)
    writer.writerow(header)

    # Trap SIGTERM/SIGINT for clean exit
    stopping = False
    def _stop(signum, frame):  # noqa: ARG001
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(f"[ram_monitor] sampling every {args.interval}s -> {args.out}", flush=True)
    print(f"[ram_monitor] process filter: {pattern.pattern}", flush=True)

    while not stopping:
        meminfo = read_meminfo()
        mem_total_kb = meminfo.get("MemTotal", 0)
        mem_avail_kb = meminfo.get("MemAvailable", 0)
        cached_kb = meminfo.get("Cached", 0)
        shmem_kb = meminfo.get("Shmem", 0)
        swap_total_kb = meminfo.get("SwapTotal", 0)
        swap_free_kb = meminfo.get("SwapFree", 0)

        shm_size_b, shm_used_b, shm_avail_b = read_shm_usage()
        cg_limit, cg_current, cg_peak = read_cgroup_memory()

        procs = read_proc_stats(pattern)
        procs.sort(key=lambda x: x[1], reverse=True)
        total_rss_kb = sum(p[1] for p in procs)

        # Kiddie-pool metric: process RSS + shared memory both compete for the
        # cgroup cap. If sum approaches cgroup_limit, OOM-killer fires.
        kiddie_pool_kb = total_rss_kb + shmem_kb

        row = [
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            kb_to_mb(mem_total_kb),
            kb_to_mb(mem_avail_kb),
            kb_to_mb(mem_total_kb - mem_avail_kb),
            kb_to_mb(cached_kb),
            kb_to_mb(shmem_kb),
            kb_to_mb(swap_total_kb - swap_free_kb),
            bytes_to_mb(shm_size_b),
            bytes_to_mb(shm_used_b),
            bytes_to_mb(shm_avail_b),
            bytes_to_mb(cg_limit),
            bytes_to_mb(cg_current),
            bytes_to_mb(cg_peak),
            len(procs),
            kb_to_mb(total_rss_kb),
            kb_to_mb(kiddie_pool_kb),
        ]
        for i in range(args.top_n):
            if i < len(procs):
                pid, rss_kb, cmd = procs[i]
                row.extend([kb_to_mb(rss_kb), pid, cmd])
            else:
                row.extend(["", "", ""])
        writer.writerow(row)

        time.sleep(args.interval)

    fh.close()
    print(f"[ram_monitor] stopped, wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
