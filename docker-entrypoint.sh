#!/bin/bash
# Docker entrypoint for Protenix
# Keeps container running for interactive use in RunPod and other cloud environments

set -e

# === Decrypt any *_ENCRYPTED env vars before anything else ===
# Source the decrypt script so exports persist into this shell + child processes.
# See /opt/protenix-tools/decrypt_env_vars.sh and ../scripts/encrypt_env.sh.
# This is security-through-obscurity (passphrase derived from SHA256(SSH pubkey)),
# useful for hiding secrets from casual UI/dashboard inspection on platforms
# like RunPod where the pubkey is auto-injected. On Salad the pubkey isn't
# injected, so this scheme silently no-ops there — see the next section.
if [ -r /opt/protenix-tools/decrypt_env_vars.sh ]; then
    # shellcheck source=/dev/null
    source /opt/protenix-tools/decrypt_env_vars.sh || true
fi

# === SSH-delivered credentials (Salad-friendly DEK + R2 creds delivery) ===
# If the user SSH'd in and dropped creds into /dev/shm/secure/creds (tmpfs,
# RAM-only), source them now so subsequent training scripts inherit
# CLOUDFLARE_R2_*, PROTENIX_DEK, AWS_* etc. without touching host disk.
#
# Delivery pattern from the user's laptop:
#   ssh root@HOST -p PORT bash -s <<EOF
#   mkdir -p /dev/shm/secure && chmod 700 /dev/shm/secure
#   cat > /dev/shm/secure/creds <<INNER
#   export PROTENIX_DEK="<64-char hex>"
#   export CLOUDFLARE_R2_ACCESS_KEY_ID="..."
#   export CLOUDFLARE_R2_SECRET_ACCESS_KEY="..."
#   export CLOUDFLARE_R2_ENDPOINT="..."
#   INNER
#   chmod 600 /dev/shm/secure/creds
#   EOF
#
# The file is in tmpfs and evaporates on container restart — no host SSD trace.
if [ -r /dev/shm/secure/creds ]; then
    echo "[entrypoint] sourcing SSH-delivered credentials from /dev/shm/secure/creds"
    # shellcheck source=/dev/null
    source /dev/shm/secure/creds || true
fi

# === CloudWatch Logs streaming (optional) ===
# If AWS_CLOUDWATCH_LOG_GROUP is set (plus real AWS creds — separate from
# CloudFlare R2), configure Python's root logger to also push to CloudWatch.
# Skipped silently when not configured.
if [ -n "$AWS_CLOUDWATCH_LOG_GROUP" ] && [ -r /opt/protenix-tools/setup_cloudwatch_logging.py ]; then
    echo "[entrypoint] AWS_CLOUDWATCH_LOG_GROUP=$AWS_CLOUDWATCH_LOG_GROUP — initializing CloudWatch logging"
    python3 /opt/protenix-tools/setup_cloudwatch_logging.py --test 2>&1 | head -5 || true
fi

# === Optional: decrypt incoming structures before training ===
# Activates only if PROTENIX_DECRYPT_STRUCTURES=true and a DEK is available.
# Run as a quick check at boot; training itself will re-invoke if needed.
if [ "$PROTENIX_DECRYPT_STRUCTURES" = "true" ] && [ -r /opt/protenix-tools/decrypt_structures.py ]; then
    echo "[entrypoint] PROTENIX_DECRYPT_STRUCTURES=true — decrypting structures"
    python3 /opt/protenix-tools/decrypt_structures.py 2>&1 | tail -10 || true
fi

# Print environment info
echo "==================================="
echo "Protenix Docker Container Started"
echo "==================================="
echo "PyTorch version: $(python -c 'import torch; print(torch.__version__)' 2>/dev/null || echo 'Not available')"
echo "CUDA available: $(python -c 'import torch; print(torch.cuda.is_available())' 2>/dev/null || echo 'Not available')"
echo "GPU count: $(python -c 'import torch; print(torch.cuda.device_count())' 2>/dev/null || echo '0')"

# Check if weights are included
if [ "$PROTENIX_WEIGHTS_INCLUDED" = "true" ]; then
    echo "Weights: Pre-installed (${PROTENIX_WEIGHTS_VERSION})"
    echo "Model: ${PROTENIX_WEIGHTS_MODEL}"
else
    echo "Weights: Not included (will download at runtime)"
fi

echo ""

# Template support info
TEMPLATE_MMCIF_DIR="/root/mmcif"
TEMPLATE_DB="/root/search_database"
TEMPLATE_COUNT=$(find $TEMPLATE_MMCIF_DIR -name "*.cif" 2>/dev/null | wc -l)
echo "Templates: $TEMPLATE_COUNT CIF files in $TEMPLATE_MMCIF_DIR"
if [ -d "$TEMPLATE_DB" ]; then
    echo "Search DB: Installed at $TEMPLATE_DB"
else
    echo "Search DB: Not installed (use fetch_remote=true for PDBe on-demand fetching)"
fi
echo ""
echo "To use templates:"
echo "  1. SCP template CIF files: scp *.cif root@HOST:/root/mmcif/"
echo "  2. Run: protenix pred --input input.json --out_dir output/ --use_template true"
echo "  Note: Templates are fetched from PDBe on demand by default (fetch_remote=true)"
echo "  Note: Set PROTENIX_MAX_TEMPLATE_DATE env var to include recent structures"
echo "        (default cutoff is 2021-09-30; set to e.g. 2026-03-28 for all structures)"

echo "==================================="

# Apply custom max_template_date if set via env var
if [ -n "$PROTENIX_MAX_TEMPLATE_DATE" ]; then
    echo "Custom max_template_date: $PROTENIX_MAX_TEMPLATE_DATE"
fi

# Set working directory
cd /workspace 2>/dev/null || cd /root

# === Universal SSH setup ===
# Any orchestrator that supports interactive access (Salad, RunPod, k8s with
# initContainers, etc.) typically injects the user's SSH public key into a
# well-known env var. Set up authorized_keys + start sshd unconditionally so
# the container is reachable on whichever platform deployed it. Harmless if
# no key was provided — sshd just won't have any keys to authenticate.
mkdir -p /root/.ssh && chmod 700 /root/.ssh
SSH_KEY="${PUBLIC_KEY:-${SSH_PUBLIC_KEY:-}}"
if [ -n "$SSH_KEY" ]; then
    echo "$SSH_KEY" > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    echo "[entrypoint] SSH key installed from PUBLIC_KEY/SSH_PUBLIC_KEY env var"
else
    echo "[entrypoint] WARNING: no PUBLIC_KEY/SSH_PUBLIC_KEY env var — SSH will require manual key setup"
fi

if command -v sshd &>/dev/null; then
    service ssh start 2>/dev/null || /usr/sbin/sshd 2>/dev/null || true
    echo "[entrypoint] sshd started"
fi

# Helpful per-platform connection hint
if [ -n "$RUNPOD_POD_ID" ]; then
    echo "[entrypoint] RunPod pod: ssh root@${RUNPOD_PUBLIC_IP} -p ${RUNPOD_TCP_PORT_22}"
elif [ -n "$SALAD_MACHINE_ID" ]; then
    echo "[entrypoint] Salad machine: $SALAD_MACHINE_ID — open the Terminal from the Instances tab in the Salad portal"
fi

# === Optional User-Data style startup script ===
# PROTENIX_STARTUP_SCRIPT mirrors AWS EC2 User Data: if set, its contents are
# written to /tmp/protenix_startup.sh, made executable, and run in the
# background (so the container keeps running for SSH access even if the script
# exits quickly or fails).
#
# Use cases:
#   - Auto-launch a training run on a fresh Salad container (no manual SSH)
#   - Auto-launch a prediction job
#   - Stage data from R2 before sshd accepts connections
#   - Different workloads from the same image (training vs prediction vs eval)
#
# Failure handling: a non-zero exit from the user script is LOGGED but does
# NOT crash the container — the sleep-infinity fallback below still runs so
# SSH stays reachable for debugging. This is opposite to AWS User Data, which
# can brick an instance on a bad script; we explicitly trade off that
# strictness for keeping SSH access alive on a remote cloud GPU.
if [ -n "$PROTENIX_STARTUP_SCRIPT" ]; then
    USER_DATA_PATH=/tmp/protenix_startup.sh
    USER_DATA_LOG=/tmp/protenix_startup.log
    printf '%s\n' "$PROTENIX_STARTUP_SCRIPT" > "$USER_DATA_PATH"
    chmod +x "$USER_DATA_PATH"
    echo "[entrypoint] PROTENIX_STARTUP_SCRIPT received ($(wc -c < "$USER_DATA_PATH") bytes) → $USER_DATA_PATH"
    echo "[entrypoint] running startup script in background (log: $USER_DATA_LOG)"
    # setsid so the script has its own session and won't be killed when the
    # entrypoint's bash exits/replaces itself with sleep infinity. nohup +
    # </dev/null + disown so it survives if anything tries to HUP us.
    setsid nohup bash "$USER_DATA_PATH" </dev/null >"$USER_DATA_LOG" 2>&1 &
    disown $! 2>/dev/null || true
    echo "[entrypoint] startup script pid: $! (use 'tail -f $USER_DATA_LOG' to follow)"
fi

# === Decide what to run ===
# 1. Explicit non-trivial command via `docker run image <args>` → run it
# 2. No command (or trivial bash-only CMD) + interactive TTY → drop into bash
# 3. No command (or trivial bash-only CMD) + no TTY (orchestrator: Salad,
#    RunPod, k8s, ECS, Cloud Run, ...) → sleep forever so the container
#    stays alive for SSH-driven interactive work.
#
# We treat `CMD ["/bin/bash"]` or `CMD ["bash"]` as "no real command" because
# bash without a TTY exits immediately, defeating any orchestrator deploy.
# The Dockerfile in this repo intentionally omits CMD for that reason; this
# detection is defensive in case someone re-adds it later.
is_trivial_bash=0
if [ "$#" -eq 1 ] && { [ "$1" = "/bin/bash" ] || [ "$1" = "bash" ] || [ "$1" = "/bin/sh" ] || [ "$1" = "sh" ]; }; then
    is_trivial_bash=1
fi

if [ "$#" -gt 0 ] && [ "$is_trivial_bash" -eq 0 ]; then
    exec "$@"
elif [ -t 0 ]; then
    echo "[entrypoint] interactive TTY detected — starting bash"
    exec /bin/bash
else
    echo "[entrypoint] no command (or trivial bash CMD), no TTY — sleeping forever (use SSH for interactive work)"
    exec sleep infinity
fi
