#!/bin/bash
# Docker entrypoint for Protenix
# Keeps container running for interactive use in RunPod and other cloud environments

set -e

# === Decrypt any *_ENCRYPTED env vars before anything else ===
# Source the decrypt script so exports persist into this shell + child processes.
# See /opt/protenix-tools/decrypt_env_vars.sh and ../scripts/encrypt_env.sh.
# This is security-through-obscurity (passphrase baked in image), useful for
# hiding secrets from casual UI/dashboard inspection on platforms like Salad
# where env vars are otherwise visible in plaintext.
if [ -r /opt/protenix-tools/decrypt_env_vars.sh ]; then
    # shellcheck source=/dev/null
    source /opt/protenix-tools/decrypt_env_vars.sh || true
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

# === Decide what to run ===
# 1. Explicit command via Docker CMD or `docker run image <args>` → run it
# 2. No command + interactive TTY (`docker run -it`) → drop into bash
# 3. No command + no TTY (any container orchestrator: Salad, RunPod, k8s,
#    ECS, Cloud Run, ...) → sleep forever so the container stays alive for
#    SSH-driven interactive work. This is the universal fix for the
#    "container exits in 4 seconds with code 0" crash loop that every Docker
#    image hits when the orchestrator doesn't attach a TTY.
if [ $# -gt 0 ]; then
    exec "$@"
elif [ -t 0 ]; then
    echo "[entrypoint] interactive TTY detected — starting bash"
    exec /bin/bash
else
    echo "[entrypoint] no command, no TTY — sleeping forever (use SSH for interactive work)"
    exec sleep infinity
fi
