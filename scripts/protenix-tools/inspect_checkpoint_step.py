#!/usr/bin/env python3
"""Inspect the 'step' field stored inside one or more Protenix checkpoint .pt files.

Loads each checkpoint with weights_only=False (the saved dict structure includes
self.step as a top-level key — see runner/train.py line 284 in Protenix).

Usage:
    python inspect_checkpoint_step.py <ckpt1.pt> [<ckpt2.pt> ...]
"""
import argparse
import sys
from pathlib import Path

import torch


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("checkpoints", nargs="+", type=Path,
                    help="Paths to .pt checkpoint files")
    args = ap.parse_args()

    width = max(len(str(p)) for p in args.checkpoints)
    for path in args.checkpoints:
        try:
            ckpt = torch.load(path, map_location="cpu", weights_only=False)
            step = ckpt.get("step", "<no step key>")
            keys = sorted(ckpt.keys())
            filename_step = path.stem.split("_")[0]  # 4998 from "4998.pt" or "4998_ema_0.999.pt"
            try:
                filename_step = int(filename_step)
            except ValueError:
                filename_step = "?"
            match = "MATCH" if step == filename_step else "MISMATCH"
            print(f"{str(path):<{width}s}  filename_step={filename_step}  stored_step={step}  {match}  top_keys={keys}")
        except Exception as e:
            print(f"{path}: ERROR — {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
