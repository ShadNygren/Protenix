#!/usr/bin/env python3
"""Stage training data from R2/S3/RunPod to local disk before a Protenix training run.

Downloads checkpoint, bioassemblies, and index files to a local staging
directory so run_salad_training.sh / launch_training.sh can find them.

URI scheme determines which storage backend to use — each routes to a
different S3-compatible endpoint with its own credentials:

  r2://bucket/key      → Cloudflare R2       (CLOUDFLARE_R2_* env vars)
  s3://bucket/key      → AWS S3              (default boto3 credential chain)
  runpod://bucket/key  → RunPod Network Vol  (RUNPOD_S3_* env vars)
  file:///path         → local copy          (or /path without scheme)

Despite all being S3-API-compatible, these are DIFFERENT endpoints with
DIFFERENT credentials. Using the wrong scheme gives "Bucket Not Found"
even when the bucket exists on another provider.

Credentials come from env vars or from a shell-export creds file
(--creds, default /dev/shm/secure/creds):

  Cloudflare R2:  CLOUDFLARE_R2_ACCESS_KEY_ID, CLOUDFLARE_R2_SECRET_ACCESS_KEY,
                  CLOUDFLARE_R2_ENDPOINT (or CLOUDFLARE_ACCOUNT_ID)
  AWS S3:         Default boto3 chain (AWS_ACCESS_KEY_ID, ~/.aws/credentials, etc.)
  RunPod S3:      RUNPOD_S3_ACCESS_KEY_ID, RUNPOD_S3_SECRET_ACCESS_KEY,
                  RUNPOD_S3_ENDPOINT (e.g. https://s3api-us-ks-2.runpod.io)

Usage:
    stage_training_data.py \\
        --checkpoint-uri  r2://vh-protenix-training/base_model/protenix_base_20250630_v1.0.0.pt \\
        --bioassembly-uri r2://vh-pdb-structures/bioassembly_crop384/idp_set.zip \\
        --indices-uri     r2://vh-protenix-training/data/idp_v2/train_fold1.csv \\
        --pdb-list-uri    r2://vh-protenix-training/data/idp_v2/train_all_pdb_ids.txt \\
        --staging-dir     /workspace/training_data

    Or via env vars (for use in PROTENIX_STARTUP_SCRIPT):
        STAGE_CHECKPOINT_URI=r2://vh-protenix-training/base_model/...
        STAGE_BIOASSEMBLY_URI=r2://vh-pdb-structures/bioassembly_crop384/idp_set.zip
        STAGE_INDICES_URI=r2://vh-protenix-training/data/idp_v2/train_fold1.csv
        STAGE_PDB_LIST_URI=r2://vh-protenix-training/data/idp_v2/train_all_pdb_ids.txt
        STAGE_EMA_URI=  (optional, empty = no EMA for first run from base model)
        STAGE_DIR=/workspace/training_data

    RunPod network volume example:
        STAGE_CHECKPOINT_URI=runpod://puq69bsg3u/checkpoints/4998.pt

Output:
    <staging-dir>/
    ├── checkpoints/
    │   ├── <checkpoint_filename>      (e.g. protenix_base_20250630_v1.0.0.pt)
    │   └── <ema_filename>             (if --ema-uri provided)
    ├── bioassembly/
    │   ├── 10ij.pkl.gz
    │   └── ...                        (extracted from ZIP)
    └── indices/
        ├── <indices_filename>         (e.g. train_fold1.csv)
        └── <pdb_list_filename>        (e.g. train_all_pdb_ids.txt)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


SUPPORTED_SCHEMES = ("r2", "s3", "runpod", "file")


@dataclass
class ParsedURI:
    scheme: str  # "r2", "s3", "runpod", "file"
    bucket: str  # empty for file://
    key: str     # full key for s3/r2/runpod, full path for file


def parse_uri(uri: str) -> ParsedURI:
    """Parse a URI into (scheme, bucket, key).

    r2://bucket/path/to/key      → ParsedURI("r2",     "bucket", "path/to/key")
    s3://bucket/path/to/key      → ParsedURI("s3",     "bucket", "path/to/key")
    runpod://bucket/path/to/key  → ParsedURI("runpod", "bucket", "path/to/key")
    file:///absolute/path        → ParsedURI("file",   "",       "/absolute/path")
    /absolute/path               → ParsedURI("file",   "",       "/absolute/path")
    """
    for scheme in ("r2://", "runpod://", "s3://"):
        if uri.startswith(scheme):
            rest = uri[len(scheme):]
            bucket, _, key = rest.partition("/")
            return ParsedURI(scheme.rstrip(":/"), bucket, key)
    if uri.startswith("file://"):
        return ParsedURI("file", "", uri[len("file://"):])
    if uri.startswith("/"):
        return ParsedURI("file", "", uri)
    raise ValueError(
        f"Unsupported URI scheme: {uri!r}. "
        f"Expected one of: {', '.join(s + '://' for s in SUPPORTED_SCHEMES)}, or /path"
    )


def parse_creds_file(path: Path) -> dict[str, str]:
    """Parse a shell-export `export FOO="bar"` file into a dict."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        kv = line.split("=", 1)
        if len(kv) != 2:
            continue
        k, v = kv[0].strip(), kv[1].strip().strip('"').strip("'")
        out[k] = v
    return out


def get_r2_creds(creds_file: Path) -> tuple[str, str, str]:
    """Return (endpoint, access_key, secret_key) for R2, checking env + creds file."""
    file_creds = parse_creds_file(creds_file)

    endpoint = os.environ.get("CLOUDFLARE_R2_ENDPOINT") or file_creds.get("CLOUDFLARE_R2_ENDPOINT")
    access_key = os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID") or file_creds.get("CLOUDFLARE_R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY") or file_creds.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY")

    if not endpoint:
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or file_creds.get("CLOUDFLARE_ACCOUNT_ID")
        if account_id:
            endpoint = f"https://{account_id}.r2.cloudflarestorage.com"

    if not all([endpoint, access_key, secret_key]):
        missing = []
        if not endpoint:
            missing.append("CLOUDFLARE_R2_ENDPOINT")
        if not access_key:
            missing.append("CLOUDFLARE_R2_ACCESS_KEY_ID")
        if not secret_key:
            missing.append("CLOUDFLARE_R2_SECRET_ACCESS_KEY")
        raise RuntimeError(
            f"Missing R2 credentials: {', '.join(missing)}. "
            f"Set via env vars or in {creds_file}"
        )
    return endpoint, access_key, secret_key


def get_runpod_creds(creds_file: Path) -> tuple[str, str, str, str]:
    """Return (endpoint, access_key, secret_key, region) for RunPod S3."""
    file_creds = parse_creds_file(creds_file)

    endpoint = os.environ.get("RUNPOD_S3_ENDPOINT") or file_creds.get("RUNPOD_S3_ENDPOINT")
    access_key = os.environ.get("RUNPOD_S3_ACCESS_KEY_ID") or file_creds.get("RUNPOD_S3_ACCESS_KEY_ID")
    secret_key = os.environ.get("RUNPOD_S3_SECRET_ACCESS_KEY") or file_creds.get("RUNPOD_S3_SECRET_ACCESS_KEY")
    region = os.environ.get("RUNPOD_S3_REGION") or file_creds.get("RUNPOD_S3_REGION", "")

    if not region and endpoint:
        # Extract region from endpoint URL: https://s3api-us-ks-2.runpod.io → us-ks-2
        import re
        m = re.search(r"s3api-([^.]+)\.runpod\.io", endpoint)
        if m:
            region = m.group(1)

    if not all([endpoint, access_key, secret_key]):
        missing = []
        if not endpoint:
            missing.append("RUNPOD_S3_ENDPOINT")
        if not access_key:
            missing.append("RUNPOD_S3_ACCESS_KEY_ID")
        if not secret_key:
            missing.append("RUNPOD_S3_SECRET_ACCESS_KEY")
        raise RuntimeError(
            f"Missing RunPod S3 credentials: {', '.join(missing)}. "
            f"Set via env vars or in {creds_file}"
        )
    return endpoint, access_key, secret_key, region or "us-east-1"


def make_s3_client(parsed: ParsedURI, creds_file: Path):
    """Create a boto3 S3 client routed to the correct backend by URI scheme."""
    import boto3
    from botocore.config import Config

    if parsed.scheme == "r2":
        endpoint, access_key, secret_key = get_r2_creds(creds_file)
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
    elif parsed.scheme == "runpod":
        endpoint, access_key, secret_key, region = get_runpod_creds(creds_file)
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version="s3v4"),
        )
    elif parsed.scheme == "s3":
        return boto3.client("s3")
    else:
        raise ValueError(f"Cannot create S3 client for scheme: {parsed.scheme}")


def download_object(uri: str, dest: Path, creds_file: Path) -> Path:
    """Download a single object from R2/S3/local to dest.

    Returns the path to the downloaded file.
    """
    parsed = parse_uri(uri)

    if parsed.scheme == "file":
        src = Path(parsed.key)
        if not src.exists():
            raise FileNotFoundError(f"Local file not found: {src}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src == dest:
            return dest
        shutil.copy2(src, dest)
        return dest

    client = make_s3_client(parsed, creds_file)
    dest.parent.mkdir(parents=True, exist_ok=True)

    obj_size = client.head_object(Bucket=parsed.bucket, Key=parsed.key)["ContentLength"]
    print(f"  downloading {uri} ({obj_size / 1_048_576:.1f} MB) → {dest}")

    t0 = time.monotonic()
    client.download_file(parsed.bucket, parsed.key, str(dest))
    elapsed = time.monotonic() - t0

    actual_size = dest.stat().st_size
    if actual_size != obj_size:
        raise RuntimeError(
            f"Size mismatch: expected {obj_size} bytes, got {actual_size} bytes"
        )

    rate_mbps = (actual_size / 1_048_576) / elapsed if elapsed > 0 else 0
    print(f"  done in {elapsed:.1f}s ({rate_mbps:.1f} MB/s), verified {actual_size} bytes")
    return dest


def download_and_extract_zip(uri: str, extract_dir: Path, creds_file: Path) -> int:
    """Download a ZIP from R2/S3 and extract to extract_dir. Returns file count."""
    parsed = parse_uri(uri)
    filename = parsed.key.rsplit("/", 1)[-1] if "/" in parsed.key else parsed.key
    tmp_zip = extract_dir.parent / f".tmp_{filename}"

    try:
        download_object(uri, tmp_zip, creds_file)

        extract_dir.mkdir(parents=True, exist_ok=True)
        print(f"  extracting {tmp_zip.name} → {extract_dir}")
        t0 = time.monotonic()
        result = subprocess.run(
            ["unzip", "-q", "-o", str(tmp_zip), "-d", str(extract_dir)],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(f"unzip failed (rc={result.returncode}): {result.stderr[:500]}")

        elapsed = time.monotonic() - t0
        file_count = sum(1 for _ in extract_dir.rglob("*.pkl.gz"))
        print(f"  extracted {file_count} .pkl.gz files in {elapsed:.1f}s")
        return file_count
    finally:
        if tmp_zip.exists():
            tmp_zip.unlink()
            print(f"  cleaned up {tmp_zip.name}")


def stage_checkpoint(args: argparse.Namespace) -> dict[str, str]:
    """Stage checkpoint (and optional EMA) to local disk. Returns path dict."""
    ckpt_dir = args.staging_dir / "checkpoints"
    result = {}

    parsed = parse_uri(args.checkpoint_uri)
    filename = parsed.key.rsplit("/", 1)[-1] if parsed.scheme != "file" else Path(parsed.key).name
    dest = ckpt_dir / filename

    print(f"\n[stage] checkpoint: {args.checkpoint_uri}")
    download_object(args.checkpoint_uri, dest, args.creds)
    result["checkpoint"] = str(dest)

    if args.ema_uri:
        parsed_ema = parse_uri(args.ema_uri)
        ema_filename = parsed_ema.key.rsplit("/", 1)[-1] if parsed_ema.scheme != "file" else Path(parsed_ema.key).name
        ema_dest = ckpt_dir / ema_filename
        print(f"\n[stage] EMA checkpoint: {args.ema_uri}")
        download_object(args.ema_uri, ema_dest, args.creds)
        result["ema"] = str(ema_dest)
    else:
        print("\n[stage] no EMA URI provided (normal for first run from base model)")

    return result


def stage_bioassembly(args: argparse.Namespace) -> dict[str, str]:
    """Stage bioassembly data. Handles ZIPs (extract) and dirs (skip)."""
    bio_dir = args.staging_dir / "bioassembly"
    result = {}

    parsed = parse_uri(args.bioassembly_uri)
    is_zip = parsed.key.endswith(".zip") if parsed.scheme != "file" else args.bioassembly_uri.endswith(".zip")

    print(f"\n[stage] bioassembly: {args.bioassembly_uri}")

    if is_zip:
        count = download_and_extract_zip(args.bioassembly_uri, bio_dir, args.creds)
        if count == 0:
            raise RuntimeError("No .pkl.gz files found after extraction")
        result["bioassembly_dir"] = str(bio_dir)
        result["bioassembly_count"] = str(count)
    elif parsed.scheme == "file":
        src = Path(parsed.key)
        if src.is_dir():
            count = sum(1 for _ in src.rglob("*.pkl.gz"))
            print(f"  local directory: {src} ({count} .pkl.gz files)")
            result["bioassembly_dir"] = str(src)
            result["bioassembly_count"] = str(count)
        else:
            raise ValueError(f"Expected ZIP file or directory, got: {src}")
    else:
        raise ValueError(
            f"Bioassembly URI must be a .zip file or local directory: {args.bioassembly_uri}"
        )

    return result


def stage_indices(args: argparse.Namespace) -> dict[str, str]:
    """Stage index CSV and PDB list files."""
    idx_dir = args.staging_dir / "indices"
    result = {}

    print(f"\n[stage] indices CSV: {args.indices_uri}")
    parsed = parse_uri(args.indices_uri)
    csv_filename = parsed.key.rsplit("/", 1)[-1] if parsed.scheme != "file" else Path(parsed.key).name
    csv_dest = idx_dir / csv_filename
    download_object(args.indices_uri, csv_dest, args.creds)
    result["indices_csv"] = str(csv_dest)

    if args.pdb_list_uri:
        print(f"\n[stage] PDB list: {args.pdb_list_uri}")
        parsed_pdb = parse_uri(args.pdb_list_uri)
        pdb_filename = parsed_pdb.key.rsplit("/", 1)[-1] if parsed_pdb.scheme != "file" else Path(parsed_pdb.key).name
        pdb_dest = idx_dir / pdb_filename
        download_object(args.pdb_list_uri, pdb_dest, args.creds)
        result["pdb_list"] = str(pdb_dest)
    else:
        print("\n[stage] no PDB list URI provided (optional)")

    return result


def write_manifest(staging_dir: Path, all_results: dict) -> Path:
    """Write a staging manifest JSON for downstream scripts."""
    import json

    manifest_path = staging_dir / "staging_manifest.json"
    manifest = {
        "staged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "paths": all_results,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\n[stage] manifest: {manifest_path}")
    return manifest_path


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage training data from R2/S3 to local disk",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "URI schemes (each routes to a DIFFERENT S3-compatible endpoint):\n"
            "  r2://bucket/key      Cloudflare R2     (CLOUDFLARE_R2_* env vars)\n"
            "  s3://bucket/key      AWS S3            (default boto3 credentials)\n"
            "  runpod://bucket/key  RunPod Network Vol (RUNPOD_S3_* env vars)\n"
            "  file:///path         Local file/directory\n"
            "  /path                Local file/directory (shorthand)\n"
            "\n"
            "WARNING: Despite all being S3-API-compatible, these are different\n"
            "endpoints with different credentials. Using the wrong scheme gives\n"
            "'Bucket Not Found' even when the bucket exists on another provider.\n"
        ),
    )
    ap.add_argument(
        "--checkpoint-uri",
        default=os.environ.get("STAGE_CHECKPOINT_URI", ""),
        help="URI of the model checkpoint .pt file (env: STAGE_CHECKPOINT_URI)",
    )
    ap.add_argument(
        "--ema-uri",
        default=os.environ.get("STAGE_EMA_URI", ""),
        help="URI of the EMA checkpoint .pt file (env: STAGE_EMA_URI). "
             "Empty for first run from base model.",
    )
    ap.add_argument(
        "--bioassembly-uri",
        default=os.environ.get("STAGE_BIOASSEMBLY_URI", ""),
        help="URI of bioassembly ZIP or local directory (env: STAGE_BIOASSEMBLY_URI)",
    )
    ap.add_argument(
        "--indices-uri",
        default=os.environ.get("STAGE_INDICES_URI", ""),
        help="URI of the training indices CSV (env: STAGE_INDICES_URI)",
    )
    ap.add_argument(
        "--pdb-list-uri",
        default=os.environ.get("STAGE_PDB_LIST_URI", ""),
        help="URI of the PDB ID list .txt (env: STAGE_PDB_LIST_URI)",
    )
    ap.add_argument(
        "--staging-dir",
        type=Path,
        default=Path(os.environ.get("STAGE_DIR", "/workspace/training_data")),
        help="Local directory to stage data into (env: STAGE_DIR)",
    )
    ap.add_argument(
        "--creds",
        type=Path,
        default=Path("/dev/shm/secure/creds"),
        help="Path to shell-export creds file for R2 credentials",
    )
    ap.add_argument(
        "--skip-if-staged",
        action="store_true",
        default=os.environ.get("STAGE_SKIP_IF_STAGED", "") == "1",
        help="Skip staging if staging_manifest.json already exists (env: STAGE_SKIP_IF_STAGED=1)",
    )
    args = ap.parse_args()

    if args.skip_if_staged and (args.staging_dir / "staging_manifest.json").exists():
        print("[stage] staging_manifest.json exists and --skip-if-staged set, skipping")
        return 0

    if not args.checkpoint_uri:
        ap.error("--checkpoint-uri is required (or set STAGE_CHECKPOINT_URI env var)")
    if not args.bioassembly_uri:
        ap.error("--bioassembly-uri is required (or set STAGE_BIOASSEMBLY_URI env var)")
    if not args.indices_uri:
        ap.error("--indices-uri is required (or set STAGE_INDICES_URI env var)")

    print("=" * 70)
    print("[stage] Protenix training data staging")
    print(f"  checkpoint:   {args.checkpoint_uri}")
    print(f"  ema:          {args.ema_uri or '(none)'}")
    print(f"  bioassembly:  {args.bioassembly_uri}")
    print(f"  indices:      {args.indices_uri}")
    print(f"  pdb_list:     {args.pdb_list_uri or '(none)'}")
    print(f"  staging_dir:  {args.staging_dir}")
    print(f"  creds:        {args.creds}")
    print("=" * 70)

    t0 = time.monotonic()
    all_results: dict[str, str] = {}

    try:
        ckpt_result = stage_checkpoint(args)
        all_results.update(ckpt_result)

        bio_result = stage_bioassembly(args)
        all_results.update(bio_result)

        idx_result = stage_indices(args)
        all_results.update(idx_result)

    except Exception as e:
        print(f"\n[stage] FATAL: {e}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - t0
    write_manifest(args.staging_dir, all_results)

    print("\n" + "=" * 70)
    print(f"[stage] COMPLETE in {elapsed:.0f}s")
    print(f"  checkpoint:       {all_results.get('checkpoint', 'N/A')}")
    if "ema" in all_results:
        print(f"  ema:              {all_results['ema']}")
    print(f"  bioassembly_dir:  {all_results.get('bioassembly_dir', 'N/A')}")
    print(f"  bioassembly count: {all_results.get('bioassembly_count', 'N/A')}")
    print(f"  indices_csv:      {all_results.get('indices_csv', 'N/A')}")
    if "pdb_list" in all_results:
        print(f"  pdb_list:         {all_results['pdb_list']}")
    print("=" * 70)

    print("\n[stage] For run_salad_training.sh, set:")
    print(f"  PREV_CKPT={all_results.get('checkpoint', '')}")
    if "ema" in all_results:
        print(f"  PREV_EMA={all_results['ema']}")
    print(f"  BIO_DIR={all_results.get('bioassembly_dir', '')}")
    print(f"  TRAIN_CSV={all_results.get('indices_csv', '')}")
    if "pdb_list" in all_results:
        print(f"  TRAIN_PDB={all_results['pdb_list']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
