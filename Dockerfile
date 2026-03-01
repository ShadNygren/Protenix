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
# PATH includes conda bin and CUDA so python3/nvcc/protenix work everywhere
ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CUTLASS_PATH=/opt/cutlass \
    PATH=/opt/conda/bin:/usr/local/cuda/bin:${PATH}

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
        openssh-client \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Configure SSH for RunPod (keys are injected at runtime by RunPod or docker-entrypoint.sh)
RUN mkdir -p /var/run/sshd /root/.ssh && \
    chmod 700 /root/.ssh && \
    echo "PermitRootLogin yes" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication no" >> /etc/ssh/sshd_config && \
    echo "PubkeyAuthentication yes" >> /etc/ssh/sshd_config && \
    echo "AuthorizedKeysFile /root/.ssh/authorized_keys" >> /etc/ssh/sshd_config && \
    echo 'PATH="/opt/conda/bin:/usr/local/cuda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"' > /etc/environment && \
    printf '#!/bin/sh\nexport PATH="/opt/conda/bin:/usr/local/cuda/bin:$PATH"\nexport PROTENIX_ROOT_DIR=/root\n' > /etc/profile.d/protenix.sh && \
    chmod +x /etc/profile.d/protenix.sh && \
    echo 'export PATH=/opt/conda/bin:/usr/local/cuda/bin:$PATH' >> /root/.bashrc && \
    echo 'export PROTENIX_ROOT_DIR=/root' >> /root/.bashrc && \
    echo 'cd /workspace' >> /root/.bashrc

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
# Default: protenix_base_20250630_v1.0.0 (practical model, 2025-06-30 data cutoff)
# Override at build time for other models, e.g.:
#   --build-arg WEIGHTS_MODEL_NAME=protenix_base_default_v1.0.0
#   --build-arg WEIGHTS_URL=https://protenix.tos-cn-beijing.volces.com/checkpoint/protenix_base_default_v1.0.0.pt
ARG WEIGHTS_VERSION=v1.0.0
ARG WEIGHTS_URL=https://protenix.tos-cn-beijing.volces.com/checkpoint/protenix_base_20250630_v1.0.0.pt
ARG WEIGHTS_MODEL_NAME=protenix_base_20250630_v1.0.0

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
ARG WEIGHTS_MODEL_NAME=protenix_base_20250630_v1.0.0

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

# Create checkpoint symlink so inference finds weights at default path
# Protenix looks for /root/checkpoint/{model_name}.pt (a file, not a directory)
RUN mkdir -p /root/checkpoint && \
    if [ -f "/root/.protenix/weights/${WEIGHTS_MODEL_NAME}/model.pt" ]; then \
        ln -sf /root/.protenix/weights/${WEIGHTS_MODEL_NAME}/model.pt /root/checkpoint/${WEIGHTS_MODEL_NAME}.pt; \
    fi

# Set environment variables for weight tracking
ENV PROTENIX_ROOT_DIR=/root
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
COPY README.md /workspace/

# Clean up any accidental files that might have been created
RUN rm -f /workspace/'=2.0.0' /workspace/'>=2.0.0' 2>/dev/null || true

# Install Protenix as editable package so it's ready to use on launch
RUN cd /workspace && pip install --no-cache-dir -e .

# Pre-compile CUDA kernel (fast_layer_norm_cuda_v2) to avoid ~10min JIT
# compilation on first inference. Only works on devel images (need nvcc).
# On runtime images, the compilation is skipped and will happen at runtime
# if nvcc is somehow available, or fail gracefully.
ARG BASE_IMAGE_VARIANT
RUN if [ "${BASE_IMAGE_VARIANT}" = "devel" ] && command -v nvcc >/dev/null 2>&1; then \
        echo "Pre-compiling CUDA kernels (devel image with nvcc)..." && \
        cd /workspace && python3 -c "from protenix.model.layer_norm import FusedLayerNorm; print('CUDA kernel compiled successfully')" && \
        ls -lh /workspace/protenix/model/layer_norm/fast_layer_norm_cuda_v2.so; \
    else \
        echo "Skipping CUDA kernel pre-compilation (runtime image, no nvcc)"; \
    fi

# Pre-download all Protenix data dependencies (~625MB total) to avoid
# slow downloads from Chinese CDN on first inference. SHA256 verified.
RUN mkdir -p /root/common && \
    CDN="https://protenix.tos-cn-beijing.volces.com/common" && \
    echo "Downloading components.cif (469MB)..." && \
    wget --no-check-certificate -q -O /root/common/components.cif ${CDN}/components.cif && \
    echo "bb31ae5cf6c8bc669924313077cb4231ee5ffefd3a20118cd14f3ec89f8bb6a5  /root/common/components.cif" | sha256sum -c - && \
    echo "Downloading components.cif.rdkit_mol.pkl (136MB)..." && \
    wget --no-check-certificate -q -O /root/common/components.cif.rdkit_mol.pkl ${CDN}/components.cif.rdkit_mol.pkl && \
    echo "d1cfb71f5993a3ebea7c47877022d7f597bbfbaf86e28a4770e957da6c50cd35  /root/common/components.cif.rdkit_mol.pkl" | sha256sum -c - && \
    echo "Downloading clusters-by-entity-40.txt (21MB)..." && \
    wget --no-check-certificate -q -O /root/common/clusters-by-entity-40.txt ${CDN}/clusters-by-entity-40.txt && \
    echo "1ab4af905e75b382eda8dec59917dc3608bee0729e36b9e71baf860bbe86850c  /root/common/clusters-by-entity-40.txt" | sha256sum -c - && \
    echo "Downloading obsolete_release_date.csv (132KB)..." && \
    wget --no-check-certificate -q -O /root/common/obsolete_release_date.csv ${CDN}/obsolete_release_date.csv && \
    echo "a4f3f63ac5d7eebd78b07995cc669b9eccd6f5d8813c9492c9df02868893cf33  /root/common/obsolete_release_date.csv" | sha256sum -c - && \
    echo "All data dependencies downloaded and verified:" && \
    ls -lh /root/common/

# Copy entrypoint script and set permissions
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Set working directory
WORKDIR /workspace

# Set entrypoint for RunPod and other cloud environments
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/bin/bash"]
