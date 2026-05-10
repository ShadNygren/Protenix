#!/usr/bin/env python3
"""Extract per-step loss values from a Protenix training.log file.

Parses lines like:
  2026-05-09 10:28:49,411 [/workspace/runner/train.py:384] INFO root: Step 71349 train metrics: {'train/...': np.float64(...), ..., 'train/loss.avg': np.float64(1.4397...), ...}

Outputs one row per logged step: step, loss, distogram_loss, pae_loss, plddt_loss
either as TSV (default) or as a summary table (--summary, every N steps).

Usage:
    python3 extract_training_loss.py /path/to/training.log
    python3 extract_training_loss.py /path/to/training.log --summary 500
    python3 extract_training_loss.py log1 log2 log3 --summary 1000
"""
import argparse
import re
import sys
from pathlib import Path

# Match: Step <N> train metrics: {dict-like text}
STEP_RE = re.compile(r"Step (\d+) train metrics:\s*(\{.*\})")
# Match a single field: 'train/<name>': np.float64(<value>)
FIELD_RE = re.compile(r"'train/([^']+)':\s*np\.float64\(([0-9.eE+-]+)\)")


def parse_log(path: Path) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = STEP_RE.search(line)
            if not m:
                continue
            step = int(m.group(1))
            body = m.group(2)
            metrics = {name: float(val) for name, val in FIELD_RE.findall(body)}
            if not metrics:
                continue
            row = {"step": step}
            for k in ("loss.avg", "distogram_loss.avg", "pae_loss.avg",
                     "plddt_loss.avg", "smooth_lddt_loss.avg",
                     "mse_loss.avg"):
                row[k] = metrics.get(k)
            rows.append(row)
    return rows


def print_summary(rows: list[dict], label: str, every_n: int) -> None:
    if not rows:
        print(f"  (no rows in {label})")
        return
    print(f"\n=== {label} ({len(rows)} log entries; printing every {every_n}th step) ===")
    print(f"  {'step':>8s} {'loss':>8s} {'disto':>8s} {'pae':>8s} {'plddt':>8s} {'lddt':>8s}")
    last = None
    selected = [r for r in rows if r['step'] % every_n < 10 or r is rows[-1]]
    for r in selected:
        if r is last:
            continue
        last = r
        print(f"  {r['step']:>8d} "
              f"{r.get('loss.avg', float('nan')):>8.4f} "
              f"{r.get('distogram_loss.avg', float('nan')):>8.4f} "
              f"{r.get('pae_loss.avg', float('nan')):>8.4f} "
              f"{r.get('plddt_loss.avg', float('nan')):>8.4f} "
              f"{r.get('smooth_lddt_loss.avg', float('nan')):>8.4f}")
    losses = [r['loss.avg'] for r in rows if r.get('loss.avg') is not None]
    if losses:
        print(f"  STATS: first={losses[0]:.4f}  last={losses[-1]:.4f}  "
              f"min={min(losses):.4f}  mean={sum(losses)/len(losses):.4f}  "
              f"delta={losses[-1]-losses[0]:+.4f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("logs", nargs="+", type=Path,
                    help="One or more training.log files")
    ap.add_argument("--summary", type=int, default=0,
                    help="Print summary table every N steps (default: print all)")
    args = ap.parse_args()

    for log in args.logs:
        rows = parse_log(log)
        if args.summary > 0:
            print_summary(rows, str(log), args.summary)
        else:
            print(f"\n=== {log} ===")
            print("step\tloss\tdistogram\tpae\tplddt\tlddt")
            for r in rows:
                print(f"{r['step']}\t"
                      f"{r.get('loss.avg', '')}\t"
                      f"{r.get('distogram_loss.avg', '')}\t"
                      f"{r.get('pae_loss.avg', '')}\t"
                      f"{r.get('plddt_loss.avg', '')}\t"
                      f"{r.get('smooth_lddt_loss.avg', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
