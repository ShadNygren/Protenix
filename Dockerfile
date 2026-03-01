# Build argument to select base image variant
# Options:
#   - runtime (default): 3.3GB base, for production deployments
#   - devel: 6.8GB base, includes CUDA toolkit, compilers, debuggers for development
# Usage: docker build --build-arg BASE_IMAGE_VARIANT=devel .
ARG BASE_IMAGE_VARIANT=runtime

# ============================================================================
# STAGE 1: Base Protenix image (runtime or devel)
# ============================================================================
FROM pytorch/pytorch:2.7.1-cuda12.6-cudnn9-${BASE_IMAGE_VARIANT} AS base

# Label the image with the variant used
LABEL org.opencontainers.image.description="Protenix with PyTorch ${BASE_IMAGE_VARIANT} base image"
LABEL org.opencontainers.image.variant="${BASE_IMAGE_VARIANT}"

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CUTLASS_PATH=/opt/cutlass

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        g++ \
        gcc \
        libc6-dev \
        make \
        postgresql \
        hmmer \
        kalign \
        wget \
        openssh-server \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Configure SSH for RunPod (they inject keys at runtime)
RUN mkdir -p /var/run/sshd && \
    echo "PermitRootLogin yes" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication no" >> /etc/ssh/sshd_config && \
    echo "PubkeyAuthentication yes" >> /etc/ssh/sshd_config && \
    echo "AuthorizedKeysFile /root/.ssh/authorized_keys" >> /etc/ssh/sshd_config

# Set working directory
WORKDIR /app

# Install Python dependencies
# Copy requirements.txt first to leverage Docker cache
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt -i https://pypi.org/simple

# Clone CUTLASS
RUN git clone -b v3.5.1 https://github.com/NVIDIA/cutlass.git /opt/cutlass

# ============================================================================
# STAGE 2: Weights Downloader (separate stage for caching)
# This stage is cached by GitHub Actions, reducing download frequency.
# Cache persists across builds until expired or weights version changes.
# ============================================================================
FROM alpine:latest AS weights-downloader

# Version arguments for weights management
# Change these to download different versions or from different sources
ARG WEIGHTS_VERSION=v1.0.0
ARG WEIGHTS_URL=https://protenix.tos-cn-beijing.volces.com/checkpoint/protenix_base_default_v1.0.0.pt
ARG WEIGHTS_MODEL_NAME=protenix_base_default_v1.0.0

RUN apk add --no-cache wget ca-certificates
WORKDIR /weights

# Download model weights with version tracking
# This layer is cached and reused across all builds
RUN echo "Downloading Protenix weights version: ${WEIGHTS_VERSION}" && \
    echo "Model: ${WEIGHTS_MODEL_NAME}" && \
    echo "URL: ${WEIGHTS_URL}" && \
    mkdir -p ${WEIGHTS_MODEL_NAME}/ && \
    wget --no-check-certificate -q --show-progress --progress=bar:force \
        -O ${WEIGHTS_MODEL_NAME}/model.pt \
        ${WEIGHTS_URL} && \
    echo "Weights downloaded successfully" && \
    ls -lh ${WEIGHTS_MODEL_NAME}/ && \
    echo "{\"version\": \"${WEIGHTS_VERSION}\", \"model\": \"${WEIGHTS_MODEL_NAME}\", \"url\": \"${WEIGHTS_URL}\", \"download_date\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" \
        > ${WEIGHTS_MODEL_NAME}/metadata.json

# ============================================================================
# STAGE 3: Final Image
# Conditionally includes weights layer based on INCLUDE_WEIGHTS build arg
# ============================================================================
FROM base AS final

# Build arguments
ARG INCLUDE_WEIGHTS=false
ARG WEIGHTS_VERSION=v1.0.0
ARG WEIGHTS_MODEL_NAME=protenix_base_default_v1.0.0

# Create .protenix directory structure
RUN mkdir -p /root/.protenix/weights/

# Conditionally copy weights from downloader stage
COPY --from=weights-downloader /weights/${WEIGHTS_MODEL_NAME} /tmp/weights_temp/

# Install or remove weights based on build argument
RUN if [ "$INCLUDE_WEIGHTS" = "true" ]; then \
        mv /tmp/weights_temp /root/.protenix/weights/${WEIGHTS_MODEL_NAME} && \
        echo "Weights ${WEIGHTS_VERSION} installed at /root/.protenix/weights/" && \
        cat /root/.protenix/weights/${WEIGHTS_MODEL_NAME}/metadata.json && \
        ls -lh /root/.protenix/weights/${WEIGHTS_MODEL_NAME}/; \
    else \
        rm -rf /tmp/weights_temp && \
        echo "No weights included - will download at runtime"; \
    fi

# Set environment variables for weight tracking
ENV PROTENIX_WEIGHTS_INCLUDED=${INCLUDE_WEIGHTS}
ENV PROTENIX_WEIGHTS_VERSION=${WEIGHTS_VERSION}
ENV PROTENIX_WEIGHTS_MODEL=${WEIGHTS_MODEL_NAME}

# Labels for image identification
LABEL org.opencontainers.image.weights="${INCLUDE_WEIGHTS}"
LABEL org.opencontainers.image.weights.version="${WEIGHTS_VERSION}"
LABEL org.opencontainers.image.weights.model="${WEIGHTS_MODEL_NAME}"

# Copy Protenix source code
COPY protenix/ /workspace/protenix/
COPY runner/ /workspace/runner/
COPY configs/ /workspace/configs/
COPY tests/ /workspace/tests/
COPY scripts/ /workspace/scripts/
COPY requirements.txt /workspace/
COPY setup.py /workspace/

# Clean up any accidental files that might have been created
RUN rm -f /workspace/'=2.0.0' /workspace/'>=2.0.0' 2>/dev/null || true

# Copy entrypoint script and set permissions
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Set working directory
WORKDIR /workspace

# Set entrypoint for RunPod and other cloud environments
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/bin/bash"]
