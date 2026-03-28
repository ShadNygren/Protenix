#!/bin/bash
# Docker entrypoint for Protenix
# Keeps container running for interactive use in RunPod and other cloud environments

set -e

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

echo "==================================="

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
