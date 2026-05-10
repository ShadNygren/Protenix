#!/bin/bash
# Encrypt a secret value as an ENCv1: blob suitable for use as a *_ENCRYPTED
# env var on Salad / RunPod / any platform that injects env vars into the
# container at runtime.
#
# Passphrase derivation (must match decrypt_env_vars.sh exactly):
#   passphrase = SHA256(SSH public key content, no trailing newline)
#
# Usage:
#   ./encrypt_env.sh "secret-value"
#   echo "secret" | ./encrypt_env.sh
#
#   # Override which public key to use (defaults: ~/.ssh/id_ed25519.pub
#   # then ~/.ssh/id_rsa.pub):
#   ENCRYPT_PUBLIC_KEY_FILE=~/.ssh/some.pub ./encrypt_env.sh "secret"
#
# Output (one line to stdout):
#   ENCv1:<base64-blob>
#
# Paste that as the value of a *_ENCRYPTED env var. At container startup,
# /opt/protenix-tools/decrypt_env_vars.sh (sourced from docker-entrypoint.sh)
# reads the SSH public key from $PUBLIC_KEY (Salad/RunPod inject this) or
# from /root/.ssh/authorized_keys, derives the same passphrase, and exports
# the decrypted cleartext as a new var without the _ENCRYPTED suffix.
#
# Per-deployment isolation: different SSH key = different passphrase, so
# secrets encrypted for User A's key cannot be decrypted in User B's
# container even if both pull the same Docker image.
#
# Crypto: AES-256-CBC + PBKDF2 (100k iter, SHA-256) + random salt per
# encryption. Standard openssl primitives, no special dependencies.

set -e

PBKDF2_ITERATIONS=100000

# === Locate the public key ===
if [ -n "$ENCRYPT_PUBLIC_KEY_FILE" ] && [ -f "$ENCRYPT_PUBLIC_KEY_FILE" ]; then
    KEY_CONTENT=$(cat "$ENCRYPT_PUBLIC_KEY_FILE")
    KEY_SOURCE="$ENCRYPT_PUBLIC_KEY_FILE"
elif [ -f "$HOME/.ssh/id_ed25519.pub" ]; then
    KEY_CONTENT=$(cat "$HOME/.ssh/id_ed25519.pub")
    KEY_SOURCE="$HOME/.ssh/id_ed25519.pub"
elif [ -f "$HOME/.ssh/id_rsa.pub" ]; then
    KEY_CONTENT=$(cat "$HOME/.ssh/id_rsa.pub")
    KEY_SOURCE="$HOME/.ssh/id_rsa.pub"
else
    echo "ERROR: no SSH public key found." >&2
    echo "  Tried: \$ENCRYPT_PUBLIC_KEY_FILE, ~/.ssh/id_ed25519.pub, ~/.ssh/id_rsa.pub" >&2
    echo "  Generate one: ssh-keygen -t ed25519" >&2
    exit 2
fi

# Derive passphrase from public key content (no trailing newline)
PASSPHRASE=$(printf '%s' "$KEY_CONTENT" | sha256sum | awk '{print $1}')
KEY_FP=$(printf '%s' "$KEY_CONTENT" | sha256sum | cut -c1-12)
echo "[encrypt_env] using public key: $KEY_SOURCE  (sha256:${KEY_FP}...)" >&2

# === Determine value to encrypt ===
if [ -n "$1" ]; then
    VALUE="$1"
elif [ ! -t 0 ]; then
    VALUE=$(cat)
else
    echo "Usage: $0 \"value-to-encrypt\"" >&2
    echo "   or: echo \"value\" | $0" >&2
    exit 1
fi

if [ -z "$VALUE" ]; then
    echo "ERROR: empty input — refusing to encrypt nothing" >&2
    exit 1
fi

# === Encrypt ===
ENC=$(printf '%s' "$VALUE" | openssl enc -aes-256-cbc -pbkdf2 -iter $PBKDF2_ITERATIONS \
        -salt -base64 -A -pass "pass:$PASSPHRASE")

echo "ENCv1:$ENC"
