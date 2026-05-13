"""Filter a Protenix fold CSV (e.g., train_fold1.csv) down to a smoke-test slice
whose PDB IDs have bioassembly .pkl.gz files in a local directory.

Used when the full bioassembly set has already been unzipped locally (e.g., from
s3://vh-pdb-structures/bioassembly_crop384/idp_set.zip) and we just want to pick
the first N PDB IDs from the fold CSV that have a matching `.pkl.gz` on disk.

Writes:
  * <indices_dir>/<out_prefix>_indices.csv     header + matching rows
  * <indices_dir>/<out_prefix>_indices.csv.gz  same, gzipped (Protenix accepts .gz)
  * <indices_dir>/<out_prefix>_pdb_ids.txt     one PDB ID per line

Usage:
    python filter_fold_csv_to_local_bioassemblies.py \
        --fold-csv /path/to/train_fold1.csv \
        --bio-dir /workspace/training_data/idp_v2/bioassembly \
        --indices-dir /workspace/training_data/idp_v2/indices \
        --out-prefix smoke \
        --n 5
"""

from __future__ import annotations

import argparse
import csv
import gzip
import shutil
import sys
from pathlib import Path


def find_first_n_matching(fold_csv: Path, bio_dir: Path, n: int) -> list[str]:
    """Walk fold CSV in order; return first n unique pdb_ids that have a
    matching <pdb_id>.pkl.gz under bio_dir."""
    picked: list[str] = []
    seen: set[str] = set()
    with fold_csv.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            pid = row.get("pdb_id", "").strip().strip('"').lower()
            if not pid or pid in seen:
                continue
            if (bio_dir / f"{pid}.pkl.gz").is_file():
                seen.add(pid)
                picked.append(pid)
                if len(picked) >= n:
                    return picked
            else:
                seen.add(pid)  # mark seen so we don't re-check
    return picked


def write_filtered_csv(fold_csv: Path, out_csv: Path, pdb_ids: set[str]) -> int:
    """Write filtered CSV preserving the original header + only rows whose
    pdb_id is in pdb_ids. Uses csv module so quoting is correct.

    Returns: number of data rows written.
    """
    n = 0
    with fold_csv.open(newline="") as fin, out_csv.open("w", newline="") as fout:
        reader = csv.reader(fin)
        writer = csv.writer(fout, quoting=csv.QUOTE_ALL)
        header = next(reader)
        writer.writerow(header)
        try:
            col = header.index("pdb_id")
        except ValueError:
            print("ERROR: pdb_id column not in CSV header", file=sys.stderr)
            return 0
        for row in reader:
            pid = row[col].strip().strip('"').lower()
            if pid in pdb_ids:
                writer.writerow(row)
                n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fold-csv", type=Path, required=True,
                    help="Source fold CSV (e.g., train_fold1.csv)")
    ap.add_argument("--bio-dir", type=Path, required=True,
                    help="Local dir containing <pdb_id>.pkl.gz bioassembly files")
    ap.add_argument("--indices-dir", type=Path, required=True,
                    help="Output dir for the filtered indices CSV + pdb_ids.txt")
    ap.add_argument("--out-prefix", default="smoke",
                    help="Filename prefix for outputs (default: smoke)")
    ap.add_argument("--n", type=int, default=5,
                    help="How many PDB IDs to include")
    args = ap.parse_args()

    if not args.fold_csv.is_file():
        print(f"ERROR: fold csv not found: {args.fold_csv}", file=sys.stderr)
        return 2
    if not args.bio_dir.is_dir():
        print(f"ERROR: bio dir not found: {args.bio_dir}", file=sys.stderr)
        return 2

    args.indices_dir.mkdir(parents=True, exist_ok=True)

    print(f"[filter] picking first {args.n} PDB IDs from {args.fold_csv} "
          f"with matching .pkl.gz in {args.bio_dir}")
    picked = find_first_n_matching(args.fold_csv, args.bio_dir, args.n)
    print(f"[filter] picked {len(picked)}: {picked}")
    if not picked:
        print("ERROR: no overlap between fold CSV and bio dir", file=sys.stderr)
        return 3

    indices_csv = args.indices_dir / f"{args.out_prefix}_indices.csv"
    indices_csv_gz = args.indices_dir / f"{args.out_prefix}_indices.csv.gz"
    indices_pdb = args.indices_dir / f"{args.out_prefix}_pdb_ids.txt"

    n_rows = write_filtered_csv(args.fold_csv, indices_csv, set(picked))
    print(f"[filter] wrote {indices_csv} ({n_rows} data rows)")

    with indices_csv.open("rb") as fin, gzip.open(indices_csv_gz, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    print(f"[filter] gzipped to {indices_csv_gz}")

    indices_pdb.write_text("\n".join(picked) + "\n")
    print(f"[filter] wrote {indices_pdb}")

    print("\n[filter] Use these as train.py overrides:")
    print(f"  --data.weightedPDB_before2109_wopb_nometalc_0925.base_info.bioassembly_dict_dir "
          f"{args.bio_dir}")
    print(f"  --data.weightedPDB_before2109_wopb_nometalc_0925.base_info.indices_fpath "
          f"{indices_csv_gz}")
    print(f"  --data.weightedPDB_before2109_wopb_nometalc_0925.base_info.pdb_list "
          f"{indices_pdb}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
