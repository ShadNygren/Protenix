"""Decrypt encrypted structure files on container startup.

Run at training-start, BEFORE Protenix imports any structure data. Decrypts
the .age files in --in-dir to --out-dir using the DEK from load_dek().

Activates only if env var PROTENIX_DECRYPT_STRUCTURES=true. If not set, this
script is a no-op (just logs "skipping").

Usage in entrypoint:
    if [ "$PROTENIX_DECRYPT_STRUCTURES" = "true" ]; then
        python3 /opt/protenix-tools/decrypt_structures.py \\
            --in-dir /workspace/encrypted-structures \\
            --out-dir /workspace/structures
    fi

Typical training-loop integration: configure Protenix to read structures from
$PROTENIX_STRUCTURES_DIR (default /workspace/structures), so the decryption
output dir is what Protenix consumes.

Pairing:
  Customer side: encrypt_structures_for_upload.py → uploads .age files to R2
  Container side: aws s3 sync s3://bucket/encrypted/ /workspace/encrypted-structures/
  Container side: this script decrypts /workspace/encrypted-structures/*.age
                  → /workspace/structures/

For non-proprietary public PDB structures the entire flow is skipped (env var
not set), and structures are downloaded cleartext-from-R2 as they always were.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from secure_checkpoint import decrypt_disk_to_bytes, load_dek


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in-dir", type=Path, default=Path("/workspace/encrypted-structures"),
                    help="dir containing .age files (default: /workspace/encrypted-structures)")
    ap.add_argument("--out-dir", type=Path, default=Path("/workspace/structures"),
                    help="dir for decrypted output (default: /workspace/structures)")
    ap.add_argument("--force", action="store_true",
                    help="ignore PROTENIX_DECRYPT_STRUCTURES env var (always decrypt)")
    args = ap.parse_args()

    if not args.force and os.environ.get("PROTENIX_DECRYPT_STRUCTURES", "").lower() != "true":
        print("[decrypt_structures] PROTENIX_DECRYPT_STRUCTURES not set — skipping",
              file=sys.stderr)
        return 0

    dek, source = load_dek()
    if dek is None:
        print("[decrypt_structures] ERROR: PROTENIX_DECRYPT_STRUCTURES=true but no DEK found",
              file=sys.stderr)
        return 2
    print(f"[decrypt_structures] DEK from {source}", file=sys.stderr)

    if not args.in_dir.exists():
        print(f"[decrypt_structures] WARN: {args.in_dir} does not exist — nothing to do",
              file=sys.stderr)
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)

    encrypted = sorted(args.in_dir.glob("*.age"))
    if not encrypted:
        print(f"[decrypt_structures] no .age files in {args.in_dir} — nothing to do",
              file=sys.stderr)
        return 0

    total_in = 0
    total_out = 0
    t0 = time.time()
    for enc_path in encrypted:
        # Strip .age suffix for output
        out_path = args.out_dir / enc_path.stem  # foo.zip.age → foo.zip
        if out_path.exists():
            print(f"[decrypt_structures] skip {out_path} (already exists)",
                  file=sys.stderr)
            continue
        plaintext = decrypt_disk_to_bytes(enc_path, dek)
        out_path.write_bytes(plaintext)
        total_in += enc_path.stat().st_size
        total_out += len(plaintext)
        print(f"[decrypt_structures] {enc_path.name} ({enc_path.stat().st_size:,} B) → "
              f"{out_path.name} ({len(plaintext):,} B)", file=sys.stderr)

    elapsed = time.time() - t0
    rate = (total_in / 1024 / 1024) / max(elapsed, 0.001)
    print(f"[decrypt_structures] done: {len(encrypted)} files, "
          f"{total_in:,} → {total_out:,} bytes in {elapsed:.1f}s ({rate:.1f} MB/s)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
