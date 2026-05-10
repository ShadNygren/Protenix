#!/usr/bin/env python3
"""Upload discarded/abandoned/flawed experimental artifacts to a R2 archive
prefix. Required by CLAUDE.md "NEVER throw away experimental results or
artifacts" rule.

Per-object metadata recorded in R2:
- sha256, md5 (chain of custody)
- archive_reason (why was this experiment flagged as flawed)
- original_experiment_dates (when was the original work done)
- src_path, src_host (original location)
- archived_at (when did we move it to archive)

Usage:
    python3 archive_discarded_artifacts.py \
        --env-file /data/.env.cloudflare \
        --bucket vh-protenix-training \
        --prefix archive/discarded_april28_29_seed42_bug/ \
        --reason "4-fold_CV_seed42_sampling_overlap_bug" \
        --dates "2026-04-28_to_2026-04-29" \
        --why-md /path/to/WHY_ARCHIVED.md \
        --files /path/to/log1.log /path/to/log2.log ...
"""
from __future__ import annotations

import argparse
import hashlib
import os
import socket
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config


def load_env(env_file: Path) -> None:
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")


def make_s3():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["CLOUDFLARE_R2_ENDPOINT"],
        aws_access_key_id=os.environ["CLOUDFLARE_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CLOUDFLARE_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def hash_file(path: Path) -> tuple[str, str]:
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", type=Path, required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--prefix", required=True,
                    help="R2 key prefix (e.g., archive/discarded_X/)")
    ap.add_argument("--reason", required=True,
                    help="Short tag describing why archived")
    ap.add_argument("--dates", default="",
                    help="Original experiment date range (e.g., 2026-04-28_to_2026-04-29)")
    ap.add_argument("--why-md", type=Path,
                    help="Path to a WHY_ARCHIVED.md to upload alongside")
    ap.add_argument("--files", nargs="+", type=Path, required=True,
                    help="Files to archive")
    args = ap.parse_args()

    if not args.prefix.endswith("/"):
        args.prefix += "/"

    load_env(args.env_file)
    s3 = make_s3()
    archived_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    host = socket.gethostname()

    for src in args.files:
        if not src.exists():
            print(f"  SKIP (missing): {src}", file=sys.stderr)
            continue
        sha, md5 = hash_file(src)
        key = args.prefix + src.name
        s3.upload_file(str(src), args.bucket, key, ExtraArgs={"Metadata": {
            "sha256": sha, "md5": md5,
            "src_path": str(src), "src_host": host,
            "archived_at": archived_at,
            "archive_reason": args.reason,
            "original_experiment_dates": args.dates,
        }})
        print(f"  UP {key} ({src.stat().st_size} bytes  sha256={sha[:12]}...)")

    if args.why_md and args.why_md.exists():
        sha, md5 = hash_file(args.why_md)
        key = args.prefix + "WHY_ARCHIVED.md"
        s3.upload_file(str(args.why_md), args.bucket, key, ExtraArgs={"Metadata": {
            "sha256": sha, "md5": md5,
            "src_path": str(args.why_md), "src_host": host,
            "archived_at": archived_at,
            "archive_reason": args.reason,
            "original_experiment_dates": args.dates,
        }})
        print(f"  UP {key}")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
