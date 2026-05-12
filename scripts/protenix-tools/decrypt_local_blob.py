"""Decrypt an age-passphrase .age blob back to cleartext.

For local-disk recovery: when the checkpoint watcher saved an encrypted copy of
a checkpoint to disk before successfully uploading to R2, and you need the
cleartext (e.g., R2 upload failed; pod died before reaching R2; you want to
verify a local encrypted blob matches what's in R2).

Run this on your laptop (where the DEK lives) with the .age file copied down
via scp from the Salad container, or run it inside the container if the DEK is
still present in env/tmpfs.

Usage:
    # On your laptop, with DEK in ~/.protenix-data-key:
    python decrypt_local_blob.py --in 9998.pt.age --out 9998.pt

    # With explicit key file:
    python decrypt_local_blob.py --in blob.age --out plain.bin \\
        --key-file /path/to/dek.hex

The .age format is portable. You can also decrypt with the upstream `age` CLI:
    age --decrypt blob.age > plain.bin    # prompts for passphrase
"""
import argparse
import sys
from pathlib import Path

# Add this script's directory to sys.path so secure_checkpoint is importable
# whether the user runs `python decrypt_local_blob.py` or `python -m ...`.
sys.path.insert(0, str(Path(__file__).parent))

from secure_checkpoint import decrypt_disk_to_bytes, load_dek


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="input", required=True, type=Path,
                    help="encrypted .age file")
    ap.add_argument("--out", dest="output", required=True, type=Path,
                    help="cleartext output path")
    ap.add_argument("--key-file", type=Path,
                    help="hex DEK file (default: ~/.protenix-data-key, then env/SSH-delivered)")
    args = ap.parse_args()

    # Resolve DEK
    dek_file = args.key_file or Path.home() / ".protenix-data-key"
    if dek_file.exists():
        dek = bytes.fromhex(dek_file.read_text().strip())
        source = f"file:{dek_file}"
    else:
        dek, source = load_dek()
        if dek is None:
            print(f"ERROR: no DEK available. Tried: {dek_file}, env, /dev/shm",
                  file=sys.stderr)
            return 2

    print(f"[decrypt_local_blob] DEK source: {source}", file=sys.stderr)
    plaintext = decrypt_disk_to_bytes(args.input, dek)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(plaintext)
    print(f"[decrypt_local_blob] {args.input.stat().st_size} bytes → "
          f"{len(plaintext)} bytes at {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
