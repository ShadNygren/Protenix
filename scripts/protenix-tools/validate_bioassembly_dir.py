"""Audit an existing bioassembly directory for corruption / suspicious files.

Reads every .pkl.gz in the directory and validates:
  * file is non-zero and >= min-size-bytes
  * gzip decompresses cleanly
  * pickle unmarshals to a dict
  * required keys are present (pdb_id, atom_array, entity_poly_type)

Outputs:
  * JSONL of {pdb_id, status, reason, size_bytes} for every file checked
  * Summary table: total / ok / suspect / corrupt
  * Optional: per-block coverage stats if --block-assignments is supplied

This runs FAST (validation only, no preprocessing) — useful as a preflight
before training to catch silent corruption that prepare_training_data.py
might have produced without raising.

Usage:
    python validate_bioassembly_dir.py \\
        --bio-dir /data/training/general_pdb/bioassembly \\
        --output-jsonl /data/training/general_pdb/validate_$(date +%Y%m%d).jsonl \\
        --min-size-bytes 5000 \\
        --block-assignments /data/training/general_pdb/metadata/block_assignments_full.json \\
        --n-cpu 32
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import pickle
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from joblib import Parallel, delayed  # type: ignore
from tqdm import tqdm  # type: ignore

REQUIRED_KEYS = {"pdb_id", "atom_array", "entity_poly_type"}


def validate_one(path: Path, min_size_bytes: int) -> dict:
    """Return a status dict for one .pkl.gz file (never raises)."""
    pdb_id = path.stem.replace(".pkl.gz", "").replace(".pkl", "")
    out = {"pdb_id": pdb_id, "path": str(path)}
    try:
        size = path.stat().st_size
        out["size_bytes"] = size
    except OSError as e:
        out["status"] = "stat-failed"
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    if size == 0:
        out["status"] = "zero-byte"
        return out
    if size < min_size_bytes:
        out["status"] = "too-small"
        out["error"] = f"size {size} < min {min_size_bytes}"
        return out

    try:
        with gzip.open(path, "rb") as fh:
            data = pickle.load(fh)
    except (gzip.BadGzipFile, EOFError) as e:
        out["status"] = "corrupt-gzip"
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    except (pickle.UnpicklingError, ValueError, AttributeError) as e:
        out["status"] = "unpickle-failed"
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    except Exception as e:
        out["status"] = "unknown-error"
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    if not isinstance(data, dict):
        out["status"] = "not-a-dict"
        out["error"] = f"got {type(data).__name__}"
        return out

    missing = REQUIRED_KEYS - set(data.keys())
    if missing:
        out["status"] = "missing-keys"
        out["error"] = f"missing: {sorted(missing)}"
        return out

    # Optional structural sanity checks
    aa = data.get("atom_array")
    if aa is not None and hasattr(aa, "__len__") and len(aa) == 0:
        out["status"] = "empty-atom-array"
        out["n_atoms"] = 0
        return out

    out["status"] = "ok"
    if aa is not None and hasattr(aa, "__len__"):
        out["n_atoms"] = len(aa)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bio-dir", type=Path, required=True,
                    help="Directory of .pkl.gz bioassembly files to validate")
    ap.add_argument("--output-jsonl", type=Path, required=True,
                    help="Output: per-file JSONL result records")
    ap.add_argument("--min-size-bytes", type=int, default=5000,
                    help="Files below this are flagged as suspect (default: 5000)")
    ap.add_argument("--n-cpu", type=int, default=os.cpu_count() or 1,
                    help=f"Parallel workers (default: {os.cpu_count()})")
    ap.add_argument("--block-assignments", type=Path, default=None,
                    help="Optional JSON file to compute per-block coverage stats")
    ap.add_argument("--limit", type=int, default=0,
                    help="Validate at most N files (testing)")
    args = ap.parse_args()

    print(f"[validate] scanning {args.bio_dir}", flush=True)
    t0 = time.time()
    files = sorted(args.bio_dir.glob("*.pkl.gz"))
    if args.limit > 0:
        files = files[:args.limit]
    print(f"[validate] {len(files)} files, n_cpu={args.n_cpu}", flush=True)

    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    args.output_jsonl.write_text("")  # truncate

    status_counts: Counter = Counter()
    sizes_by_status: dict[str, list[int]] = defaultdict(list)

    results = Parallel(n_jobs=args.n_cpu, return_as="generator_unordered")(
        delayed(validate_one)(p, args.min_size_bytes) for p in files
    )

    with args.output_jsonl.open("a") as out_fh:
        for r in tqdm(results, total=len(files)):
            status = r.get("status", "unknown")
            status_counts[status] += 1
            sizes_by_status[status].append(r.get("size_bytes", 0))
            json.dump(r, out_fh, default=str)
            out_fh.write("\n")

    elapsed = time.time() - t0
    print(f"\n[validate] done in {elapsed:.1f}s", flush=True)
    print(f"\nStatus breakdown:", flush=True)
    for status in sorted(status_counts.keys(), key=lambda s: -status_counts[s]):
        n = status_counts[status]
        sizes = sizes_by_status[status]
        if sizes:
            median_kb = sorted(sizes)[len(sizes)//2] / 1024
            print(f"  {status:25s} n={n:>6}  median={median_kb:>8.1f} KB", flush=True)
        else:
            print(f"  {status:25s} n={n:>6}", flush=True)

    # Per-block coverage
    if args.block_assignments and args.block_assignments.exists():
        print(f"\nPer-block coverage:", flush=True)
        with args.block_assignments.open() as fh:
            ba = json.load(fh)
        assignments = ba.get("assignments", ba)

        # Build ok-set from the JSONL we just wrote
        ok_ids: set[str] = set()
        with args.output_jsonl.open() as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    if rec.get("status") == "ok":
                        ok_ids.add(rec.get("pdb_id", "").lower())
                except Exception:
                    pass

        block_totals: dict[int, int] = defaultdict(int)
        block_ok: dict[int, int] = defaultdict(int)
        for pid, b in assignments.items():
            b = int(b)
            block_totals[b] += 1
            if pid.lower() in ok_ids:
                block_ok[b] += 1

        print(f"  {'Block':>5}  {'Assigned':>10}  {'Valid':>10}  {'Coverage':>10}", flush=True)
        for b in sorted(block_totals):
            total = block_totals[b]
            ok = block_ok.get(b, 0)
            pct = 100.0 * ok / total if total else 0
            flag = ""
            if pct == 0:
                flag = "  ← EMPTY"
            elif pct < 50:
                flag = "  ← BROKEN"
            elif pct < 90:
                flag = "  ← PARTIAL"
            print(f"   {b:02d}     {total:>10}  {ok:>10}  {pct:>9.1f}%{flag}", flush=True)

    return 0 if status_counts.get("ok", 0) == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
