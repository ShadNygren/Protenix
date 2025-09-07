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

# If no command is provided, start an interactive bash shell
if [ $# -eq 0 ]; then
    echo "Starting interactive shell..."
    echo "To run Protenix inference, use the appropriate Python scripts."
    echo ""
    exec /bin/bash
else
    # Execute the provided command
    exec "$@"
fi