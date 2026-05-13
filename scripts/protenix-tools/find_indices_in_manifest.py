"""Search the R2 master manifest for files matching a regex.

Used to locate Protenix training indices CSVs and similar artifacts when their
canonical R2 path isn't known yet. Reads the manifest JSON downloaded from
s3://vh-protenix-training/metadata/master_manifest_*.json and walks the tree
emitting any string value matching the pattern, with its full JSON path.

Usage:
    python find_indices_in_manifest.py --manifest /tmp/mm.json
    python find_indices_in_manifest.py --manifest /tmp/mm.json --pattern '\\.csv'
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def walk(obj, pattern: re.Pattern, path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk(v, pattern, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk(v, pattern, f"{path}[{i}]")
    elif isinstance(obj, str):
        if pattern.search(obj):
            print(f"  {path}: {obj[:140]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True,
                    help="Local path to the manifest JSON")
    ap.add_argument("--pattern", default=r"indices|weightedPDB|\.csv",
                    help="Regex (case-insensitive) to search string values for")
    ap.add_argument("--show-keys", action="store_true",
                    help="Also print the top-level keys of the manifest")
    args = ap.parse_args()

    if not args.manifest.exists():
        print(f"ERROR: manifest not found: {args.manifest}", file=sys.stderr)
        return 2

    with args.manifest.open() as fh:
        m = json.load(fh)

    if args.show_keys:
        if isinstance(m, dict):
            print("top-level keys:", list(m.keys())[:30])

    pattern = re.compile(args.pattern, re.IGNORECASE)
    print(f"search pattern: {pattern.pattern}")
    walk(m, pattern)
    return 0


if __name__ == "__main__":
    sys.exit(main())
