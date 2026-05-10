#!/bin/bash
# Encrypt a single secret value for use as a *_ENCRYPTED env var on
# Salad (or any cloud platform that exposes env vars in plaintext).
#
# Usage:
#   ./encrypt_env.sh "actual-secret-value"
#   echo "actual-secret-value" | ./encrypt_env.sh
#
# Output (single line):
#   ENCv1:<base64-blob>
#
# Paste this output (including the ENCv1: prefix) as the value of an env var
# named with a _ENCRYPTED suffix, e.g.:
#   CLOUDFLARE_R2_ACCESS_KEY_ID_ENCRYPTED=ENCv1:U2FsdGVk...
#
# At container startup, decrypt_env_vars.sh (baked into the image at
# /opt/protenix-tools/) will scan for *_ENCRYPTED vars, decrypt them with
# the same hardcoded passphrase, and export the cleartext as a new var
# without the _ENCRYPTED suffix.
#
# IMPORTANT: This is security-through-obscurity, not real cryptography.
# The passphrase is baked into the public Docker image, so anyone who
# pulls the image can extract it. Use this to hide secrets from casual
# observation in the Salad UI / dashboard / API responses, NOT to defeat
# determined attackers.
#
# Crypto: AES-256-CBC + PBKDF2 (100k iterations, SHA-256) + random salt.

set -e

PASSPHRASE="Protenix"
PBKDF2_ITERATIONS=100000

if [ -n "$1" ]; then
    # Arg takes priority over stdin (works in both interactive and scripted contexts)
    VALUE="$1"
elif [ ! -t 0 ]; then
    # Arg not provided AND stdin is piped — read from stdin
    VALUE=$(cat)
else
    echo "Usage: $0 \"value-to-encrypt\"" >&2
    echo "   or: echo \"value\" | $0" >&2
    exit 1
fi

if [ -z "$VALUE" ]; then
    echo "Empty input — refusing to encrypt nothing" >&2
    exit 1
fi

ENC=$(printf '%s' "$VALUE" | openssl enc -aes-256-cbc -pbkdf2 -iter $PBKDF2_ITERATIONS \
        -salt -base64 -A -pass "pass:$PASSPHRASE")

echo "ENCv1:$ENC"
