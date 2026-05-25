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

Companion process management:
  - training_monitor.py is restarted between runs (watches per-run stdout)
  - sidecar_log_mirror.sh is restarted between runs (updated file list)
  - Health checks every 60s restart any crashed companions
  - ram_monitor, vram_monitor, checkpoint_watcher are persistent (external)

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

import argparse
import glob
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

sys.path.insert(0, str(Path(__file__).parent))
from select_next_training_run import (
    build_full_schedule,
    find_latest_boundary_checkpoint,
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

SCRIPTS_DIR = Path(__file__).parent
SIDECAR_INTERVAL = 60
HEALTH_CHECK_INTERVAL = 60


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
    pattern = os.path.join(training_output, f"*/checkpoints/{expected_step}.pt")
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def find_intermediate_checkpoints(run_info: dict, training_output: str,
                                   prev_boundary: int | None) -> list[tuple[int, str]]:
    target = run_info["checkpoint_step"]
    floor = (prev_boundary or -1) + 1
    pattern = os.path.join(training_output, "*/checkpoints/*.pt")
    intermediates = []
    for path in glob.glob(pattern):
        basename = os.path.basename(path)
        if "_ema_" in basename:
            continue
        m = re.match(r"^(\d+)\.pt$", basename)
        if not m:
            continue
        step = int(m.group(1))
        if floor <= step < target:
            intermediates.append((step, path))
    intermediates.sort(key=lambda x: x[0])
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
    print(f"  Watcher upload not confirmed after {pause_seconds}s — proceeding anyway.",
          flush=True)


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
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_all_companions(companions: dict[str, int]) -> None:
    for name, pid in companions.items():
        if pid:
            stop_process(pid, name)


def start_training_monitor(training_stdout: Path, ohlc_csv: Path,
                            log_dir: Path) -> int:
    cmd = [
        sys.executable, str(SCRIPTS_DIR / "training_monitor.py"),
        "--log", str(training_stdout),
        "--out", str(ohlc_csv),
        "--poll-interval", "5",
    ]
    return start_background_process(
        cmd, log_dir / "training_monitor.stdout", "training_monitor",
        append=True)


def build_sidecar_file_list(log_dir: Path, training_stdout: Path) -> list[Path]:
    return [
        training_stdout,
        log_dir / "chain_runner.jsonl",
        log_dir / "chain_runner.stdout",
        log_dir / "ram_monitor.csv",
        log_dir / "checkpoint_watcher.log",
        log_dir / "training_ohlc.csv",
        log_dir / "training_monitor.stdout",
        log_dir / "ram_monitor.stdout",
    ]


def start_sidecar(r2_prefix: str, log_dir: Path,
                   training_stdout: Path) -> int:
    file_list = build_sidecar_file_list(log_dir, training_stdout)
    cmd = [
        "bash", str(SCRIPTS_DIR / "sidecar_log_mirror.sh"),
        "--prefix", r2_prefix,
        "--interval", str(SIDECAR_INTERVAL),
    ]
    for f in file_list:
        cmd.extend(["--add", str(f)])
    return start_background_process(
        cmd, log_dir / "sidecar.log", "sidecar", append=True)


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
    schedule = build_full_schedule()
    chain_log = args.chain_log
    pdb_staging_dir = Path(args.pdb_staging_dir)
    creds_file = args.creds_file
    log_dir = Path(args.log_dir)
    ohlc_csv = Path(args.ohlc_csv)
    r2_prefix = args.r2_ops_prefix
    manage = args.manage_companions

    ensure_idp_data(args.bioassembly_dir, args.idp_index, args.idp_pdb_list,
                    creds_file)

    log_event(chain_log, {"event": "chain_start",
                          "total_runs": 147,
                          "campaign": R2_CAMPAIGN_PREFIX,
                          "manage_companions": manage})

    consecutive_failures = 0
    skipped_runs: set[int] = set()
    companions: dict[str, int] = {}
    training_stdout_fh: IO | None = None

    while True:
        latest_step, latest_ckpt = find_latest_boundary_checkpoint(
            args.training_output)
        completed = get_completed_run_count(latest_step)

        run_idx = completed
        while run_idx < 147 and schedule[run_idx]["run"] in skipped_runs:
            run_idx += 1

        if run_idx >= 147:
            log_event(chain_log, {"event": "chain_complete",
                                  "completed_runs": completed,
                                  "skipped_runs": sorted(skipped_runs)})
            print("All 147 runs complete (or skipped). Training is done.",
                  flush=True)
            if manage:
                stop_all_companions(companions)
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

        print(f"  Training PID: {proc.pid}", flush=True)

        # ── Start new companions for this run ────────────────────────────
        if manage:
            companions["training_monitor"] = start_training_monitor(
                training_stdout, ohlc_csv, log_dir)
            companions["sidecar"] = start_sidecar(
                r2_prefix, log_dir, training_stdout)

            run_name = training_stdout.stem
            write_run_manifest(log_dir, run_name, companions, next_run,
                               training_stdout, proc.pid)

        # ── Wait for training with health monitoring ─────────────────────
        try:
            while proc.poll() is None:
                if manage:
                    dead = health_check_companions(companions)
                    for name in dead:
                        log_event(chain_log, {
                            "event": "companion_restarted",
                            "companion": name,
                            "run_number": next_run["run"],
                        })
                        if name == "training_monitor":
                            companions[name] = start_training_monitor(
                                training_stdout, ohlc_csv, log_dir)
                        elif name == "sidecar":
                            companions[name] = start_sidecar(
                                r2_prefix, log_dir, training_stdout)
                time.sleep(HEALTH_CHECK_INTERVAL)
        except KeyboardInterrupt:
            print("\n  KeyboardInterrupt — sending SIGTERM to training...",
                  flush=True)
            proc.terminate()
            proc.wait(timeout=30)
            log_event(chain_log, {"event": "chain_interrupted",
                                  "run_number": next_run["run"]})
            if manage:
                stop_all_companions(companions)
            if training_stdout_fh:
                training_stdout_fh.close()
            return 130

        duration = time.time() - t0
        exit_code = proc.returncode

        if training_stdout_fh:
            training_stdout_fh.close()
            training_stdout_fh = None

        boundary_exists = find_boundary_checkpoint(
            next_run, args.training_output) is not None

        if exit_code == 0 or boundary_exists:
            log_event(chain_log, {
                "event": "run_end",
                "run_number": next_run["run"],
                "dataset": next_run["dataset"],
                "seed": seed_override or next_run["seed"],
                "exit_code": exit_code,
                "duration_s": round(duration, 1),
                "boundary_checkpoint": next_run["checkpoint_step"],
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
    ap.add_argument("--inter-run-pause", type=int, default=90,
                    help="Seconds between runs for watcher upload (default: 90)")
    ap.add_argument("--max-retries", type=int, default=3,
                    help="Max consecutive failures before aborting (default: 3)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would run without executing")
    ap.add_argument("--log-dir", default="/data",
                    help="Directory for companion process logs (default: /data)")
    ap.add_argument("--ohlc-csv", default="/data/training_ohlc.csv",
                    help="Path for OHLC training monitor output")
    ap.add_argument("--r2-ops-prefix", default=R2_CAMPAIGN_PREFIX,
                    help="R2 ops/ prefix for sidecar log mirror")
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
