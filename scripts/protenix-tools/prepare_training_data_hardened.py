"""Hardened wrapper around `scripts/prepare_training_data.py` for bulk
re-preprocessing of bioassembly .pkl.gz files.

The original `prepare_training_data.py` (committed by ByteDance upstream) has
several gaps that bit us when blocks 4-7 ended up with partial/missing/corrupt
output:

1. **No skip-if-exists**: re-running re-processed every CIF, wasting CPU days.
2. **No post-write validation**: silently accepts 0-byte gzip stubs or
   truncated pickles. The training pipeline only fails later with cryptic
   errors.
3. **No size sanity check**: a "successful" run that produced only a few
   bytes per PDB went undetected for blocks 4 and 5 until the chain crashed.
4. **No per-PDB failure log**: joblib's `return_as="generator_unordered"`
   silently drops failures — you can't tell WHICH PDBs failed without
   diffing inputs against outputs.
5. **No per-PDB exception isolation**: a single bad CIF could abort the
   whole batch (joblib re-raises in the main thread).

This script wraps `gen_a_bioassembly_data` with:

* **`skip_if_valid`** — checks if output exists AND passes a post-write
  decompress/unpickle/keys-present test. Skips if valid; reprocesses if
  missing or fails validation.
* **`min_size_bytes`** — flags outputs below this size as suspicious. The
  observed median across 122,700 successful files is ~100 KB; the 5th
  percentile is around 15 KB. Anything below 5 KB is almost certainly
  broken — gets `.suspect` suffix and logged.
* **Per-PDB JSONL failure log** at `--failure-log` with `{pdb_id, error,
  trace}` for every failure. Makes systematic problems (e.g., "all 4c-4m
  PDBs fail with same KeyError") visible at a glance.
* **Per-PDB exception isolation** — wraps `gen_a_bioassembly_data` in
  try/except so one bad CIF can't kill the pool.

Usage (on the pod, after the audit cleanup completes):

    # First: build the list of CIFs we need to re-preprocess
    python prepare_training_data_hardened.py \\
        --input-cif-dir /data/training/general_pdb/cif_files \\
        --bio-output-dir /data/training/general_pdb/bioassembly \\
        --output-indices-csv /data/training/general_pdb/indices/rebuilt_blocks_4to7.csv \\
        --pdb-id-allowlist-file /data/training/general_pdb/indices/blocks_4to7_pdb_ids.txt \\
        --failure-log /data/training/general_pdb/data_prep_rebuild.failures.jsonl \\
        --n-cpu 32 \\
        --min-size-bytes 5000

The script imports the upstream entry-point function `gen_a_bioassembly_data`
from `scripts/prepare_training_data.py` so we stay in sync with whatever the
upstream pipeline does — we're only adding pre-/post-processing.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import pickle
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

# Ensure we can import the upstream prepare_training_data module
sys.path.insert(0, "/workspace/scripts")
sys.path.insert(0, "/workspace")

try:
    from prepare_training_data import gen_a_bioassembly_data  # type: ignore
except ImportError as e:
    print(f"FATAL: cannot import upstream prepare_training_data: {e}", file=sys.stderr)
    print(f"Make sure this script runs in an environment with the Protenix repo at /workspace.", file=sys.stderr)
    sys.exit(2)

import pandas as pd  # type: ignore
from joblib import Parallel, delayed  # type: ignore
from tqdm import tqdm  # type: ignore


# ---------- validation helpers ----------

# Keys we expect every valid bioassembly .pkl.gz to contain (per DataPipeline
# output schema). Verified against known-good files from blocks 0-3 + 8-15.
REQUIRED_BIOASSEMBLY_KEYS = {
    "pdb_id",
    "atom_array",
    "entity_poly_type",
}


def validate_bioassembly_pkl(path: Path, min_size_bytes: int) -> tuple[bool, str]:
    """Verify the .pkl.gz file is well-formed.

    Returns (ok, reason). reason is empty if ok=True, else describes the
    failure for the JSONL failure log.
    """
    if not path.exists():
        return False, "missing"
    size = path.stat().st_size
    if size == 0:
        return False, "zero-byte"
    if size < min_size_bytes:
        return False, f"too-small ({size} bytes < {min_size_bytes})"
    try:
        with gzip.open(path, "rb") as fh:
            data = pickle.load(fh)
    except (gzip.BadGzipFile, EOFError) as e:
        return False, f"corrupt-gzip ({type(e).__name__}: {e})"
    except (pickle.UnpicklingError, ValueError, AttributeError) as e:
        return False, f"unpickle-failed ({type(e).__name__}: {e})"
    except Exception as e:
        return False, f"unknown-load-error ({type(e).__name__}: {e})"
    if not isinstance(data, dict):
        return False, f"not-a-dict (got {type(data).__name__})"
    missing_keys = REQUIRED_BIOASSEMBLY_KEYS - set(data.keys())
    if missing_keys:
        return False, f"missing-keys ({missing_keys})"
    return True, ""


# ---------- single-PDB processing wrapper with full isolation ----------

def process_one_pdb_safe(
    mmcif_path: Path,
    bio_output_dir: Path,
    cluster_file: Optional[Path],
    distillation: bool,
    min_size_bytes: int,
    skip_if_valid: bool,
    failure_log_path: Optional[Path],
) -> dict:
    """Process one CIF → bioassembly with exception isolation + validation.

    Returns a result dict (always — never raises). Caller writes the dict to
    JSONL on failure.
    """
    pdb_id = mmcif_path.stem.replace(".cif", "")
    output_path = bio_output_dir / f"{pdb_id}.pkl.gz"
    result = {"pdb_id": pdb_id, "status": "unknown"}

    # Skip if already valid
    if skip_if_valid:
        ok, reason = validate_bioassembly_pkl(output_path, min_size_bytes)
        if ok:
            result["status"] = "skip-already-valid"
            return result
        elif reason != "missing":
            result["pre_skip_reason"] = reason

    # Run upstream processing inside try/except
    t0 = time.time()
    sample_indices_list = None
    try:
        sample_indices_list = gen_a_bioassembly_data(
            mmcif=mmcif_path,
            bioassembly_output_dir=bio_output_dir,
            cluster_file=cluster_file,
            distillation=distillation,
        )
    except Exception as e:
        result["status"] = "exception-during-processing"
        result["error"] = f"{type(e).__name__}: {e}"
        result["trace"] = traceback.format_exc()
        result["elapsed_s"] = round(time.time() - t0, 2)
        return result

    elapsed = time.time() - t0
    result["elapsed_s"] = round(elapsed, 2)

    # Post-write validation
    ok, reason = validate_bioassembly_pkl(output_path, min_size_bytes)
    if not ok:
        # Move suspect file aside so it doesn't pollute downstream training
        suspect_path = output_path.with_suffix(".pkl.gz.suspect")
        if output_path.exists():
            output_path.rename(suspect_path)
        result["status"] = "post-write-invalid"
        result["error"] = reason
        result["suspect_path"] = str(suspect_path)
        return result

    result["status"] = "ok"
    result["sample_count"] = len(sample_indices_list) if sample_indices_list else 0
    result["sample_indices"] = sample_indices_list
    return result


def append_to_jsonl(path: Path, record: dict) -> None:
    """Append a JSON record to JSONL file. Safe for parallel append (line-based)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        json.dump(record, fh, default=str)
        fh.write("\n")


# ---------- main batch driver ----------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--input-cif-dir", type=Path, required=True,
                    help="Directory containing <pdb_id>.cif files")
    ap.add_argument("--bio-output-dir", type=Path, required=True,
                    help="Directory to write <pdb_id>.pkl.gz files")
    ap.add_argument("--output-indices-csv", type=Path, required=True,
                    help="CSV of successful sample indices (matches upstream output)")
    ap.add_argument("--pdb-id-allowlist-file", type=Path, default=None,
                    help="Optional .txt file listing PDB IDs to process; "
                         "if omitted, all CIFs in --input-cif-dir are processed")
    ap.add_argument("--cluster-file", type=Path, default=None,
                    help="Optional cluster .txt file (passed through to upstream)")
    ap.add_argument("--distillation", action="store_true",
                    help="Use distillation dataset config (default: WeightedPDB)")
    ap.add_argument("--n-cpu", type=int, default=os.cpu_count() or 1,
                    help=f"Parallel workers (default: all available = {os.cpu_count()})")
    ap.add_argument("--min-size-bytes", type=int, default=5000,
                    help="Files below this size are flagged as suspect (default: 5000)")
    ap.add_argument("--no-skip-if-valid", action="store_true",
                    help="Force reprocess even if output already passes validation")
    ap.add_argument("--failure-log", type=Path, default=None,
                    help="JSONL path for per-PDB failure records (default: alongside output CSV)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N PDBs (for testing)")
    args = ap.parse_args()

    if args.failure_log is None:
        args.failure_log = args.output_indices_csv.with_suffix(".failures.jsonl")

    # Build the CIF list
    print(f"[hardened] input dir: {args.input_cif_dir}", flush=True)
    all_cifs = list(args.input_cif_dir.glob("*.cif")) + list(args.input_cif_dir.glob("*.cif.gz"))
    print(f"[hardened] total CIFs in dir: {len(all_cifs)}", flush=True)

    if args.pdb_id_allowlist_file:
        allowed = {line.strip().lower() for line in args.pdb_id_allowlist_file.read_text().splitlines() if line.strip()}
        all_cifs = [c for c in all_cifs if c.stem.lower() in allowed]
        print(f"[hardened] filtered to allowlist: {len(all_cifs)} CIFs (allowlist has {len(allowed)} IDs)", flush=True)

    if args.limit > 0:
        all_cifs = all_cifs[:args.limit]
        print(f"[hardened] --limit applied: processing only first {args.limit}", flush=True)

    if not all_cifs:
        print("ERROR: no CIFs to process", file=sys.stderr)
        return 2

    args.bio_output_dir.mkdir(parents=True, exist_ok=True)
    args.output_indices_csv.parent.mkdir(parents=True, exist_ok=True)
    args.failure_log.parent.mkdir(parents=True, exist_ok=True)
    # Truncate failure log at start so reruns don't accumulate stale entries.
    args.failure_log.write_text("")

    print(f"[hardened] starting batch: {len(all_cifs)} CIFs, "
          f"n_cpu={args.n_cpu}, min_size={args.min_size_bytes} B, "
          f"skip_if_valid={not args.no_skip_if_valid}", flush=True)
    print(f"[hardened] failure log: {args.failure_log}", flush=True)

    t_start = time.time()
    n_ok = 0
    n_skip = 0
    n_fail = 0
    all_sample_indices = []

    results = Parallel(n_jobs=args.n_cpu, return_as="generator_unordered")(
        delayed(process_one_pdb_safe)(
            mmcif_path=cif,
            bio_output_dir=args.bio_output_dir,
            cluster_file=args.cluster_file,
            distillation=args.distillation,
            min_size_bytes=args.min_size_bytes,
            skip_if_valid=not args.no_skip_if_valid,
            failure_log_path=args.failure_log,
        )
        for cif in all_cifs
    )

    for result in tqdm(results, total=len(all_cifs)):
        status = result.get("status", "unknown")
        if status == "ok":
            n_ok += 1
            si = result.pop("sample_indices", None)
            if si:
                all_sample_indices += si
        elif status == "skip-already-valid":
            n_skip += 1
        else:
            n_fail += 1
            # Write failure record
            append_to_jsonl(args.failure_log, result)

    # Write the indices CSV (matches upstream behavior)
    if all_sample_indices:
        df = pd.DataFrame(all_sample_indices)
        df.to_csv(args.output_indices_csv, index=False, quoting=1)
        print(f"[hardened] wrote {args.output_indices_csv} ({len(df)} rows)", flush=True)

    elapsed = time.time() - t_start
    rate = (n_ok + n_skip + n_fail) / max(elapsed, 1)
    print(f"\n[hardened] DONE in {elapsed:.1f}s ({rate:.2f} PDB/sec)", flush=True)
    print(f"  ok:    {n_ok}", flush=True)
    print(f"  skip:  {n_skip} (already valid)", flush=True)
    print(f"  fail:  {n_fail} (see {args.failure_log})", flush=True)

    if n_fail > 0:
        # Summarize failure modes
        print(f"\n[hardened] failure breakdown (top reasons):", flush=True)
        from collections import Counter
        reasons = Counter()
        with args.failure_log.open() as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    err = rec.get("error", rec.get("status", "unknown"))
                    # Bucket by the first few words of the error
                    bucket = " ".join(err.split()[:4]) if isinstance(err, str) else "unknown"
                    reasons[bucket] += 1
                except Exception:
                    pass
        for reason, count in reasons.most_common(10):
            print(f"  {count:>5}  {reason}", flush=True)

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
