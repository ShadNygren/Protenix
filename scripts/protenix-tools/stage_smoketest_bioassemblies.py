"""Stage a tiny subset of bioassembly .pkl.gz files for the v2.2 smoke test.

Reads the first N PDB IDs from train_fold1.csv, looks each one up in
block_assignments_full.json to find its block ZIP, then uses `remotezip` to
fetch only that single entry from R2 — no need to download an 850 MB block ZIP
to extract a 60 KB file.

Writes:
  * <bio_dir>/<pdb_id>.pkl.gz                    — staged bioassembly
  * <indices_dir>/smoketest_indices.csv          — header + matching rows
  * <indices_dir>/smoketest_indices.csv.gz       — gzipped (Protenix wants .gz)
  * <indices_dir>/smoketest_pdb_ids.txt          — one ID per line

Reads R2 endpoint + creds from /dev/shm/secure/creds (shell-export format).

Usage:
    python stage_smoketest_bioassemblies.py \
        --train-csv /workspace/staging/extracted/folds/train_fold1.csv \
        --assignments /workspace/staging/extracted/metadata/block_assignments_full.json \
        --bucket vh-pdb-structures \
        --prefix bioassembly_crop384 \
        --bio-dir   /workspace/staging/smoketest_subset/bioassembly \
        --indices-dir /workspace/staging/smoketest_subset/indices \
        --n 5
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def parse_creds_file(path: Path) -> dict[str, str]:
    """Parse a shell-export `export FOO="bar"` file into a dict."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        kv = line[len("export "):].split("=", 1)
        if len(kv) != 2:
            continue
        k, v = kv[0].strip(), kv[1].strip().strip('"').strip("'")
        out[k] = v
    return out


def boto3_client(endpoint: str, access_key: str, secret_key: str):
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def first_n_unique_pdb_ids(csv_path: Path, n: int,
                           allowed: set[str] | None = None) -> list[str]:
    """Read CSV col 'pdb_id' and return first n unique IDs (preserves order).

    If `allowed` is given, only return IDs that also appear in that set.
    Caller scans the whole file (no early break) until n IDs match or we
    exhaust the input.
    """
    ids: list[str] = []
    seen: set[str] = set()
    with csv_path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            pid = row.get("pdb_id", "").strip().strip('"').lower()
            if not pid or pid in seen:
                continue
            if allowed is not None and pid not in allowed:
                continue
            seen.add(pid)
            ids.append(pid)
            if len(ids) >= n:
                break
    return ids


def fetch_one_via_remotezip(s3_url: str, member: str, dest: Path,
                            endpoint: str, access_key: str, secret_key: str) -> bool:
    """Use remotezip to fetch `member` from `s3_url` (s3://bucket/key) into `dest`.

    remotezip can read from any URL its HTTP client understands. We construct a
    presigned URL via boto3 so remotezip can use plain HTTP GET range requests.
    """
    bucket, _, key = s3_url[len("s3://"):].partition("/")
    s3 = boto3_client(endpoint, access_key, secret_key)
    presigned = s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=3600,
    )
    try:
        from remotezip import RemoteZip  # type: ignore
    except ImportError:
        print("remotezip not installed; falling back to full block download", file=sys.stderr)
        return False

    try:
        with RemoteZip(presigned) as rz:
            members = rz.namelist()
            target = None
            for cand in (member, f"./{member}", f"{member}"):
                if cand in members:
                    target = cand
                    break
            if target is None:
                # Try lowercase match
                lc = member.lower()
                for m in members:
                    if m.lower().endswith(lc):
                        target = m
                        break
            if target is None:
                print(f"  member {member!r} not found in {key} ({len(members)} entries)",
                      file=sys.stderr)
                return False
            data = rz.read(target)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return True
    except Exception as e:
        print(f"  remotezip fetch failed: {e}", file=sys.stderr)
        return False


def filter_csv_by_pdb_ids(src: Path, dst: Path, pdb_ids: set[str]) -> int:
    """Copy header + rows where pdb_id is in pdb_ids. Returns row count."""
    n = 0
    with src.open(newline="") as fh_in, dst.open("w", newline="") as fh_out:
        reader = csv.reader(fh_in)
        writer = csv.writer(fh_out, quoting=csv.QUOTE_ALL)
        header = next(reader)
        writer.writerow(header)
        try:
            pdb_col = header.index("pdb_id")
        except ValueError:
            print("ERROR: pdb_id column not found in CSV header", file=sys.stderr)
            return 0
        for row in reader:
            pid = row[pdb_col].strip().strip('"').lower()
            if pid in pdb_ids:
                writer.writerow(row)
                n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-csv", type=Path, required=True)
    ap.add_argument("--assignments", type=Path, required=True)
    ap.add_argument("--bucket", default="vh-pdb-structures")
    ap.add_argument("--prefix", default="bioassembly_crop384")
    ap.add_argument("--bio-dir", type=Path, required=True)
    ap.add_argument("--indices-dir", type=Path, required=True)
    ap.add_argument("--n", type=int, default=5,
                    help="Number of PDB IDs to stage")
    ap.add_argument("--start-block", type=int, default=0,
                    help="First block index to inspect (default: 0)")
    ap.add_argument("--max-blocks", type=int, default=17,
                    help="How many consecutive blocks to walk if start-block "
                         "has no fold1 intersection")
    ap.add_argument("--creds", type=Path, default=Path("/dev/shm/secure/creds"))
    args = ap.parse_args()

    creds = parse_creds_file(args.creds)
    endpoint = creds.get("CLOUDFLARE_R2_ENDPOINT") or os.environ.get("CLOUDFLARE_R2_ENDPOINT")
    access_key = creds.get("CLOUDFLARE_R2_ACCESS_KEY_ID") or os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID")
    secret_key = creds.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY") or os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY")
    if not (endpoint and access_key and secret_key):
        print("ERROR: missing R2 creds (CLOUDFLARE_R2_*)", file=sys.stderr)
        return 2

    args.bio_dir.mkdir(parents=True, exist_ok=True)
    args.indices_dir.mkdir(parents=True, exist_ok=True)

    print(f"[stage] loading block assignments from {args.assignments}")
    with args.assignments.open() as fh:
        ba_root = json.load(fh)
    assignments = ba_root.get("assignments", ba_root)
    print(f"[stage] block_assignments covers {len(assignments)} PDB IDs (planned)")

    # Load FULL set of PDB IDs from train_fold1 (no caps) — train CSVs are
    # ~75 MB, fits easily in RAM. Preserves the fold1 ordering so we still
    # pick IDs that fold1 trains on first.
    print(f"[stage] loading all unique PDB IDs from {args.train_csv}")
    fold_ids = first_n_unique_pdb_ids(args.train_csv, n=10**9, allowed=None)
    print(f"  fold has {len(fold_ids)} unique PDB IDs")
    fold_id_set = set(fold_ids)

    # Walk blocks; ground-truth their contents via remotezip.namelist(); take
    # the intersection with fold1 IDs (preserving fold1 order). Stop at args.n.
    from remotezip import RemoteZip  # type: ignore

    staged: list[str] = []
    for block_idx in range(args.start_block, args.start_block + args.max_blocks):
        if len(staged) >= args.n:
            break
        block_zip_key = f"{args.prefix}/block{block_idx:02d}.zip"
        s3_url = f"s3://{args.bucket}/{block_zip_key}"
        print(f"[stage] inspecting {s3_url}")
        bucket, _, key = s3_url[len("s3://"):].partition("/")
        s3 = boto3_client(endpoint, access_key, secret_key)
        try:
            presigned = s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=3600,
            )
            with RemoteZip(presigned) as rz:
                actual_ids = {m[:-len(".pkl.gz")] for m in rz.namelist()
                              if m.endswith(".pkl.gz")}
                intersect_size = len(fold_id_set & actual_ids)
                print(f"  block{block_idx:02d}: {len(actual_ids)} entries in ZIP, "
                      f"{intersect_size} intersect with fold1")
                if intersect_size == 0:
                    continue
                # Pick first N from fold1 ordering that are in this block
                for pid in fold_ids:
                    if len(staged) >= args.n:
                        break
                    if pid not in actual_ids:
                        continue
                    member = f"{pid}.pkl.gz"
                    dest = args.bio_dir / member
                    try:
                        data = rz.read(member)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)
                        if dest.stat().st_size > 0:
                            print(f"    {pid}: OK ({dest.stat().st_size} bytes)")
                            staged.append(pid)
                        else:
                            print(f"    {pid}: zero-byte read")
                    except Exception as e:
                        print(f"    {pid}: read failed: {e}")
        except Exception as e:
            print(f"  block{block_idx:02d}: namelist failed: {e}")
            continue

    if not staged:
        print("ERROR: no bioassemblies staged", file=sys.stderr)
        return 3

    print(f"[stage] staged {len(staged)} bioassemblies: {staged}")

    # Build indices CSV
    indices_csv = args.indices_dir / "smoketest_indices.csv"
    indices_csv_gz = indices_csv.with_suffix(".csv.gz")
    indices_pdb = args.indices_dir / "smoketest_pdb_ids.txt"

    n_rows = filter_csv_by_pdb_ids(args.train_csv, indices_csv, set(staged))
    print(f"[stage] wrote {indices_csv} ({n_rows} rows)")

    # Gzip it (Protenix's csv reader accepts .gz)
    with indices_csv.open("rb") as fin, gzip.open(indices_csv_gz, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    print(f"[stage] gzipped to {indices_csv_gz}")

    indices_pdb.write_text("\n".join(staged) + "\n")
    print(f"[stage] wrote {indices_pdb}")

    print("\n[stage] DONE. Inputs for train.py:")
    print(f"  --data.<dataset>.base_info.bioassembly_dict_dir {args.bio_dir}")
    print(f"  --data.<dataset>.base_info.indices_fpath        {indices_csv_gz}")
    print(f"  --data.<dataset>.base_info.pdb_list             {indices_pdb}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
