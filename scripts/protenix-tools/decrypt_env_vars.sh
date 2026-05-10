#!/bin/bash
# Decrypt all *_ENCRYPTED env vars and re-export them without the suffix.
#
# Designed to be SOURCED (not exec'd) from docker-entrypoint.sh BEFORE the
# main command runs. Sourcing means the exports persist in the current shell
# and inherit into any child process started after.
#
# How it works:
#   1. Walk every env var whose name ends in _ENCRYPTED
#   2. For each, expect the value format: ENCv1:<base64>
#   3. Decrypt with AES-256-CBC + PBKDF2 (100k iter) + the baked passphrase
#   4. Export decrypted value as a new var with the _ENCRYPTED suffix removed
#   5. Leave the original *_ENCRYPTED var in place (so it's still inspectable)
#
# Example:
#   Input env:  CLOUDFLARE_R2_ACCESS_KEY_ID_ENCRYPTED=ENCv1:U2FsdGVk...
#   After this: CLOUDFLARE_R2_ACCESS_KEY_ID=<plaintext>
#               (and the _ENCRYPTED version remains)
#
# Failure modes:
#   - Wrong format (no ENCv1: prefix): WARN and skip
#   - Decryption fails (wrong passphrase, corrupted ciphertext, tampering):
#     ERROR and skip; the unprefixed var is NOT exported
#
# IMPORTANT: This is security-through-obscurity, not real cryptography.
# The passphrase is baked into this script (which lives in a public Docker
# image), so anyone who pulls the image can extract it. The protection is
# against casual observation (Salad UI, accidental log capture, API GET
# responses), NOT against determined attackers.

PASSPHRASE="Protenix"
PBKDF2_ITERATIONS=100000

# Find all *_ENCRYPTED vars (strict match: end of name)
encrypted_vars=$(env | grep -oE '^[A-Z][A-Z0-9_]*_ENCRYPTED=' | sed 's/=$//' | sort -u)

if [ -z "$encrypted_vars" ]; then
    # Nothing to do
    return 0 2>/dev/null || exit 0
fi

decrypted_count=0
failed_count=0

for varname in $encrypted_vars; do
    encrypted_value="${!varname}"

    # Sanity-check the format
    if [[ "$encrypted_value" != ENCv1:* ]]; then
        echo "[decrypt_env_vars] WARN: $varname does not start with ENCv1: prefix, skipping" >&2
        failed_count=$((failed_count + 1))
        continue
    fi

    # Strip the prefix
    base64_blob="${encrypted_value#ENCv1:}"

    # Decrypt
    decrypted=$(printf '%s' "$base64_blob" | \
        openssl enc -aes-256-cbc -pbkdf2 -iter $PBKDF2_ITERATIONS \
                    -salt -base64 -A -d -pass "pass:$PASSPHRASE" 2>/dev/null)
    rc=$?

    if [ $rc -ne 0 ]; then
        echo "[decrypt_env_vars] ERROR: failed to decrypt $varname (wrong passphrase or corrupted ciphertext)" >&2
        failed_count=$((failed_count + 1))
        continue
    fi

    # Export decrypted value as new var without _ENCRYPTED suffix
    new_varname="${varname%_ENCRYPTED}"
    export "$new_varname"="$decrypted"

    # Log without revealing the value
    echo "[decrypt_env_vars] decrypted $varname -> $new_varname (length=${#decrypted})" >&2
    decrypted_count=$((decrypted_count + 1))
done

if [ $decrypted_count -gt 0 ] || [ $failed_count -gt 0 ]; then
    echo "[decrypt_env_vars] summary: $decrypted_count decrypted, $failed_count failed" >&2
fi
