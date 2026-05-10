#!/usr/bin/env python3
"""Find the latest Protenix training checkpoint in R2.

Disaster-recovery utility for interruptible cloud GPUs (e.g., Salad
Low-priority). When a fresh pod comes up after the previous one was destroyed,
this script tells you exactly which R2 object to resume from.

What "latest" means:
- Lists every <step>.pt under s3://vh-protenix-training/checkpoints/
- Picks the maximum-numbered step across both idp_only/ and interleaved/
- Returns its R2 key plus matching _ema_0.999.pt key
- Optionally downloads the pair to a local path

Usage:
    # Just print the latest:
    python3 find_latest_r2_checkpoint.py --env-file /data/.env.cloudflare

    # Download the latest to a local dir:
    python3 find_latest_r2_checkpoint.py --env-file /data/.env.cloudflare \
        --download-to /data/resume_ckpt

Output (machine-readable JSON to stdout):
    {
      "latest_step": 74998,
      "category": "interleaved",
      "run_name": "...",
      "model_key": "checkpoints/interleaved/.../74998.pt",
      "ema_key":   "checkpoints/interleaved/.../74998_ema_0.999.pt",
      "model_size": 4427191403,
      "ema_size":   4427422239,
      "model_sha256": "...",
      "ema_sha256":   "..."
    }
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config
from boto3.s3.transfer import TransferConfig


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
        config=Config(signature_version="s3v4", max_pool_connections=20),
    )


def list_all_checkpoints(s3, bucket: str) -> list[dict]:
    """Return entries for every <step>.pt (non-EMA) in checkpoints/."""
    paginator = s3.get_paginator("list_objects_v2")
    out: list[dict] = []
    for prefix in ("checkpoints/idp_only/", "checkpoints/interleaved/"):
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                m = re.match(rf"{re.escape(prefix)}([^/]+)/(\d+)\.pt$", key)
                if not m:
                    continue
                run_name = m.group(1)
                step = int(m.group(2))
                category = prefix.split("/")[1]
                out.append({
                    "category": category,
                    "run_name": run_name,
                    "step": step,
                    "model_key": key,
                    "model_size": obj["Size"],
                    "model_etag": obj["ETag"].strip('"'),
                })
    return out


def fetch_metadata(s3, bucket: str, key: str) -> dict:
    h = s3.head_object(Bucket=bucket, Key=key)
    return h.get("Metadata", {}) or {}


def download_with_verify(s3, bucket: str, key: str, dest: Path) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cfg = TransferConfig(multipart_threshold=64 * 1024 * 1024,
                         multipart_chunksize=64 * 1024 * 1024,
                         max_concurrency=10)
    print(f"  Downloading {key} → {dest} ...", flush=True)
    t0 = time.time()
    s3.download_file(bucket, key, str(dest), Config=cfg)
    elapsed = time.time() - t0
    size = dest.stat().st_size
    rate = size / 1024 / 1024 / max(elapsed, 0.001)
    print(f"  Done in {elapsed:.0f}s ({rate:.1f} MB/s)", flush=True)

    # Verify sha256 against R2 metadata
    import hashlib
    sha = hashlib.sha256()
    with open(dest, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            sha.update(chunk)
    local_sha = sha.hexdigest()
    meta = fetch_metadata(s3, bucket, key)
    r2_sha = meta.get("sha256")
    if r2_sha and r2_sha != local_sha:
        raise RuntimeError(f"sha256 mismatch on {key}: R2 metadata says {r2_sha}, local computed {local_sha}")
    print(f"  sha256 verified: {local_sha[:16]}...", flush=True)
    return {"path": str(dest), "size": size, "sha256": local_sha}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", type=Path, default=Path("/data/.env.cloudflare"))
    ap.add_argument("--bucket", default="vh-protenix-training")
    ap.add_argument("--download-to", type=Path,
                    help="If set, download the latest model+ema pair into this dir")
    args = ap.parse_args()

    load_env(args.env_file)
    s3 = make_s3_client()

    entries = list_all_checkpoints(s3, args.bucket)
    if not entries:
        print(json.dumps({"error": "no checkpoints found in R2"}), file=sys.stderr)
        return 1

    entries.sort(key=lambda e: e["step"])
    latest = entries[-1]
    step = latest["step"]
    category = latest["category"]
    run_name = latest["run_name"]
    base_key = f"checkpoints/{category}/{run_name}"
    ema_key = f"{base_key}/{step}_ema_0.999.pt"

    try:
        ema_head = s3.head_object(Bucket=args.bucket, Key=ema_key)
        ema_size = ema_head["ContentLength"]
    except Exception as e:
        print(json.dumps({"error": f"EMA not found: {ema_key}: {e}"}),
              file=sys.stderr)
        return 1

    model_meta = fetch_metadata(s3, args.bucket, latest["model_key"])
    ema_meta = fetch_metadata(s3, args.bucket, ema_key)

    result: dict = {
        "latest_step": step,
        "category": category,
        "run_name": run_name,
        "model_key": latest["model_key"],
        "ema_key": ema_key,
        "model_size": latest["model_size"],
        "ema_size": ema_size,
        "model_sha256": model_meta.get("sha256"),
        "ema_sha256": ema_meta.get("sha256"),
    }

    if args.download_to:
        args.download_to.mkdir(parents=True, exist_ok=True)
        model_dest = args.download_to / f"{step}.pt"
        ema_dest = args.download_to / f"{step}_ema_0.999.pt"
        result["model_local"] = download_with_verify(s3, args.bucket,
                                                     latest["model_key"],
                                                     model_dest)
        result["ema_local"] = download_with_verify(s3, args.bucket,
                                                   ema_key, ema_dest)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
