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

    while True:
        try:
            pending = find_pending_pairs(args.runs_root, sizes_history, state,
                                         skip_stability_check=args.once)
            for model_path, ema_path, run_name, step in pending:
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
                m_skip = "SKIP" if m_result.get("skipped") else f"{m_result.get('rate_mb_per_s', 0)} MB/s"
                e_skip = "SKIP" if e_result.get("skipped") else f"{e_result.get('rate_mb_per_s', 0)} MB/s"
                enc_note = " + encrypted local copy" if enc_result else ""
                print(f"  ✓ {step}.pt {m_skip}, {step}_ema {e_skip}{enc_note}", flush=True)
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] ERROR: {e}", flush=True)

        if args.once:
            break
        time.sleep(args.poll_interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
