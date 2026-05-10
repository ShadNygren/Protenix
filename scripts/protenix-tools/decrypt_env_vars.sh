#!/bin/bash
# Decrypt all *_ENCRYPTED env vars and re-export them without the suffix.
#
# Sourced (not exec'd) by docker-entrypoint.sh BEFORE the main command runs.
# Sourcing means the exports persist into this shell + child processes.
#
# Passphrase derivation (must match encrypt_env.sh exactly):
#   passphrase = SHA256(SSH public key content, no trailing newline)
#
# Public key source (in priority order):
#   1. $PUBLIC_KEY env var (Salad / RunPod inject this for SSH access)
#   2. First non-empty line of /root/.ssh/authorized_keys (where
#      docker-entrypoint.sh writes $PUBLIC_KEY before sourcing this script)
#   3. /etc/protenix-decrypt-key (escape hatch for environments where
#      neither of the above is present)
#
# If no key is available, *_ENCRYPTED vars are LEFT UNTOUCHED. Downstream
# code that expects the unprefixed var will fail at use-time — intentional,
# so the missing-key condition is loud rather than silent.
#
# Per-deployment isolation: User A's container running with User A's SSH
# public key cannot decrypt secrets that User B encrypted with User B's
# public key, even when both pull the same image.

PBKDF2_ITERATIONS=100000

# === Locate the public key ===
KEY_CONTENT=""
KEY_SOURCE=""

if [ -n "$PUBLIC_KEY" ]; then
    KEY_CONTENT=$(printf '%s' "$PUBLIC_KEY")
    KEY_SOURCE="env var \$PUBLIC_KEY"
elif [ -s /root/.ssh/authorized_keys ]; then
    KEY_CONTENT=$(grep -v '^[[:space:]]*$' /root/.ssh/authorized_keys 2>/dev/null | head -1)
    KEY_SOURCE="/root/.ssh/authorized_keys"
elif [ -s /etc/protenix-decrypt-key ]; then
    KEY_CONTENT=$(cat /etc/protenix-decrypt-key)
    KEY_SOURCE="/etc/protenix-decrypt-key"
fi

if [ -z "$KEY_CONTENT" ]; then
    return 0 2>/dev/null || exit 0
fi

PASSPHRASE=$(printf '%s' "$KEY_CONTENT" | sha256sum | awk '{print $1}')
KEY_FP=$(printf '%s' "$KEY_CONTENT" | sha256sum | cut -c1-12)

# === Find all *_ENCRYPTED vars ===
encrypted_vars=$(env | grep -oE '^[A-Z][A-Z0-9_]*_ENCRYPTED=' | sed 's/=$//' | sort -u)

if [ -z "$encrypted_vars" ]; then
    return 0 2>/dev/null || exit 0
fi

echo "[decrypt_env_vars] using public key from $KEY_SOURCE  (sha256:${KEY_FP}...)" >&2

decrypted_count=0
failed_count=0

for varname in $encrypted_vars; do
    encrypted_value="${!varname}"

    if [[ "$encrypted_value" != ENCv1:* ]]; then
        echo "[decrypt_env_vars] WARN: $varname does not start with ENCv1: prefix, skipping" >&2
        failed_count=$((failed_count + 1))
        continue
    fi

    base64_blob="${encrypted_value#ENCv1:}"

    decrypted=$(printf '%s' "$base64_blob" | \
        openssl enc -aes-256-cbc -pbkdf2 -iter $PBKDF2_ITERATIONS \
                    -salt -base64 -A -d -pass "pass:$PASSPHRASE" 2>/dev/null)
    rc=$?

    if [ $rc -ne 0 ]; then
        echo "[decrypt_env_vars] ERROR: failed to decrypt $varname (wrong key or corrupted ciphertext)" >&2
        failed_count=$((failed_count + 1))
        continue
    fi

    new_varname="${varname%_ENCRYPTED}"
    export "$new_varname"="$decrypted"
    echo "[decrypt_env_vars] decrypted $varname -> $new_varname (length=${#decrypted})" >&2
    decrypted_count=$((decrypted_count + 1))
done

echo "[decrypt_env_vars] summary: $decrypted_count decrypted, $failed_count failed" >&2
