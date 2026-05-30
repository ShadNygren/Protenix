#!/usr/bin/env python3
"""Chain runner for the 147-run Protenix production training schedule.

Orchestrates sequential execution: launch run → wait → detect checkpoint →
pause for watcher upload → launch next run. Handles crash recovery with
resumption seed convention.

Data management:
  - IDP bioassemblies + indices stay permanently on local disk (~3 GB)
  - PDB block data is downloaded from R2 before each PDB run and deleted
    after the run completes and checkpoint is confirmed on R2
  - If IDP data is missing on startup, downloads from R2 automatically

Companion process management (ALL mandatory — training cannot start without them):
  - training_monitor.py is restarted between runs (watches per-run stdout)
  - sidecar_log_mirror.sh is restarted between runs (updated file list)
  - checkpoint_watcher.py is persistent (started once, runs across all runs)
  - Health checks every 60s restart any crashed companions
  - ram_monitor, vram_monitor are persistent (external, optional)

All run-selection logic is delegated to select_next_training_run.py.
This script manages process lifecycle, data staging, and chaining.

Usage:
    # Dry run (print what would happen):
    python3 chain_runner.py --dry-run

    # Run the full chain with companion management:
    python3 chain_runner.py

    # Run without managing training_monitor/sidecar (external launcher handles them):
    python3 chain_runner.py --no-manage-companions

    # Resume after pod restart (auto-detects progress from checkpoints):
    python3 chain_runner.py
"""
from __future__ import annotations

VERSION = "2.3.0-20260529"

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def print_version_banner() -> None:
    me = __file__
    sha = _file_sha256(me)
    print(f"=" * 72, flush=True)
    print(f"  chain_runner.py  v{VERSION}  sha256:{sha}", flush=True)
    print(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}", flush=True)
    print(f"  File: {me}", flush=True)
    print(f"=" * 72, flush=True)

sys.path.insert(0, str(Path(__file__).parent))
from select_next_training_run import (
    build_full_schedule,
    find_latest_boundary_checkpoint,
    _recover_boundary_from_r2,
    get_completed_run_count,
    get_data_paths,
    get_pdb_block_r2_uris,
    get_idp_r2_uris,
    build_launch_command,
    generate_run_name,
    validate_prerequisites,
    STEPS_PER_RUN,
    R2_CAMPAIGN_PREFIX,
    DEFAULT_TRAINING_OUTPUT,
    DEFAULT_BIOASSEMBLY_DIR,
    DEFAULT_IDP_INDEX,
    DEFAULT_IDP_PDB_LIST,
    DEFAULT_PDB_BLOCK_DIR,
    DEFAULT_PDB_STAGING_DIR,
    DEFAULT_BASE_MODEL,
    DEFAULT_PROTENIX_DIR,
)
from stage_training_data import (
    download_object,
    download_and_extract_zip,
)

CHAIN_LOG_DEFAULT = Path("/data/chain_runner.jsonl")
WATCHER_STATE_DEFAULT = Path("/data/checkpoint_watcher_state.json")
MONITOR_JSONL_DEFAULT = Path("/data/training_monitor.jsonl")
DEFAULT_CREDS_FILE = Path("/dev/shm/secure/creds")
LOCK_FILE = Path("/data/chain_runner.lock")

SCRIPTS_DIR = Path(__file__).parent
SIDECAR_INTERVAL = 60
HEALTH_CHECK_INTERVAL = 60
OHLC_STALE_THRESHOLD = 180  # seconds — kill training if OHLC heartbeat older than this
OHLC_STALE_GRACE_STEPS = 50  # don't check OHLC freshness until training has produced this many steps
TRAINING_STDOUT_STALENESS = 120  # seconds — used in multiple places

# SIGTERM flag for graceful shutdown (pod restarts, preemption)
_SIGTERM_RECEIVED = False


def _handle_sigterm(signum, frame):
    global _SIGTERM_RECEIVED
    _SIGTERM_RECEIVED = True
    print(f"\n  SIGTERM received — initiating graceful shutdown...", flush=True)


signal.signal(signal.SIGTERM, _handle_sigterm)


class BlockSkipped(Exception):
    """Raised when a PDB block should be skipped (e.g., 0 training rows)."""
    pass


# ── Logging ──────────────────────────────────────────────────────────────────

def log_event(chain_log: Path, event: dict) -> None:
    event["t"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(chain_log, "a") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")
    _print_event(event)


def _print_event(event: dict) -> None:
    ev = event.get("event", "?")
    run = event.get("run_number", "?")
    ds = event.get("dataset", "")
    seed = event.get("seed", "")
    ts = event.get("t", "")
    extra = ""
    if ev == "run_end":
        dur = event.get("duration_s", 0)
        h, m = divmod(int(dur), 3600)
        m = m // 60
        extra = f" ({h}h {m}m, exit={event.get('exit_code', '?')})"
    elif ev == "run_failed":
        extra = f" (exit={event.get('exit_code', '?')}, reason={event.get('reason', '?')})"
    elif ev == "block_skipped":
        extra = f" (block={event.get('block', '?')}, reason={event.get('reason', '?')})"
    print(f"[{ts}] {ev}: run {run} {ds} seed={seed}{extra}", flush=True)


# ── Checkpoint helpers ───────────────────────────────────────────────────────

def find_boundary_checkpoint(run_info: dict, training_output: str) -> str | None:
    expected_step = run_info["checkpoint_step"]
    # Check both .pt and .pt.age (encrypted by checkpoint_watcher)
    for ext in [".pt", ".pt.age"]:
        pattern = os.path.join(training_output,
                               f"*/checkpoints/{expected_step}{ext}")
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


def find_intermediate_checkpoints(run_info: dict, training_output: str,
                                   prev_boundary: int | None) -> list[tuple[int, str]]:
    target = run_info["checkpoint_step"]
    floor = (prev_boundary or -1) + 1
    intermediates = []
    # Scan both .pt and .pt.age files
    for ext_pattern in ["*.pt", "*.pt.age"]:
        pattern = os.path.join(training_output, f"*/checkpoints/{ext_pattern}")
        for path in glob.glob(pattern):
            basename = os.path.basename(path)
            if "_ema_" in basename:
                continue
            m = re.match(r"^(\d+)\.pt(?:\.age)?$", basename)
            if not m:
                continue
            step = int(m.group(1))
            if floor <= step < target:
                intermediates.append((step, path))
    # Deduplicate (same step may exist as both .pt and .pt.age)
    seen: dict[int, str] = {}
    for step, path in intermediates:
        if step not in seen or not path.endswith(".age"):
            seen[step] = path
    intermediates = sorted(seen.items(), key=lambda x: x[0])
    return intermediates


def compute_resumption_seed(original_seed: int, n: int) -> int:
    return original_seed + n * 10_000_000


def wait_for_watcher(pause_seconds: int, run_info: dict,
                     watcher_state: Path) -> None:
    target_step = run_info["checkpoint_step"]
    print(f"  Waiting up to {pause_seconds}s for checkpoint_watcher to upload "
          f"step {target_step}...", flush=True)
    deadline = time.time() + pause_seconds
    while time.time() < deadline:
        if watcher_state.exists():
            try:
                state = json.loads(watcher_state.read_text())
                uploaded = state.get("uploaded", {})
                for key in uploaded:
                    parts = key.rsplit("/", 1)
                    if len(parts) == 2 and parts[1].isdigit():
                        if int(parts[1]) == target_step:
                            print(f"  Watcher confirmed upload of step {target_step}.",
                                  flush=True)
                            return
            except Exception:
                pass
        time.sleep(10)
    print(f"  WARNING: Watcher upload not confirmed after {pause_seconds}s "
          f"— proceeding to next run. Checkpoint may not be on R2 yet.",
          flush=True)


# ── OHLC verification (hard gate on training progression) ────────────────────

def verify_ohlc_completeness(ohlc_csvs: Path | list[Path], step_lo: int,
                              step_hi: int,
                              run_name: str | None = None) -> tuple[bool, str]:
    """Verify OHLC data covers the full step range for a completed run.
    Returns (is_complete, message). This is a HARD GATE — training MUST NOT
    advance to the next run if this returns False.

    ohlc_csvs: single Path or list of Paths. For resumed runs, pass ALL
    OHLC CSVs covering the run window (original + resume variants) so the
    union of their step coverage is checked.
    """
    import csv as _csv
    if isinstance(ohlc_csvs, Path):
        ohlc_csvs = [ohlc_csvs]

    existing = [p for p in ohlc_csvs if p.exists()]
    if not existing:
        names = ", ".join(str(p) for p in ohlc_csvs)
        return False, f"No OHLC files exist: {names}"

    covered_steps: set[int] = set()
    candle_count = 0
    rows_with_null_loss = 0

    for csv_path in existing:
        with open(csv_path) as f:
            reader = _csv.DictReader(f)
            for row in reader:
                if run_name and row.get("run_name") and row["run_name"] != run_name:
                    continue
                try:
                    first = int(row["step_first"])
                    last = int(row["step_last"])
                except (ValueError, KeyError):
                    continue
                if first < step_lo or last > step_hi:
                    continue
                for s in range(first, last + 1):
                    covered_steps.add(s)
                candle_count += 1

                loss_med = row.get("loss_median", "")
                if not loss_med or loss_med.strip() == "":
                    rows_with_null_loss += 1

    expected_count = step_hi - step_lo + 1
    actual_count = len(covered_steps)
    coverage_pct = (actual_count / expected_count * 100) if expected_count > 0 else 0
    files_msg = f" across {len(existing)} file(s)" if len(existing) > 1 else ""

    if actual_count == 0:
        return False, (f"OHLC has ZERO steps for range {step_lo}-{step_hi} "
                       f"({candle_count} candles found{files_msg})")

    missing = set(range(step_lo, step_hi + 1)) - covered_steps
    if missing:
        first_missing = min(missing)
        return False, (f"OHLC incomplete: {coverage_pct:.1f}% coverage "
                       f"({actual_count}/{expected_count} steps), "
                       f"first missing step: {first_missing}")

    if rows_with_null_loss > 0:
        return False, (f"OHLC has {rows_with_null_loss} candles with null loss values "
                       f"(100% step coverage but data quality failure)")

    return True, (f"OHLC verified: {candle_count} candles, "
                  f"100% step coverage ({actual_count} steps){files_msg}, "
                  f"no null values")


def check_ohlc_freshness(heartbeat_path: Path) -> tuple[bool, float]:
    """Check that OHLC heartbeat has been touched recently.
    Returns (is_fresh, age_seconds).
    """
    if not heartbeat_path.exists():
        return False, float("inf")
    age = time.time() - heartbeat_path.stat().st_mtime
    return age < OHLC_STALE_THRESHOLD, age


def check_training_active(training_stdout: Path) -> bool:
    """Check if training is actively producing output (stdout growing)."""
    if not training_stdout.exists():
        return False
    age = time.time() - training_stdout.stat().st_mtime
    return age < 120


def estimate_steps_from_stdout(training_stdout: Path) -> int:
    """Quick estimate of steps produced by counting 'Step N' lines in tail."""
    try:
        with open(training_stdout, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            seek_pos = max(0, size - 50000)
            f.seek(seek_pos)
            tail = f.read().decode("utf-8", errors="replace")
        steps = re.findall(r"Step (\d+)", tail)
        return int(steps[-1]) if steps else 0
    except Exception:
        return 0


# ── Lock file (prevent duplicate instances) ─────────────────────────────────

def acquire_lock(lock_path: Path) -> bool:
    """Acquire an exclusive lock file. Returns True if acquired."""
    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_fh = open(lock_path, "w")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fh.write(f"{os.getpid()}\n")
        lock_fh.flush()
        # Keep file handle open for the lifetime of the process
        acquire_lock._fh = lock_fh
        return True
    except (IOError, OSError):
        return False


def release_lock(lock_path: Path) -> None:
    import fcntl
    fh = getattr(acquire_lock, "_fh", None)
    if fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()
        except Exception:
            pass
    lock_path.unlink(missing_ok=True)


# ── OHLC root cause diagnostics ──────────────────────────────────────────────

def _diagnose_ohlc_failure(monitor_pid: int, ohlc_csv: Path,
                            heartbeat_path: Path, training_stdout: Path,
                            monitor_state: Path | None,
                            heartbeat_age: float) -> list[str]:
    """Diagnose WHY OHLC stopped being written. Returns list of findings."""
    findings = []

    # 1. Is training_monitor PID alive?
    if monitor_pid and not is_process_alive(monitor_pid):
        findings.append(f"training_monitor pid={monitor_pid} is DEAD")
    elif monitor_pid:
        findings.append(f"training_monitor pid={monitor_pid} is alive")

    # 2. Is training producing step output?
    if training_stdout.exists():
        stdout_age = time.time() - training_stdout.stat().st_mtime
        stdout_size = training_stdout.stat().st_size
        if stdout_age > 120:
            findings.append(f"training stdout stale ({stdout_age:.0f}s old, {stdout_size} bytes)")
        else:
            findings.append(f"training stdout active ({stdout_age:.0f}s old, {stdout_size} bytes)")
    else:
        findings.append("training stdout file does not exist")

    # 3. Does OHLC file exist and have data?
    if ohlc_csv.exists():
        ohlc_size = ohlc_csv.stat().st_size
        ohlc_age = time.time() - ohlc_csv.stat().st_mtime
        findings.append(f"OHLC file exists ({ohlc_size} bytes, last modified {ohlc_age:.0f}s ago)")
    else:
        findings.append("OHLC file does NOT exist")

    # 4. Check monitor state file
    if monitor_state and monitor_state.exists():
        try:
            state = json.loads(monitor_state.read_text())
            findings.append(f"monitor state: file_pos={state.get('file_pos', '?')}, "
                          f"steps_written={state.get('steps_written', '?')}")
        except Exception:
            findings.append("monitor state file corrupt")
    elif monitor_state:
        findings.append("monitor state file does not exist (first start?)")

    # 5. Check heartbeat
    if heartbeat_path.exists():
        findings.append(f"heartbeat file exists, age={heartbeat_age:.0f}s")
    else:
        findings.append("heartbeat file does NOT exist (monitor never wrote a candle?)")

    # 6. Check disk space
    try:
        st = os.statvfs(str(ohlc_csv.parent))
        free_gb = (st.f_bavail * st.f_frsize) / (1024**3)
        if free_gb < 1:
            findings.append(f"DISK NEARLY FULL: {free_gb:.2f} GB free")
        else:
            findings.append(f"disk OK: {free_gb:.1f} GB free")
    except Exception:
        pass

    return findings


# ── Companion process management ─────────────────────────────────────────────

def start_background_process(cmd: list[str], log_path: Path, name: str,
                              append: bool = False) -> int:
    """Start a detached background process, return PID."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "a" if append else "w")
    proc = subprocess.Popen(
        cmd, stdout=log_fh, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_fh.close()
    print(f"  [{name}] started pid={proc.pid}, log={log_path}", flush=True)
    return proc.pid


def stop_process(pid: int, name: str, timeout: int = 10) -> None:
    """SIGTERM → wait → SIGKILL if needed."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    print(f"  [{name}] stopping pid={pid}...", flush=True)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"  [{name}] pid={pid} exited", flush=True)
            return
        time.sleep(0.5)
    print(f"  [{name}] pid={pid} didn't exit, sending SIGKILL", flush=True)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # os.kill(0) succeeds for zombies — check /proc status
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    return "Z" not in line
    except (FileNotFoundError, PermissionError):
        return False
    return True


def stop_all_companions(companions: dict[str, int]) -> None:
    for name, pid in companions.items():
        if pid:
            stop_process(pid, name)


def start_training_monitor(training_stdout: Path, ohlc_csv: Path,
                            log_dir: Path, run_name: str = "unknown",
                            heartbeat_path: Path | None = None,
                            state_file: Path | None = None) -> int:
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "training_monitor.py"),
        "--log", str(training_stdout),
        "--out", str(ohlc_csv),
        "--poll-interval", "5",
        "--run-name", run_name,
    ]
    if heartbeat_path:
        cmd.extend(["--heartbeat", str(heartbeat_path)])
    if state_file:
        cmd.extend(["--state-file", str(state_file)])
    return start_background_process(
        cmd, log_dir / "training_monitor.stdout", "training_monitor",
        append=True)


def build_sidecar_file_list(log_dir: Path, training_stdout: Path,
                            per_run_ohlc: Path | None = None) -> list[Path]:
    files = [
        training_stdout,
        log_dir / "chain_runner.jsonl",
        log_dir / "chain_runner.stdout",
        log_dir / "ram_monitor.csv",
        log_dir / "checkpoint_watcher.log",
        log_dir / "training_monitor.stdout",
        log_dir / "ram_monitor.stdout",
    ]
    if per_run_ohlc:
        files.append(per_run_ohlc)
    return files


def start_sidecar(r2_prefix: str, log_dir: Path,
                   training_stdout: Path,
                   per_run_ohlc: Path | None = None) -> int:
    file_list = build_sidecar_file_list(log_dir, training_stdout, per_run_ohlc)
    cmd = [
        "bash", str(SCRIPTS_DIR / "sidecar_log_mirror.sh"),
        "--prefix", r2_prefix,
        "--interval", str(SIDECAR_INTERVAL),
    ]
    for f in file_list:
        cmd.extend(["--add", str(f)])
    return start_background_process(
        cmd, log_dir / "sidecar.log", "sidecar", append=True)


def start_checkpoint_watcher(training_output: str, log_dir: Path,
                              env_file: Path, r2_prefix: str) -> int:
    """Start checkpoint_watcher as a managed companion. MANDATORY."""
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "checkpoint_watcher.py"),
        "--env-file", str(env_file),
        "--runs-root", str(training_output),
        "--poll-interval", "30",
        "--prefix-override", r2_prefix,
    ]
    return start_background_process(
        cmd, log_dir / "checkpoint_watcher.log", "checkpoint_watcher",
        append=True)


def health_check_companions(companions: dict[str, int]) -> list[str]:
    """Return names of companion processes that have died."""
    dead = []
    for name, pid in companions.items():
        if pid and not is_process_alive(pid):
            dead.append(name)
    return dead


def write_run_manifest(log_dir: Path, run_name: str,
                        companions: dict[str, int], run_info: dict,
                        training_stdout: Path, train_pid: int) -> None:
    manifest = {
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_name": run_name,
        "run_number": run_info["run"],
        "dataset": run_info["dataset"],
        "block": run_info.get("block"),
        "seed": run_info["seed"],
        "max_steps": run_info["max_steps"],
        "checkpoint_step": run_info["checkpoint_step"],
        "training_stdout": str(training_stdout),
        "pids": {
            "training": train_pid,
            **{name: pid for name, pid in companions.items() if pid},
        },
    }
    manifest_path = log_dir / "run_manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"  Manifest: {manifest_path}", flush=True)


# ── Training launch ──────────────────────────────────────────────────────────

def launch_training(run_info: dict, data_paths: dict, resume_ckpt: str | None,
                    base_model: str, protenix_dir: str,
                    training_output: str, seed_override: int | None = None,
                    run_name_override: str | None = None,
                    ) -> tuple[subprocess.Popen, Path, IO]:
    """Launch training as a subprocess. Returns (proc, stdout_path, stdout_fh)."""
    if seed_override is not None:
        ri = dict(run_info)
        ri["seed"] = seed_override
    else:
        ri = run_info

    cmd, run_name = build_launch_command(ri, data_paths, resume_ckpt,
                                          base_model, protenix_dir)
    if run_name_override:
        idx = cmd.index("--run_name") + 1
        cmd[idx] = run_name_override
        run_name = run_name_override

    env = os.environ.copy()
    env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

    print(f"  Launching: {run_name}", flush=True)
    print(f"  Seed: {ri['seed']}, max_steps: {ri['max_steps']}", flush=True)
    print(f"  Resume from: {resume_ckpt or base_model}", flush=True)

    log_path = Path(training_output) / f"{run_name}.stdout"
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(cmd, env=env, stdout=log_fh, stderr=subprocess.STDOUT,
                            cwd=protenix_dir)
    return proc, log_path, log_fh


# ── Data staging ─────────────────────────────────────────────────────────────

def ensure_idp_data(bioassembly_dir: str, idp_index: str, idp_pdb_list: str,
                    creds_file: Path) -> None:
    """Verify IDP data is on local disk; download from R2 if missing."""
    bio_dir = Path(bioassembly_dir)
    idx_path = Path(idp_index)
    pdb_path = Path(idp_pdb_list)
    uris = get_idp_r2_uris()

    if not bio_dir.exists() or sum(1 for _ in bio_dir.glob("*.pkl.gz")) < 1000:
        print("[startup] IDP bioassemblies missing or incomplete, downloading "
              "from R2...", flush=True)
        bio_dir.mkdir(parents=True, exist_ok=True)
        count = download_and_extract_zip(uris["bioassembly_zip"], bio_dir,
                                         creds_file)
        print(f"[startup] staged {count} IDP bioassemblies", flush=True)
    else:
        count = sum(1 for _ in bio_dir.glob("*.pkl.gz"))
        print(f"[startup] IDP bioassemblies present: {count} files", flush=True)

    if not idx_path.exists():
        print(f"[startup] IDP index missing, downloading...", flush=True)
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        download_object(uris["indices_csv"], idx_path, creds_file)

    if not pdb_path.exists():
        print(f"[startup] IDP PDB list missing, downloading...", flush=True)
        pdb_path.parent.mkdir(parents=True, exist_ok=True)
        download_object(uris["pdb_list"], pdb_path, creds_file)


def stage_pdb_block(run_info: dict, pdb_staging_dir: Path,
                    pdb_block_dir: str, creds_file: Path) -> dict:
    """Download PDB block data from R2 before a PDB training run.

    Downloads block ZIP → extracts bioassemblies to staging dir.
    Downloads block index CSV + PDB ID list to pdb_block_dir.
    Returns data_paths dict pointing at the staged data.

    Raises BlockSkipped for blocks with 0 training rows (permanent condition).
    Raises RuntimeError for transient staging failures (retryable).
    """
    block = run_info["block"]
    uris = get_pdb_block_r2_uris(block)
    bio_dir = pdb_staging_dir / "bioassembly"

    if bio_dir.exists():
        shutil.rmtree(bio_dir)
    bio_dir.mkdir(parents=True, exist_ok=True)

    print(f"  [stage] downloading PDB block {block:02d} bioassemblies...",
          flush=True)
    count = download_and_extract_zip(uris["bioassembly_zip"], bio_dir,
                                     creds_file)
    print(f"  [stage] extracted {count} .pkl.gz files for block {block:02d}",
          flush=True)

    idx_dir = Path(pdb_block_dir)
    idx_dir.mkdir(parents=True, exist_ok=True)
    csv_dest = idx_dir / f"block{block:02d}.csv"
    ids_dest = idx_dir / f"block{block:02d}_pdb_ids.txt"

    print(f"  [stage] downloading block {block:02d} index files...", flush=True)
    download_object(uris["indices_csv"], csv_dest, creds_file)
    download_object(uris["pdb_list"], ids_dest, creds_file)

    if count == 0:
        raise RuntimeError(
            f"Block {block:02d} ZIP contained no .pkl.gz files")
    if not csv_dest.exists():
        raise RuntimeError(f"Block {block:02d} index CSV download failed")
    csv_lines = sum(1 for _ in open(csv_dest))
    if csv_lines < 2:
        shutil.rmtree(bio_dir, ignore_errors=True)
        csv_dest.unlink(missing_ok=True)
        ids_dest.unlink(missing_ok=True)
        raise BlockSkipped(
            f"Block {block:02d} has 0 training rows in master index — "
            f"no data to train on, skipping permanently")

    csv_mb = csv_dest.stat().st_size / (1024 * 1024)
    print(f"  [stage] block {block:02d} ready: {count} bioassemblies, "
          f"index {csv_mb:.1f} MB", flush=True)

    return {
        "bioassembly_dict_dir": str(bio_dir),
        "indices_fpath": str(csv_dest),
        "pdb_list": str(ids_dest),
    }


def cleanup_pdb_block(run_info: dict, pdb_staging_dir: Path,
                      pdb_block_dir: str) -> None:
    """Delete PDB block data after training completes and checkpoint
    is confirmed on R2."""
    block = run_info["block"]
    bio_dir = pdb_staging_dir / "bioassembly"

    if bio_dir.exists():
        count = sum(1 for _ in bio_dir.rglob("*.pkl.gz"))
        shutil.rmtree(bio_dir)
        print(f"  [cleanup] deleted {count} PDB block {block:02d} "
              f"bioassemblies from staging", flush=True)

    for fname in [f"block{block:02d}.csv", f"block{block:02d}_pdb_ids.txt"]:
        p = Path(pdb_block_dir) / fname
        if p.exists():
            p.unlink()
            print(f"  [cleanup] deleted {fname}", flush=True)


# ── Main chain loop ──────────────────────────────────────────────────────────

def run_chain(args) -> int:
    print_version_banner()

    # Print companion script versions for audit
    for script_name in ["checkpoint_watcher.py", "training_monitor.py",
                        "sidecar_log_mirror.sh", "select_next_training_run.py"]:
        script_path = SCRIPTS_DIR / script_name
        if script_path.exists():
            sha = _file_sha256(str(script_path))
            print(f"  companion: {script_name}  sha256:{sha}", flush=True)
    print(flush=True)

    # ── Exclusive lock — prevent duplicate chain_runner instances ────
    if not acquire_lock(LOCK_FILE):
        print(f"FATAL: Another chain_runner is already running "
              f"(lock file: {LOCK_FILE}). Exiting.", flush=True)
        return 1

    schedule = build_full_schedule()
    total_runs = len(schedule)
    chain_log = args.chain_log
    pdb_staging_dir = Path(args.pdb_staging_dir)
    creds_file = args.creds_file
    log_dir = Path(args.log_dir)
    r2_prefix = args.r2_ops_prefix
    manage = args.manage_companions

    ensure_idp_data(args.bioassembly_dir, args.idp_index, args.idp_pdb_list,
                    creds_file)

    log_event(chain_log, {"event": "chain_start",
                          "version": VERSION,
                          "total_runs": total_runs,
                          "campaign": R2_CAMPAIGN_PREFIX,
                          "manage_companions": manage})

    consecutive_failures = 0
    skipped_runs: set[int] = set()
    companions: dict[str, int] = {}
    training_stdout_fh: IO | None = None

    # ── Start checkpoint_watcher (persistent, runs across all runs) ──────
    # MANDATORY: training must not proceed without R2 checkpoint uploads.
    # Local disk is ephemeral; only R2 is persistent storage.
    if manage:
        env_file = Path(args.env_file)
        if not env_file.exists():
            print(f"FATAL: checkpoint_watcher env file not found: {env_file}",
                  flush=True)
            print("  Cannot start training without checkpoint_watcher.",
                  flush=True)
            release_lock(LOCK_FILE)
            return 1
        companions["checkpoint_watcher"] = start_checkpoint_watcher(
            args.training_output, log_dir, env_file, r2_prefix)
        log_event(chain_log, {
            "event": "checkpoint_watcher_started",
            "pid": companions["checkpoint_watcher"],
        })

    while True:
        latest_step, latest_ckpt = find_latest_boundary_checkpoint(
            args.training_output, r2_prefix=r2_prefix,
            creds_file=str(creds_file))
        completed = get_completed_run_count(latest_step)

        run_idx = completed
        while run_idx < total_runs and schedule[run_idx]["run"] in skipped_runs:
            run_idx += 1

        if run_idx >= total_runs:
            log_event(chain_log, {"event": "chain_complete",
                                  "completed_runs": completed,
                                  "skipped_runs": sorted(skipped_runs)})
            print(f"All {total_runs} runs complete (or skipped). Training is done.",
                  flush=True)
            if manage:
                stop_all_companions(companions)
            release_lock(LOCK_FILE)
            return 0

        next_run = schedule[run_idx]
        prev_boundary = latest_step
        is_pdb = next_run["dataset"] == "PDB"

        # ── Data staging ─────────────────────────────────────────────────
        if is_pdb:
            try:
                data_paths = stage_pdb_block(
                    next_run, pdb_staging_dir, args.pdb_block_dir,
                    creds_file)
                log_event(chain_log, {
                    "event": "pdb_block_staged",
                    "run_number": next_run["run"],
                    "block": next_run["block"],
                })
            except BlockSkipped as e:
                log_event(chain_log, {
                    "event": "block_skipped",
                    "run_number": next_run["run"],
                    "block": next_run.get("block"),
                    "reason": str(e),
                })
                skipped_runs.add(next_run["run"])
                continue
            except Exception as e:
                log_event(chain_log, {
                    "event": "staging_error",
                    "run_number": next_run["run"],
                    "block": next_run.get("block"),
                    "reason": str(e),
                })
                print(f"  ERROR staging PDB block: {e}", flush=True)
                consecutive_failures += 1
                if consecutive_failures >= args.max_retries:
                    if manage:
                        stop_all_companions(companions)
                    return 1
                time.sleep(60)
                continue
        else:
            data_paths = get_data_paths(
                next_run, args.bioassembly_dir, args.idp_index,
                args.idp_pdb_list, args.pdb_block_dir)

        # ── Interrupted run detection ────────────────────────────────────
        intermediates = find_intermediate_checkpoints(
            next_run, args.training_output, prev_boundary)

        seed_override = None
        run_name_override = None
        resume_ckpt = latest_ckpt

        if intermediates:
            highest_step, highest_path = intermediates[-1]
            resume_num = 1
            for _, p in intermediates:
                m = re.search(r"_resume(\d+)", p)
                if m:
                    resume_num = max(resume_num, int(m.group(1)) + 1)

            seed_override = compute_resumption_seed(next_run["seed"], resume_num)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            base_name = generate_run_name(next_run).rsplit("_", 1)[0]
            run_name_override = f"{base_name}_resume{resume_num}_{ts}"
            resume_ckpt = highest_path

            # If the latest intermediate exists only as .age (encrypted by
            # checkpoint_watcher post-upload), train.py cannot load it.
            # Recover the cleartext .pt from R2.
            if resume_ckpt and resume_ckpt.endswith(".age"):
                print(f"  Latest intermediate is encrypted ({resume_ckpt}); "
                      f"recovering cleartext for step {highest_step} from R2...",
                      flush=True)
                rec_step, rec_path = _recover_boundary_from_r2(
                    args.training_output, r2_prefix,
                    creds_file=str(creds_file),
                    target_step=highest_step)
                if rec_step == highest_step and rec_path:
                    resume_ckpt = rec_path
                else:
                    print(f"  WARNING: could not recover step {highest_step} "
                          f"from R2; falling back to latest boundary checkpoint.",
                          flush=True)
                    # Discard the intermediate-resume attempt; chain_runner
                    # will start fresh from the boundary (which has its own
                    # R2 fallback in find_latest_boundary_checkpoint).
                    seed_override = None
                    run_name_override = None
                    resume_ckpt = latest_ckpt

            log_event(chain_log, {
                "event": "resuming_interrupted",
                "run_number": next_run["run"],
                "dataset": next_run["dataset"],
                "seed": seed_override,
                "original_seed": next_run["seed"],
                "resume_from_step": highest_step,
                "resume_path": highest_path,
                "resumption_number": resume_num,
            })
        else:
            log_event(chain_log, {
                "event": "run_start",
                "run_number": next_run["run"],
                "dataset": next_run["dataset"],
                "block": next_run.get("block"),
                "visit": next_run.get("visit"),
                "seed": seed_override or next_run["seed"],
                "max_steps": next_run["max_steps"],
                "checkpoint_step": next_run["checkpoint_step"],
                "resume_from": str(resume_ckpt or args.base_model),
            })

        # ── Prerequisite validation ──────────────────────────────────────
        errors = validate_prerequisites(next_run, data_paths, resume_ckpt,
                                         args.base_model)
        if errors:
            for e in errors:
                print(f"  ERROR: {e}", flush=True)
            log_event(chain_log, {
                "event": "fatal_error",
                "run_number": next_run["run"],
                "reason": "; ".join(errors),
            })
            if manage:
                stop_all_companions(companions)
            return 1

        if args.dry_run:
            print(f"  [DRY RUN] Would launch run {next_run['run']}: "
                  f"{next_run['dataset']}, seed={seed_override or next_run['seed']}, "
                  f"max_steps={next_run['max_steps']}", flush=True)
            return 0

        # ── Stop old companions before new run ───────────────────────────
        if manage:
            if companions.get("training_monitor"):
                stop_process(companions["training_monitor"], "training_monitor")
                companions["training_monitor"] = 0
            if companions.get("sidecar"):
                stop_process(companions["sidecar"], "sidecar")
                companions["sidecar"] = 0

        if training_stdout_fh:
            training_stdout_fh.close()
            training_stdout_fh = None

        # ── Launch training ──────────────────────────────────────────────
        t0 = time.time()
        proc, training_stdout, training_stdout_fh = launch_training(
            next_run, data_paths, resume_ckpt, args.base_model,
            args.protenix_dir, args.training_output,
            seed_override=seed_override,
            run_name_override=run_name_override)

        run_name = training_stdout.stem
        print(f"  Training PID: {proc.pid}", flush=True)

        # ── Per-run OHLC paths ──────────────────────────────────────────
        ohlc_dir = Path(args.log_dir) / "ohlc"
        ohlc_dir.mkdir(parents=True, exist_ok=True)
        per_run_ohlc = ohlc_dir / f"{run_name}.csv"
        heartbeat_path = ohlc_dir / f"{run_name}.heartbeat"
        monitor_state = ohlc_dir / f"{run_name}.monitor_state"

        # ── Start new companions for this run ────────────────────────────
        if manage:
            companions["training_monitor"] = start_training_monitor(
                training_stdout, per_run_ohlc, log_dir,
                run_name=run_name,
                heartbeat_path=heartbeat_path,
                state_file=monitor_state)
            companions["sidecar"] = start_sidecar(
                r2_prefix, log_dir, training_stdout, per_run_ohlc)

            write_run_manifest(log_dir, run_name, companions, next_run,
                               training_stdout, proc.pid)

        # ── Wait for training with health monitoring ─────────────────────
        ohlc_kill_reason = None
        try:
            while proc.poll() is None:
                if _SIGTERM_RECEIVED:
                    print("  SIGTERM: stopping training gracefully...", flush=True)
                    proc.terminate()
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    log_event(chain_log, {"event": "sigterm_shutdown",
                                          "run_number": next_run["run"]})
                    if manage:
                        stop_all_companions(companions)
                    if training_stdout_fh:
                        training_stdout_fh.close()
                    release_lock(LOCK_FILE)
                    return 143

                if manage:
                    # 1. Check if companion PIDs are alive
                    dead = health_check_companions(companions)
                    for name in dead:
                        log_event(chain_log, {
                            "event": "companion_restarted",
                            "companion": name,
                            "run_number": next_run["run"],
                        })
                        if name == "training_monitor":
                            companions[name] = start_training_monitor(
                                training_stdout, per_run_ohlc, log_dir,
                                run_name=run_name,
                                heartbeat_path=heartbeat_path,
                                state_file=monitor_state)
                        elif name == "sidecar":
                            companions[name] = start_sidecar(
                                r2_prefix, log_dir, training_stdout,
                                per_run_ohlc)
                        elif name == "checkpoint_watcher":
                            companions[name] = start_checkpoint_watcher(
                                args.training_output, log_dir,
                                Path(args.env_file), r2_prefix)

                    # 2. OHLC freshness gate — HARD REQUIREMENT
                    current_step = estimate_steps_from_stdout(training_stdout)
                    step_lo = next_run["checkpoint_step"] - STEPS_PER_RUN + 1
                    steps_into_run = current_step - step_lo + 1 if current_step >= step_lo else 0

                    if steps_into_run > OHLC_STALE_GRACE_STEPS:
                        is_fresh, age = check_ohlc_freshness(heartbeat_path)
                        if not is_fresh and check_training_active(training_stdout):
                            # ── Root cause diagnosis before any restart ──
                            diag = _diagnose_ohlc_failure(
                                companions.get("training_monitor", 0),
                                per_run_ohlc, heartbeat_path,
                                training_stdout, monitor_state, age)
                            print(f"  WARNING: OHLC heartbeat stale ({age:.0f}s)",
                                  flush=True)
                            for line in diag:
                                print(f"    DIAG: {line}", flush=True)
                            log_event(chain_log, {
                                "event": "ohlc_stale_detected",
                                "run_number": next_run["run"],
                                "heartbeat_age_s": round(age, 1),
                                "diagnosis": diag,
                            })

                            # Attempt targeted fix based on diagnosis
                            if companions.get("training_monitor"):
                                stop_process(companions["training_monitor"],
                                             "training_monitor", timeout=5)
                            companions["training_monitor"] = start_training_monitor(
                                training_stdout, per_run_ohlc, log_dir,
                                run_name=run_name,
                                heartbeat_path=heartbeat_path,
                                state_file=monitor_state)
                            time.sleep(30)

                            is_fresh2, age2 = check_ohlc_freshness(heartbeat_path)
                            if not is_fresh2:
                                diag2 = _diagnose_ohlc_failure(
                                    companions.get("training_monitor", 0),
                                    per_run_ohlc, heartbeat_path,
                                    training_stdout, monitor_state, age2)
                                ohlc_kill_reason = (
                                    f"OHLC heartbeat still stale after monitor restart "
                                    f"({age2:.0f}s old). Root cause: {'; '.join(diag2)}. "
                                    f"Training HALTED — fix the root cause before retrying.")
                                print(f"  FATAL: {ohlc_kill_reason}", flush=True)
                                log_event(chain_log, {
                                    "event": "ohlc_fatal",
                                    "run_number": next_run["run"],
                                    "diagnosis": diag2,
                                    "reason": ohlc_kill_reason,
                                })
                                proc.terminate()
                                try:
                                    proc.wait(timeout=30)
                                except subprocess.TimeoutExpired:
                                    proc.kill()
                                    proc.wait()
                                break

                time.sleep(HEALTH_CHECK_INTERVAL)
        except KeyboardInterrupt:
            print("\n  KeyboardInterrupt — sending SIGTERM to training...",
                  flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            log_event(chain_log, {"event": "chain_interrupted",
                                  "run_number": next_run["run"]})
            if manage:
                stop_all_companions(companions)
            if training_stdout_fh:
                training_stdout_fh.close()
            release_lock(LOCK_FILE)
            return 130

        duration = time.time() - t0
        exit_code = proc.returncode

        if training_stdout_fh:
            training_stdout_fh.close()
            training_stdout_fh = None

        # ── Flush training_monitor's final candle before OHLC check ─────
        # training_monitor flushes its in-memory candle on SIGTERM (graceful
        # shutdown). We MUST stop it and wait for exit BEFORE checking OHLC
        # completeness, otherwise we race against the final candle write.
        if manage and companions.get("training_monitor"):
            stop_process(companions["training_monitor"], "training_monitor")
            companions["training_monitor"] = 0

        # ── Handle OHLC-triggered kill ──────────────────────────────────
        if ohlc_kill_reason:
            consecutive_failures += 1
            log_event(chain_log, {
                "event": "run_failed",
                "run_number": next_run["run"],
                "dataset": next_run["dataset"],
                "seed": seed_override or next_run["seed"],
                "exit_code": -1,
                "duration_s": round(duration, 1),
                "consecutive_failures": consecutive_failures,
                "reason": ohlc_kill_reason,
            })
            if consecutive_failures >= args.max_retries:
                log_event(chain_log, {
                    "event": "fatal_error",
                    "run_number": next_run["run"],
                    "reason": f"OHLC monitoring failure after {consecutive_failures} attempts",
                })
                if manage:
                    stop_all_companions(companions)
                release_lock(LOCK_FILE)
                return 1
            print(f"  Retrying in 60s (failure {consecutive_failures}/{args.max_retries})...",
                  flush=True)
            time.sleep(60)
            continue

        boundary_exists = find_boundary_checkpoint(
            next_run, args.training_output) is not None

        if exit_code == 0 or boundary_exists:
            # ── OHLC completeness verification (HARD GATE) ──────────────
            step_lo = next_run["checkpoint_step"] - STEPS_PER_RUN + 1
            step_hi = next_run["checkpoint_step"]
            # For resumed runs, OHLC data is split across original + resume
            # CSVs. Collect ALL matching files for this run window.
            run_prefix = f"run{next_run['run']:03d}_"
            step_tag = f"step{next_run['checkpoint_step']}_"
            all_run_ohlcs = sorted(
                p for p in ohlc_dir.glob(f"{run_prefix}*{step_tag}*.csv"))
            if not all_run_ohlcs:
                all_run_ohlcs = [per_run_ohlc]
            ohlc_ok, ohlc_msg = verify_ohlc_completeness(
                all_run_ohlcs, step_lo, step_hi)

            if not ohlc_ok:
                log_event(chain_log, {
                    "event": "ohlc_verification_failed",
                    "run_number": next_run["run"],
                    "dataset": next_run["dataset"],
                    "seed": seed_override or next_run["seed"],
                    "exit_code": exit_code,
                    "duration_s": round(duration, 1),
                    "ohlc_status": ohlc_msg,
                    "reason": f"Training completed but OHLC verification failed: {ohlc_msg}",
                })
                print(f"  FATAL: OHLC verification failed — {ohlc_msg}", flush=True)
                print(f"  Training WILL NOT advance. The run must be repeated "
                      f"with working OHLC monitoring.", flush=True)
                if manage:
                    stop_all_companions(companions)
                release_lock(LOCK_FILE)
                return 1

            print(f"  OHLC: {ohlc_msg}", flush=True)
            log_event(chain_log, {
                "event": "run_end",
                "run_number": next_run["run"],
                "dataset": next_run["dataset"],
                "seed": seed_override or next_run["seed"],
                "exit_code": exit_code,
                "duration_s": round(duration, 1),
                "boundary_checkpoint": next_run["checkpoint_step"],
                "ohlc_status": ohlc_msg,
                "ohlc_file": str(per_run_ohlc),
            })
            consecutive_failures = 0

            wait_for_watcher(args.inter_run_pause, next_run,
                             args.watcher_state)

            if is_pdb:
                cleanup_pdb_block(next_run, pdb_staging_dir,
                                  args.pdb_block_dir)
                log_event(chain_log, {
                    "event": "pdb_block_cleaned",
                    "run_number": next_run["run"],
                    "block": next_run["block"],
                })
        else:
            consecutive_failures += 1
            log_event(chain_log, {
                "event": "run_failed",
                "run_number": next_run["run"],
                "dataset": next_run["dataset"],
                "seed": seed_override or next_run["seed"],
                "exit_code": exit_code,
                "duration_s": round(duration, 1),
                "consecutive_failures": consecutive_failures,
                "reason": f"exit code {exit_code}, no boundary checkpoint",
            })
            if consecutive_failures >= args.max_retries:
                log_event(chain_log, {
                    "event": "fatal_error",
                    "run_number": next_run["run"],
                    "reason": f"{consecutive_failures} consecutive failures",
                })
                if manage:
                    stop_all_companions(companions)
                release_lock(LOCK_FILE)
                return 1
            print(f"  Retrying in 60s (failure {consecutive_failures}/{args.max_retries})...",
                  flush=True)
            time.sleep(60)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--training-output", default=DEFAULT_TRAINING_OUTPUT)
    ap.add_argument("--bioassembly-dir", default=DEFAULT_BIOASSEMBLY_DIR)
    ap.add_argument("--idp-index", default=DEFAULT_IDP_INDEX)
    ap.add_argument("--idp-pdb-list", default=DEFAULT_IDP_PDB_LIST)
    ap.add_argument("--pdb-block-dir", default=DEFAULT_PDB_BLOCK_DIR)
    ap.add_argument("--pdb-staging-dir", default=DEFAULT_PDB_STAGING_DIR,
                    help="Temp dir for PDB block bioassemblies (downloaded/deleted per run)")
    ap.add_argument("--creds-file", type=Path, default=DEFAULT_CREDS_FILE,
                    help="Shell-export creds file for R2 access")
    ap.add_argument("--base-model", default=DEFAULT_BASE_MODEL)
    ap.add_argument("--protenix-dir", default=DEFAULT_PROTENIX_DIR)
    ap.add_argument("--chain-log", type=Path, default=CHAIN_LOG_DEFAULT)
    ap.add_argument("--watcher-state", type=Path, default=WATCHER_STATE_DEFAULT)
    ap.add_argument("--inter-run-pause", type=int, default=480,
                    help="Seconds between runs for watcher upload (default: 480). "
                         "Must cover poll-delay (≤30s) + model upload (~60s) + "
                         "ema upload (~60s) + encrypt (~15s) + state write. "
                         "Below ~180s, state file will NOT yet show the boundary "
                         "step when timeout fires, causing a guaranteed "
                         "warning-then-recovery cycle on the next run.")
    ap.add_argument("--max-retries", type=int, default=3,
                    help="Max consecutive failures before aborting (default: 3)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would run without executing")
    ap.add_argument("--log-dir", default="/data",
                    help="Directory for companion process logs (default: /data)")
    ap.add_argument("--ohlc-dir", default="/data/logs/ohlc",
                    help="Directory for per-run OHLC CSV files")
    ap.add_argument("--r2-ops-prefix", default=R2_CAMPAIGN_PREFIX,
                    help="R2 ops/ prefix for sidecar log mirror")
    ap.add_argument("--env-file", default="/data/.env.cloudflare",
                    help="Env file with R2 credentials for checkpoint_watcher")
    manage_group = ap.add_mutually_exclusive_group()
    manage_group.add_argument("--manage-companions", dest="manage_companions",
                              action="store_true", default=True,
                              help="Manage training_monitor + sidecar lifecycle (default)")
    manage_group.add_argument("--no-manage-companions", dest="manage_companions",
                              action="store_false",
                              help="Skip companion management (external launcher handles them)")
    args = ap.parse_args()
    return run_chain(args)


if __name__ == "__main__":
    sys.exit(main())
