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

# Check if we're running in RunPod environment
if [ -n "$RUNPOD_POD_ID" ] || [ -n "$RUNPOD_DC_ID" ]; then
    echo "Detected RunPod environment (Pod: ${RUNPOD_POD_ID})"

    # Set up SSH authorized keys from PUBLIC_KEY env var (RunPod Full SSH method)
    mkdir -p /root/.ssh && chmod 700 /root/.ssh
    if [ -n "$PUBLIC_KEY" ]; then
        echo "$PUBLIC_KEY" > /root/.ssh/authorized_keys
        chmod 600 /root/.ssh/authorized_keys
        echo "SSH key installed from PUBLIC_KEY env var"
    elif [ -n "$SSH_PUBLIC_KEY" ]; then
        echo "$SSH_PUBLIC_KEY" > /root/.ssh/authorized_keys
        chmod 600 /root/.ssh/authorized_keys
        echo "SSH key installed from SSH_PUBLIC_KEY env var"
    else
        echo "WARNING: No PUBLIC_KEY env var — SSH will require manual key setup via Web Terminal"
    fi

    # Start SSH service
    echo "Starting SSH daemon for RunPod access..."
    if command -v sshd &> /dev/null; then
        service ssh start 2>/dev/null || /usr/sbin/sshd || true
    fi

    echo "Container ready for connections"
    echo "SSH: ssh root@${RUNPOD_PUBLIC_IP} -p ${RUNPOD_TCP_PORT_22}"
    echo "To run Protenix inference, use the appropriate Python scripts in /workspace"

    # Keep container running with a sleep loop
    while true; do
        sleep 3600
    done
elif [ $# -eq 0 ]; then
    # No command provided and not in RunPod - start interactive shell
    echo "Starting interactive shell..."
    echo "To run Protenix inference, use the appropriate Python scripts."
    echo ""
    exec /bin/bash
else
    # Execute the provided command
    exec "$@"
fi
