#!/usr/bin/env python3
"""Check if an R2 object exists. Exit 0 if YES, 2 if NO, 1 on error.

Used by chain_interleaved_runs.sh's cleanup_intermediates() to verify R2 has
a checkpoint before deleting it locally.

Usage:
    python3 r2_object_exists.py \
        --env-file /data/.env.cloudflare \
        --bucket vh-protenix-training \
        --key checkpoints/interleaved/run_name/49998.pt
"""
import argparse
import os
import sys
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", type=Path, required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--key", required=True)
    args = ap.parse_args()

    load_env(args.env_file)
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["CLOUDFLARE_R2_ENDPOINT"],
        aws_access_key_id=os.environ["CLOUDFLARE_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CLOUDFLARE_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    try:
        s3.head_object(Bucket=args.bucket, Key=args.key)
        return 0  # YES
    except s3.exceptions.ClientError as e:
        code = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code == 404:
            return 2  # NO
        print(f"ERROR: {e}", file=sys.stderr)
        return 1  # error


if __name__ == "__main__":
    sys.exit(main())
