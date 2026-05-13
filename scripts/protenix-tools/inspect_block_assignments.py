"""Inspect the R2 block_assignments_full.json layout and find PDB IDs in a given block.

Used during smoke-test data staging to find PDB IDs that live in a specific
bioassembly block ZIP (so we can download a small block and have a matched
indices CSV).

Usage:
    python inspect_block_assignments.py --json /path/to/block_assignments_full.json
    python inspect_block_assignments.py --json ... --block 4 --n 10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def find_pdb_ids_in_block(ba, target_block: int, n: int) -> list[str]:
    """Return up to n PDB IDs in target_block, regardless of dict layout."""
    if not isinstance(ba, dict):
        return []
    sample_key = next(iter(ba))
    sample_val = ba[sample_key]
    # Layout 1: {pdb_id: block_index_or_label}
    if isinstance(sample_val, (int, str)) and not isinstance(sample_val, list):
        candidates = (str(target_block), f"{target_block:02d}", f"block{target_block:02d}", f"block{target_block}")
        return [k for k, v in ba.items() if str(v) in candidates][:n]
    # Layout 2: {block_label: [pdb_ids]}
    if isinstance(sample_val, list):
        for key in (target_block, str(target_block), f"{target_block:02d}",
                    f"block{target_block:02d}", f"block{target_block}"):
            if key in ba:
                return ba[key][:n]
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, required=True,
                    help="Path to block_assignments_full.json")
    ap.add_argument("--block", type=int, default=4,
                    help="Block index to query (default: 4, smallest known block)")
    ap.add_argument("--n", type=int, default=10,
                    help="Number of PDB IDs to print")
    args = ap.parse_args()

    if not args.json.exists():
        print(f"ERROR: not found: {args.json}", file=sys.stderr)
        return 2

    with args.json.open() as fh:
        ba = json.load(fh)

    if isinstance(ba, dict):
        sample_key = next(iter(ba))
        print(f"layout sample key: {sample_key!r} -> {ba[sample_key]!r}")
        print(f"total entries: {len(ba)}")
    else:
        print(f"non-dict root: {type(ba).__name__}")

    ids = find_pdb_ids_in_block(ba, args.block, args.n)
    print(f"block{args.block} sample PDB IDs ({len(ids)}):")
    for pid in ids:
        print(f"  {pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
