#!/usr/bin/env python3
"""Generate PDB block training indices and upload to R2.

For each block, produces:
  - blockNN.csv    — master-index rows filtered to this block's PDB IDs
  - blockNN_pdb_ids.txt — sorted PDB ID list (one per line)

Uses block_assignments_full.json (maps PDB ID → block number) and the
master general_pdb CSVs (contain training pairs for all structures).

Usage:
    # Generate missing indices (03-16) and upload to R2:
    python3 generate_block_indices.py \
        --assignments /tmp/metadata/block_assignments_full.json \
        --master-csv /tmp/indices/general_pdb.csv \
        --extra-csv /tmp/indices/general_pdb_remaining.csv \
                    /tmp/indices/general_pdb_remaining2.csv \
        --output-dir /tmp/block_indices \
        --blocks 3-16 \
        --upload

    # Generate all 17 blocks without uploading (dry run):
    python3 generate_block_indices.py \
        --assignments /tmp/metadata/block_assignments_full.json \
        --master-csv /tmp/indices/general_pdb.csv \
        --output-dir /tmp/block_indices \
        --blocks 0-16
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path


def load_assignments(path: Path) -> dict[str, int]:
    with open(path) as f:
        data = json.load(f)
    return data["assignments"]


def parse_block_range(spec: str) -> list[int]:
    """Parse '3-16' or '0,5,10' or '7' into a list of block numbers."""
    blocks = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            blocks.extend(range(int(lo), int(hi) + 1))
        else:
            blocks.append(int(part))
    return sorted(set(blocks))


def generate_block_index(
    block_num: int,
    block_pdb_ids: set[str],
    master_csvs: list[Path],
    output_dir: Path,
) -> tuple[Path, Path, int]:
    """Generate blockNN.csv and blockNN_pdb_ids.txt for one block."""
    csv_path = output_dir / f"block{block_num:02d}.csv"
    txt_path = output_dir / f"block{block_num:02d}_pdb_ids.txt"

    block_ids_lower = {pid.lower() for pid in block_pdb_ids}
    row_count = 0
    header = None

    with open(csv_path, "w", newline="") as out_f:
        writer = None
        for master in master_csvs:
            with open(master) as in_f:
                reader = csv.reader(in_f)
                file_header = next(reader)
                if header is None:
                    header = file_header
                    writer = csv.writer(out_f, quoting=csv.QUOTE_ALL)
                    writer.writerow(header)
                pdb_col = header.index("pdb_id")
                for row in reader:
                    if row[pdb_col].lower() in block_ids_lower:
                        writer.writerow(row)
                        row_count += 1

    with open(txt_path, "w") as f:
        for pid in sorted(block_ids_lower):
            f.write(pid + "\n")

    return csv_path, txt_path, row_count


def upload_to_r2(local_path: Path, r2_key: str) -> bool:
    """Upload a file to R2 using AWS CLI with cloudflare-r2 profile."""
    endpoint = os.environ.get(
        "CLOUDFLARE_R2_ENDPOINT",
        "https://38b89c40322f61724de07fbc1faa813a.r2.cloudflarestorage.com",
    )
    cmd = [
        "aws", "s3", "cp", str(local_path), f"s3://{r2_key}",
        "--profile", "cloudflare-r2",
        "--endpoint-url", endpoint,
        "--region", "auto",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        print(f"  UPLOAD FAILED: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assignments", type=Path, required=True,
                    help="block_assignments_full.json")
    ap.add_argument("--master-csv", type=Path, required=True,
                    help="Primary master index CSV (general_pdb.csv)")
    ap.add_argument("--extra-csv", type=Path, nargs="*", default=[],
                    help="Additional master CSVs to merge")
    ap.add_argument("--output-dir", type=Path, required=True,
                    help="Local directory for generated indices")
    ap.add_argument("--blocks", type=str, default="0-16",
                    help="Block range to generate (e.g. '3-16', '0,5,10')")
    ap.add_argument("--upload", action="store_true",
                    help="Upload generated indices to R2")
    ap.add_argument("--r2-prefix", type=str,
                    default="vh-protenix-training/data/general_pdb",
                    help="R2 bucket/prefix for upload")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing files")
    args = ap.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    blocks_to_gen = parse_block_range(args.blocks)
    print(f"Generating indices for blocks: {blocks_to_gen}")

    assignments = load_assignments(args.assignments)
    print(f"Loaded {len(assignments)} PDB ID assignments")

    block_to_pids: dict[int, set[str]] = {}
    for pid, block in assignments.items():
        block_to_pids.setdefault(block, set()).add(pid)

    master_csvs = [args.master_csv] + [p for p in args.extra_csv if p.exists()]
    print(f"Master CSVs: {[str(p) for p in master_csvs]}")

    for block in blocks_to_gen:
        csv_out = args.output_dir / f"block{block:02d}.csv"
        if csv_out.exists() and not args.force:
            print(f"  block{block:02d}: already exists, skipping (use --force)")
            continue

        pids = block_to_pids.get(block, set())
        if not pids:
            print(f"  block{block:02d}: no PDB IDs in assignments, skipping")
            continue

        print(f"  block{block:02d}: {len(pids)} assigned PDB IDs...", end="", flush=True)
        csv_path, txt_path, rows = generate_block_index(
            block, pids, master_csvs, args.output_dir)
        size_mb = csv_path.stat().st_size / (1024 * 1024)
        print(f" → {rows} rows ({size_mb:.1f} MB), {len(pids)} PDB IDs")

        if args.upload:
            ok1 = upload_to_r2(csv_path, f"{args.r2_prefix}/block{block:02d}.csv")
            ok2 = upload_to_r2(txt_path, f"{args.r2_prefix}/block{block:02d}_pdb_ids.txt")
            status = "uploaded" if (ok1 and ok2) else "UPLOAD FAILED"
            print(f"    {status}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
