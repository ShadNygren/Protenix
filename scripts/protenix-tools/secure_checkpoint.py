"""Secure checkpoint helpers: encrypt to disk, upload cleartext to R2.

Used by checkpoint_watcher.py and any other script handling sensitive
training artifacts. Provides:

  * load_dek()                      — cascade source for the data-encryption key
  * encrypt_bytes_to_disk()         — write age-passphrase blob to a file
  * decrypt_disk_to_bytes()         — read age-passphrase blob back to bytes
  * encrypt_file_in_place()         — read a file, write <path>.age, optionally
                                      delete the cleartext original
  * upload_cleartext_to_r2()        — boto3 multipart upload (streams from disk)

Encryption format: age (https://age-encryption.org) in passphrase mode. The DEK
is used as the passphrase; age internally derives a key via scrypt with default
work factor. Output begins with the "age-encryption.org/v1" header and is
portable to the upstream `age` CLI for decryption.

Why age and not openssl: standard modern file-encryption format, conservative
KDF (scrypt vs PBKDF2-SHA256), authenticated encryption (ChaCha20-Poly1305 vs
unauthenticated AES-256-CBC). Both work; age is the cleaner default for new
code.

DEK delivery cascade — load_dek() checks, in order:

  1. PROTENIX_DEK environment variable (typically populated by the entrypoint
     after decrypting PROTENIX_DEK_ENCRYPTED via the ENCv1 scheme).
  2. /dev/shm/protenix-dek file (SSH-delivered DEK on tmpfs / RAM-only).
  3. /run/protenix-dek file (alternate tmpfs path; for systemd-style hosts).

If none of these are present:

  * Returns (None, None) by default — caller proceeds without encryption.
  * If PROTENIX_REQUIRE_ENCRYPTION=true, raises SystemExit instead (fail-loud
    mode for production where missing encryption is a deploy bug).

DEK format: either hex (recommended; 64-char string for a 32-byte key) or raw
bytes. load_dek() returns bytes; helpers accept either bytes or str.
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

# Lazy import — pyrage isn't needed if the user disables encryption entirely.
_pyrage = None


def _pyrage_mod():
    global _pyrage
    if _pyrage is None:
        try:
            import pyrage  # type: ignore
        except ImportError as e:
            raise SystemExit(
                "pyrage is required for encrypted checkpoint handling but is "
                "not installed. `pip install pyrage`. Original error: %s" % e
            )
        _pyrage = pyrage
    return _pyrage


def load_dek():
    """Return (dek_bytes, source_label) or (None, None).

    See module docstring for the cascade order.
    """
    # 1. Env var (decrypted by entrypoint at startup)
    if dek_hex := os.environ.get("PROTENIX_DEK"):
        try:
            return bytes.fromhex(dek_hex), "env:PROTENIX_DEK"
        except ValueError:
            return dek_hex.encode(), "env:PROTENIX_DEK(raw)"

    # 2 + 3. SSH-delivered DEK in tmpfs
    for path in ("/dev/shm/protenix-dek", "/run/protenix-dek"):
        p = Path(path)
        if p.exists() and p.is_file():
            data = p.read_bytes().strip()
            try:
                return bytes.fromhex(data.decode()), f"file:{path}"
            except (ValueError, UnicodeDecodeError):
                return data, f"file:{path}"

    # No DEK found
    if os.environ.get("PROTENIX_REQUIRE_ENCRYPTION", "").lower() == "true":
        raise SystemExit(
            "PROTENIX_REQUIRE_ENCRYPTION=true but no DEK found.\n"
            "  Option 1: set PROTENIX_DEK env var to your 32-byte hex DEK.\n"
            "  Option 2: write the DEK to /dev/shm/protenix-dek via SSH "
            "(e.g. `ssh root@HOST -p PORT 'cat > /dev/shm/protenix-dek' < dek.txt`)."
        )

    return None, None


def _to_passphrase(dek):
    """Return age-passphrase string for a DEK that may be bytes or hex string."""
    if isinstance(dek, bytes):
        return dek.hex()
    return dek


def encrypt_bytes_to_disk(plaintext, dek, output_path):
    """Encrypt plaintext bytes with age-passphrase mode; write to output_path.

    Args:
        plaintext: bytes to encrypt (already in memory).
        dek: the data-encryption key as bytes or hex string.
        output_path: destination .age file. Parent dirs are created.

    Returns the ciphertext length in bytes.

    Note on pyrage API: the passphrase encrypt/decrypt functions live in the
    `pyrage.passphrase` submodule, NOT at top level. Earlier drafts of this
    file used `pyrage.passphrase_encrypt(...)` which doesn't exist — that was
    a mistake corrected in commit-after-v2-smoke-test.
    """
    pyrage = _pyrage_mod()
    ciphertext = pyrage.passphrase.encrypt(plaintext, _to_passphrase(dek))
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(ciphertext)
    return len(ciphertext)


def decrypt_disk_to_bytes(input_path, dek):
    """Read an age-passphrase .age file; return decrypted bytes."""
    pyrage = _pyrage_mod()
    ciphertext = Path(input_path).read_bytes()
    return pyrage.passphrase.decrypt(ciphertext, _to_passphrase(dek))


def encrypt_file_in_place(file_path, dek, delete_original=True):
    """Read `file_path`, write `file_path.age`, optionally delete the cleartext.

    Streams via memory — for files larger than available RAM, prefer a
    streaming encrypt approach with `openssl enc -aes-256-cbc -pbkdf2` instead
    (see SALAD.md "Streaming download + AES-256-CBC encryption pattern"). For
    typical Protenix checkpoints (~9 GB) on a 24 GB-RAM Salad node, in-memory
    is fine.

    Returns the path to the new .age file.
    """
    file_path = Path(file_path)
    plaintext = file_path.read_bytes()
    enc_path = file_path.with_suffix(file_path.suffix + ".age")
    encrypt_bytes_to_disk(plaintext, dek, enc_path)
    if delete_original:
        file_path.unlink()
    return enc_path


def upload_cleartext_to_r2(local_path, bucket, key, s3_client,
                           extra_metadata=None):
    """Upload `local_path` (cleartext) to R2 via boto3 multipart.

    s3_client is a pre-configured boto3 S3 client (see checkpoint_watcher.py
    `make_s3_client()` for the canonical setup pointing at CloudFlare R2 with
    s3v4 signing).

    extra_metadata: optional dict of strings merged into R2 object Metadata.
    """
    from boto3.s3.transfer import TransferConfig

    cfg = TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=10,
        use_threads=True,
    )
    metadata = {"uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if extra_metadata:
        metadata.update(extra_metadata)
    t0 = time.time()
    s3_client.upload_file(
        str(local_path), bucket, key,
        Config=cfg,
        ExtraArgs={"Metadata": metadata},
    )
    elapsed = time.time() - t0
    size = Path(local_path).stat().st_size
    rate_mbps = (size / 1024 / 1024) / max(elapsed, 0.001)
    return {"size": size, "elapsed_s": round(elapsed, 1),
            "rate_mb_per_s": round(rate_mbps, 1)}


def main():
    """CLI: encrypt or decrypt a single file. For ops/debug use.

    Usage:
        python -m secure_checkpoint encrypt --in file.pt --out file.pt.age
        python -m secure_checkpoint decrypt --in file.pt.age --out file.pt
    """
    import argparse

    ap = argparse.ArgumentParser(description="age-passphrase encrypt/decrypt one file")
    ap.add_argument("mode", choices=("encrypt", "decrypt"))
    ap.add_argument("--in", dest="input", required=True, type=Path)
    ap.add_argument("--out", dest="output", required=True, type=Path)
    ap.add_argument("--key-file", type=Path,
                    help="DEK file (hex). Defaults to env/SSH-delivered DEK.")
    args = ap.parse_args()

    if args.key_file:
        dek_bytes = bytes.fromhex(args.key_file.read_text().strip())
        source = f"file:{args.key_file}"
    else:
        dek_bytes, source = load_dek()
        if dek_bytes is None:
            print("ERROR: no DEK available (env or SSH-delivered)", file=sys.stderr)
            return 2

    print(f"[secure_checkpoint] DEK from {source}", file=sys.stderr)

    if args.mode == "encrypt":
        plaintext = args.input.read_bytes()
        n = encrypt_bytes_to_disk(plaintext, dek_bytes, args.output)
        print(f"Encrypted {len(plaintext)} → {n} bytes ({args.output})", file=sys.stderr)
    else:
        plaintext = decrypt_disk_to_bytes(args.input, dek_bytes)
        args.output.write_bytes(plaintext)
        print(f"Decrypted {args.input.stat().st_size} → {len(plaintext)} bytes ({args.output})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
