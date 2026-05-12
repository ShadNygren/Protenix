"""Encrypt a structure file (CIF, PDB, ZIP of either, ...) for R2 upload.

Run on the customer's / data-owner's laptop BEFORE uploading proprietary
structures to a shared R2 bucket. Uses age-passphrase mode with the same DEK
that protects training checkpoints, so a single DEK manages both directions of
the data flow.

This is intentionally generic — it doesn't care whether the input is a single
mmCIF, a ZIP of many CIFs, an HDF5, etc. It just encrypts the bytes.

Usage:
    # Single file
    python encrypt_structures_for_upload.py \\
        --in proprietary_targets.zip \\
        --out proprietary_targets.zip.age

    # With explicit key file (default: ~/.protenix-data-key)
    python encrypt_structures_for_upload.py \\
        --in raw.cif --out raw.cif.age \\
        --key-file /path/to/dek.hex

    # Upload to R2 in one step (requires aws CLI configured with R2 endpoint)
    python encrypt_structures_for_upload.py --in raw.cif --out /tmp/raw.cif.age
    aws --endpoint-url https://ACCOUNT.r2.cloudflarestorage.com \\
        --region auto s3 cp /tmp/raw.cif.age \\
        s3://your-bucket/encrypted/raw.cif.age

Decryption side (in the container, on training startup):
    See decrypt_structures.py — reads PROTENIX_DEK + PROTENIX_DECRYPT_STRUCTURES=true
    and decrypts to /workspace/structures/ before training begins.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from secure_checkpoint import encrypt_bytes_to_disk


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="input", required=True, type=Path)
    ap.add_argument("--out", dest="output", required=True, type=Path)
    ap.add_argument("--key-file", type=Path,
                    default=Path.home() / ".protenix-data-key",
                    help="hex DEK file (default: ~/.protenix-data-key)")
    args = ap.parse_args()

    if not args.key_file.exists():
        print(f"ERROR: DEK file {args.key_file} not found. Create one with:",
              file=sys.stderr)
        print("  python -c 'import secrets; print(secrets.token_hex(32))' "
              "> ~/.protenix-data-key", file=sys.stderr)
        print("  chmod 600 ~/.protenix-data-key", file=sys.stderr)
        return 2

    dek = bytes.fromhex(args.key_file.read_text().strip())
    if not args.input.exists():
        print(f"ERROR: input file {args.input} not found", file=sys.stderr)
        return 2

    plaintext = args.input.read_bytes()
    n = encrypt_bytes_to_disk(plaintext, dek, args.output)
    print(f"[encrypt_structures] {args.input.stat().st_size:,} bytes → "
          f"{n:,} bytes at {args.output}", file=sys.stderr)
    print(f"[encrypt_structures] DEK from {args.key_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
