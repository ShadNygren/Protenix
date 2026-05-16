#!/usr/bin/env python3
"""Watch /data/training_output/*/checkpoints/ for new Protenix checkpoint .pt
files and upload each one to Cloudflare R2 immediately. Both intermediate
checkpoints (saved every checkpoint_interval steps) and end-of-run checkpoints
are uploaded.

Designed for interruptible cloud GPUs (e.g., Salad Low-priority): if the pod
terminates mid-run, the latest checkpoint is already on R2 and training can
resume from R2 on a fresh pod via find_latest_r2_checkpoint.py.

Usage (run as daemon on the pod):
    nohup python3 /data/scripts/checkpoint_watcher.py \
        --env-file /data/.env.cloudflare \
        --runs-root /data/training_output \
        --poll-interval 30 \
        > /data/checkpoint_watcher.log 2>&1 &

Behaviors:
- Uploads each <step>.pt and <step>_ema_0.999.pt as a model+EMA pair only after
  BOTH files are present and their sizes are stable for 2 polls (avoids
  uploading a half-written file).
- Each upload stores sha256 + md5 + run_name + step + category in R2 object
  Metadata.
- Re-runnable / idempotent: skips files whose sha256 already matches what's in
  R2. Tracks uploaded files in /data/checkpoint_watcher_state.json.
- Categorizes runs into idp_only/ vs interleaved/ based on run name pattern.

Stop the daemon with: pkill -f checkpoint_watcher.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from boto3.s3.transfer import TransferConfig

# Optional: secure-checkpoint helpers (lives next to this file)
sys.path.insert(0, str(Path(__file__).parent))
try:
    from secure_checkpoint import (  # type: ignore
        load_dek,
        encrypt_file_in_place,
    )
    _SECURE_AVAILABLE = True
except Exception:  # pyrage may not be installed in older images
    _SECURE_AVAILABLE = False


def load_env(env_file: Path) -> None:
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")


def make_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["CLOUDFLARE_R2_ENDPOINT"],
        aws_access_key_id=os.environ["CLOUDFLARE_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CLOUDFLARE_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4", max_pool_connections=20,
                      retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def hash_file(path: Path) -> tuple[str, str, int]:
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), md5.hexdigest(), size


def existing_sha(s3, bucket: str, key: str) -> str | None:
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        return head.get("Metadata", {}).get("sha256")
    except Exception:
        return None


def categorize(run_name: str) -> str:
    """Determine R2 prefix (idp_only vs interleaved) from run dir name.

    Runs with self.step starting <50000 (the IDP-only era through step 49998)
    go to idp_only/. Everything else (interleaved era from step 50000 onwards)
    goes to interleaved/.
    """
    if run_name.startswith("pdb_block"):
        return "interleaved"
    m = re.search(r"step(\d+)to", run_name)
    if m:
        start = int(m.group(1))
        return "interleaved" if start >= 50000 else "idp_only"
    return "interleaved"


def upload_one(s3, local_path: Path, bucket: str, key: str,
               run_name: str, step: int, category: str) -> dict:
    sha256, md5, size = hash_file(local_path)
    existing = existing_sha(s3, bucket, key)
    if existing == sha256:
        return {"key": key, "size": size, "sha256": sha256, "md5": md5,
                "skipped": True}

    cfg = TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=10,
        use_threads=True,
    )
    t0 = time.time()
    s3.upload_file(
        str(local_path), bucket, key, Config=cfg,
        ExtraArgs={"Metadata": {
            "sha256": sha256, "md5": md5,
            "run_name": run_name, "step": str(step),
            "category": category,
            "src_host": socket.gethostname(),
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }})
    elapsed = time.time() - t0
    rate = (size / 1024 / 1024) / max(elapsed, 0.001)
    head = s3.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != size:
        raise RuntimeError(f"size mismatch on {key}: local={size} r2={head['ContentLength']}")
    return {"key": key, "size": size, "sha256": sha256, "md5": md5,
            "skipped": False, "elapsed_s": round(elapsed, 1),
            "rate_mb_per_s": round(rate, 1)}


def find_pending_pairs(runs_root: Path, sizes_history: dict,
                       state: dict, skip_stability_check: bool = False
                       ) -> list[tuple[Path, Path, str, int]]:
    """Discover all checkpoint pairs (model.pt + ema.pt) whose sizes are stable
    and that have not yet been uploaded according to state.

    skip_stability_check: when True, upload immediately without requiring
    sizes-stable-across-2-polls. Use for --once mode or for backfilling
    older checkpoints whose write completed long ago.
    """
    pending = []
    for run_dir in sorted(runs_root.iterdir()):
        if not run_dir.is_dir():
            continue
        ckpt_dir = run_dir / "checkpoints"
        if not ckpt_dir.is_dir():
            continue
        # Find all <step>.pt files (non-EMA)
        for model_path in sorted(ckpt_dir.glob("*.pt")):
            if "_ema_" in model_path.name:
                continue
            m = re.match(r"(\d+)\.pt$", model_path.name)
            if not m:
                continue
            step = int(m.group(1))
            ema_path = ckpt_dir / f"{step}_ema_0.999.pt"
            if not ema_path.exists():
                continue

            uploaded_key = f"{run_dir.name}/{step}"
            if uploaded_key in state.get("uploaded", {}):
                continue

            if skip_stability_check:
                # Older / completed checkpoints — upload immediately
                pending.append((model_path, ema_path, run_dir.name, step))
                continue

            # Stability check: size should not have changed since last poll
            model_size = model_path.stat().st_size
            ema_size = ema_path.stat().st_size
            prev = sizes_history.get(uploaded_key)
            if prev != (model_size, ema_size):
                sizes_history[uploaded_key] = (model_size, ema_size)
                continue  # wait one more poll for stability
            # Sizes stable across polls — safe to upload
            pending.append((model_path, ema_path, run_dir.name, step))
    return pending


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except Exception:
            pass
    return {"uploaded": {}, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                         time.gmtime())}


def save_state(state_path: Path, state: dict) -> None:
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(state_path)


def _get_cpu_steal_pct() -> float:
    """Read cumulative CPU steal % from /proc/stat."""
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    fields = line.split()
                    if len(fields) >= 9:
                        steal = int(fields[8])
                        total = sum(int(x) for x in fields[1:])
                        return steal / total * 100 if total > 0 else 0.0
    except Exception:
        pass
    return 0.0


def _get_recent_step_rates(run_dir: Path, last_n: int = 50) -> list[float]:
    """Extract recent step rates from training.log tqdm output."""
    log_path = run_dir / "training.log"
    if not log_path.exists():
        return []
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 50000))
            tail = f.read().decode("utf-8", errors="ignore")
        rates = [float(m) for m in re.findall(r"(\d+\.\d+)s/it\]", tail)]
        return rates[-last_n:] if rates else []
    except Exception:
        return []


def _assess_host_quality(
    upload_rate_mbps: float,
    run_dir: Path,
    step: int,
) -> tuple[bool, str]:
    """Assess whether this host should continue training.

    Called after every successful checkpoint upload to R2. The checkpoint
    is safe on R2 at this point, so aborting is safe — we can resume from
    this exact step on a new host.

    Returns (should_abort, reason).

    Configurable via environment:
      WATCHER_MIN_UPLOAD_MBPS: minimum upload speed (default: 5)
      WATCHER_MAX_STEP_RATE: maximum acceptable sec/step (default: 20)
      WATCHER_MAX_STEAL_PCT: maximum CPU steal % (default: 15)
      WATCHER_BASELINE_STEP_RATE: expected step rate on good host (default: 7.0)
      WATCHER_DEGRADATION_RATIO: abort if current/baseline > this (default: 2.5)
    """
    min_upload = float(os.environ.get("WATCHER_MIN_UPLOAD_MBPS", "5"))
    max_step_rate = float(os.environ.get("WATCHER_MAX_STEP_RATE", "20"))
    max_steal = float(os.environ.get("WATCHER_MAX_STEAL_PCT", "15"))
    baseline_rate = float(os.environ.get("WATCHER_BASELINE_STEP_RATE", "7.0"))
    max_ratio = float(os.environ.get("WATCHER_DEGRADATION_RATIO", "2.5"))

    reasons = []

    # Check 1: Upload bandwidth too low (indicates poor network)
    if upload_rate_mbps > 0 and upload_rate_mbps < min_upload:
        reasons.append(f"upload_bandwidth={upload_rate_mbps:.1f} MB/s < {min_upload} MB/s threshold")

    # Check 2: Recent step rate degraded
    recent_rates = _get_recent_step_rates(run_dir, last_n=50)
    if recent_rates:
        median_rate = sorted(recent_rates)[len(recent_rates) // 2]
        if median_rate > max_step_rate:
            reasons.append(f"step_rate={median_rate:.1f}s > {max_step_rate}s absolute max")
        elif baseline_rate > 0 and median_rate / baseline_rate > max_ratio:
            reasons.append(f"step_rate={median_rate:.1f}s is {median_rate/baseline_rate:.1f}x baseline ({baseline_rate}s)")

    # Check 3: CPU steal time (host owner's workload competing)
    steal_pct = _get_cpu_steal_pct()
    if steal_pct > max_steal:
        reasons.append(f"cpu_steal={steal_pct:.1f}% > {max_steal}% (host owner active)")

    # Decision: abort only if multiple signals agree, or one is extreme
    if len(reasons) >= 2:
        return True, " + ".join(reasons)
    elif reasons and ("absolute max" in reasons[0] or steal_pct > 30):
        return True, reasons[0]
    else:
        if reasons:
            print(f"  [quality] warning (not aborting): {reasons[0]}", flush=True)
        return False, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", type=Path, default=Path("/data/.env.cloudflare"))
    ap.add_argument("--bucket", default="vh-protenix-training")
    ap.add_argument("--runs-root", type=Path, default=Path("/data/training_output"))
    ap.add_argument("--state-file", type=Path,
                    default=Path("/data/checkpoint_watcher_state.json"))
    ap.add_argument("--poll-interval", type=int, default=30,
                    help="Seconds between polls")
    ap.add_argument("--once", action="store_true",
                    help="Do one pass then exit (useful for cron / one-shot)")
    ap.add_argument("--prefix-override", default=None,
                    help="If set, all uploads go to "
                         "checkpoints/<prefix-override>/<run_name>/<step>.pt "
                         "regardless of the auto-categorize() classification. "
                         "Use for non-production environments (e.g., Salad "
                         "testing nodes) where ALL output must be quarantined "
                         "in a known prefix for later cleanup. Examples: "
                         "--prefix-override salad_testing/<node-id>")
    ap.add_argument("--heartbeat", action="store_true",
                    help="Write a heartbeat JSON to R2 every poll cycle. "
                         "Key derived from --prefix-override (e.g., "
                         "ops/salad_testing/<node-id>/heartbeat.json) so a "
                         "post-mortem session can tell when the watcher last "
                         "ran even if the container is gone. Recommended for "
                         "interruptible cloud nodes.")
    ap.add_argument("--heartbeat-key", default=None,
                    help="Override the auto-derived heartbeat key. If unset "
                         "and --heartbeat is on, defaults to "
                         "ops/<prefix-override or hostname>/heartbeat.json")
    ap.add_argument("--enforce-naming", action="store_true",
                    help="Validate every run_name against SEED_CONVENTION.md "
                         "regex before uploading. Non-compliant names are "
                         "STILL uploaded (we never lose data) but a record is "
                         "appended to R2 at ops/<prefix>/naming_violations.jsonl "
                         "for human review. See SEED_CONVENTION.md sections "
                         "'Run naming for resumed runs' and the chain script "
                         "patterns for the canonical forms.")
    args = ap.parse_args()

    load_env(args.env_file)
    s3 = make_s3_client()
    print(f"=== Checkpoint watcher started ===", flush=True)
    print(f"  R2 bucket: {args.bucket}", flush=True)
    print(f"  Runs root: {args.runs_root}", flush=True)
    print(f"  State:     {args.state_file}", flush=True)
    print(f"  Poll:      {args.poll_interval}s", flush=True)

    state = load_state(args.state_file)
    sizes_history: dict[str, tuple[int, int]] = {}

    # === Heartbeat setup ===
    # Writing a small JSON object to R2 on every poll cycle gives a definitive
    # "watcher was alive at T" signal that survives the container's death. If
    # the next session sees heartbeat.json's mtime ages indefinitely, they know
    # the watcher (and the box) died at that timestamp — not 30s before, but
    # within poll_interval of it.
    heartbeat_key: str | None = None
    if args.heartbeat:
        if args.heartbeat_key:
            heartbeat_key = args.heartbeat_key
        elif args.prefix_override:
            heartbeat_key = f"ops/{args.prefix_override}/heartbeat.json"
        else:
            heartbeat_key = f"ops/{socket.gethostname()}/heartbeat.json"
        print(f"[watcher] heartbeat ENABLED → s3://{args.bucket}/{heartbeat_key}", flush=True)

    # === Optional disk encryption after successful R2 upload ===
    # Encryption gives us a recovery copy that's safe-at-rest on the host SSD.
    # Salad /workspace is wiped on container restart so this is mostly defense
    # against in-flight forensic capture during the container's lifetime, not
    # post-mortem analysis. Controlled by PROTENIX_ENCRYPT_LOCAL_CHECKPOINTS
    # (default true) and gated on actually having a DEK.
    dek = None
    dek_source = None
    encrypt_enabled = os.environ.get("PROTENIX_ENCRYPT_LOCAL_CHECKPOINTS", "true").lower() == "true"
    if encrypt_enabled and _SECURE_AVAILABLE:
        try:
            dek, dek_source = load_dek()
        except SystemExit:
            raise
        except Exception as e:
            print(f"[watcher] load_dek failed: {e} (continuing without encryption)", flush=True)
    if dek:
        print(f"[watcher] post-upload encryption ENABLED (DEK source: {dek_source})", flush=True)
    elif encrypt_enabled and _SECURE_AVAILABLE:
        print(f"[watcher] no DEK available — checkpoints will NOT be encrypted on disk", flush=True)
    elif not encrypt_enabled:
        print(f"[watcher] PROTENIX_ENCRYPT_LOCAL_CHECKPOINTS=false — encryption disabled", flush=True)
    else:
        print(f"[watcher] secure_checkpoint module unavailable (older image?) — no encryption", flush=True)

    last_uploaded_step = max(
        (int(k.rsplit("/", 1)[-1]) for k in state.get("uploaded", {}).keys()
         if k.rsplit("/", 1)[-1].isdigit()),
        default=None,
    )

    # === Naming-convention enforcement ===
    # The de facto canonical form has TWO timestamps:
    #   <run_type>_seed<N>_<TS_launch>_<TS_protenix_init>[_resume<R>_<TS>]
    #
    # TS_launch: stamp embedded by our chain/launch script (date -u when the
    #            shell wrote the RUN_NAME variable)
    # TS_protenix_init: stamp Protenix's runner/train.py:384 appends when it
    #            finishes argparse + CUDA init and calls "Using run name: ...".
    #            Typically TS_launch + 5-10 seconds; growing delta over time
    #            signals image-load slowdown.
    #
    # SEED_CONVENTION.md shows the SINGLE-TS form (TS_launch only) as the
    # "intended" name — that's the name our scripts CONSTRUCT. The on-disk
    # and R2 canonical form is ALWAYS double-TS because Protenix appends one.
    # Both timestamps are different and informative; do not collapse them.
    #
    # Watcher policy: warn if _seed<N>_ is missing OR if the TS pattern is
    # broken. Never block uploads — record violations to R2 for audit.
    # Single canonical pattern. Run name MUST start with one of two prefixes
    # and contain _seed<N>_. After that, any sequence of _<TS> markers (each
    # YYYYMMDD_HHMMSS) and/or _resume<R> tokens is allowed. Optional .tainted
    # suffix is honored.
    NAMING_REGEXES = [
        re.compile(
            r"^"
            r"(idp_v2_fresh_step\d+(to\d+)?|pdb_block\d{2}_run\d+)"  # prefix
            r"_seed\d+"                                                # seed required
            r"(_\d{8}_\d{6}|_resume\d+)*"                              # any TS or resume markers
            r"(\.tainted)?"                                            # optional taint flag
            r"$"
        ),
    ]
    violations_reported: set[str] = set()

    while True:
        # Heartbeat: written BEFORE the poll work, so even if the poll throws
        # we still recorded "watcher was alive at T". The JSON body includes
        # last-uploaded step + loadavg so an observer can correlate watcher
        # liveness with training progress + system load.
        if heartbeat_key:
            try:
                with open("/proc/loadavg") as fh:
                    loadavg = fh.read().strip()
            except OSError:
                loadavg = "unavailable"
            heartbeat = {
                "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "watcher_pid": os.getpid(),
                "hostname": socket.gethostname(),
                "prefix_override": args.prefix_override,
                "last_uploaded_step": last_uploaded_step,
                "loadavg": loadavg,
                "poll_interval_s": args.poll_interval,
            }
            try:
                s3.put_object(
                    Bucket=args.bucket,
                    Key=heartbeat_key,
                    Body=json.dumps(heartbeat, indent=2).encode(),
                    ContentType="application/json",
                )
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] heartbeat write FAILED: {e}", flush=True)

        try:
            pending = find_pending_pairs(args.runs_root, sizes_history, state,
                                         skip_stability_check=args.once)
            for model_path, ema_path, run_name, step in pending:
                # Per-run naming check (once per run, not per checkpoint)
                if args.enforce_naming and run_name not in violations_reported:
                    if not any(rx.match(run_name) for rx in NAMING_REGEXES):
                        print(f"[{time.strftime('%H:%M:%S')}] NAMING VIOLATION: "
                              f"run_name '{run_name}' does not match "
                              f"SEED_CONVENTION.md patterns. Uploading anyway.",
                              flush=True)
                        violations_reported.add(run_name)
                        viol = {
                            "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "run_name": run_name,
                            "expected_patterns": [rx.pattern for rx in NAMING_REGEXES],
                            "hostname": socket.gethostname(),
                            "prefix_override": args.prefix_override,
                        }
                        viol_key_prefix = (args.prefix_override
                                           or socket.gethostname())
                        try:
                            # Append to a JSONL: download existing, add line, re-upload
                            viol_key = f"ops/{viol_key_prefix}/naming_violations.jsonl"
                            try:
                                existing = s3.get_object(Bucket=args.bucket, Key=viol_key)["Body"].read().decode()
                            except Exception:
                                existing = ""
                            new_body = existing + json.dumps(viol) + "\n"
                            s3.put_object(Bucket=args.bucket, Key=viol_key,
                                          Body=new_body.encode(),
                                          ContentType="application/x-ndjson")
                        except Exception as e:
                            print(f"  ! failed to record naming violation to R2: {e}", flush=True)
                if args.prefix_override:
                    category = args.prefix_override
                else:
                    category = categorize(run_name)
                base_key = f"checkpoints/{category}/{run_name}"
                print(f"[{time.strftime('%H:%M:%S')}] Uploading {run_name}/{step} ({category})...", flush=True)
                m_result = upload_one(s3, model_path, args.bucket,
                                      f"{base_key}/{step}.pt",
                                      run_name, step, category)
                e_result = upload_one(s3, ema_path, args.bucket,
                                      f"{base_key}/{step}_ema_0.999.pt",
                                      run_name, step, category)
                # Post-upload encryption: replace cleartext .pt files on local
                # disk with .pt.age blobs. Only on successful upload (so we
                # never lose data — R2 has the canonical copy).
                enc_result = {}
                if dek and not m_result.get("skipped"):
                    try:
                        enc_model = encrypt_file_in_place(model_path, dek, delete_original=True)
                        enc_result["model_age"] = str(enc_model)
                    except Exception as e:
                        print(f"  ! encrypt failed for {model_path.name}: {e}", flush=True)
                if dek and not e_result.get("skipped"):
                    try:
                        enc_ema = encrypt_file_in_place(ema_path, dek, delete_original=True)
                        enc_result["ema_age"] = str(enc_ema)
                    except Exception as e:
                        print(f"  ! encrypt failed for {ema_path.name}: {e}", flush=True)
                key = f"{run_name}/{step}"
                state["uploaded"][key] = {
                    "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime()),
                    "category": category,
                    "model": m_result,
                    "ema": e_result,
                    **({"encrypted": enc_result} if enc_result else {}),
                }
                save_state(args.state_file, state)
                if last_uploaded_step is None or step > last_uploaded_step:
                    last_uploaded_step = step
                m_skip = "SKIP" if m_result.get("skipped") else f"{m_result.get('rate_mb_per_s', 0)} MB/s"
                e_skip = "SKIP" if e_result.get("skipped") else f"{e_result.get('rate_mb_per_s', 0)} MB/s"
                enc_note = " + encrypted local copy" if enc_result else ""
                print(f"  ✓ {step}.pt {m_skip}, {step}_ema {e_skip}{enc_note}", flush=True)

                # === Post-checkpoint host quality assessment ===
                # Now that the checkpoint is safely on R2, assess whether this
                # host is still performing well enough to continue training.
                # Signals: upload bandwidth, step rate trend, CPU steal time.
                # If the host is degraded, abort so the platform reallocates.
                if not args.once and os.environ.get("WATCHER_QUALITY_CHECK", "true").lower() == "true":
                    try:
                        should_abort, abort_reason = _assess_host_quality(
                            upload_rate_mbps=m_result.get("rate_mb_per_s", 0),
                            run_dir=model_path.parent.parent,
                            step=step,
                        )
                        if should_abort:
                            print(f"[{time.strftime('%H:%M:%S')}] HOST QUALITY DEGRADED after "
                                  f"checkpoint {step} upload confirmed on R2.", flush=True)
                            print(f"  Reason: {abort_reason}", flush=True)
                            print(f"  Action: exiting to trigger platform reallocation.", flush=True)
                            print(f"  Resume: next container loads {step}.pt from R2 automatically.", flush=True)
                            # Log the abort decision to R2
                            abort_record = {
                                "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "hostname": socket.gethostname(),
                                "last_step": step,
                                "reason": abort_reason,
                                "run_name": run_name,
                            }
                            abort_key_prefix = args.prefix_override or socket.gethostname()
                            try:
                                s3.put_object(
                                    Bucket=args.bucket,
                                    Key=f"ops/{abort_key_prefix}/host_abort_{step}.json",
                                    Body=json.dumps(abort_record, indent=2).encode(),
                                    ContentType="application/json",
                                )
                            except Exception:
                                pass
                            sys.exit(1)
                    except Exception as e:
                        print(f"  ! quality check error (non-fatal): {e}", flush=True)

        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] ERROR: {e}", flush=True)

        if args.once:
            break
        time.sleep(args.poll_interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
