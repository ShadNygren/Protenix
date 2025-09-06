# Build argument to select base image variant
# Options:
#   - runtime (default): 3.3GB base, for production deployments
#   - devel: 6.8GB base, includes CUDA toolkit, compilers, debuggers for development
# Usage: docker build --build-arg BASE_IMAGE_VARIANT=devel .
ARG BASE_IMAGE_VARIANT=runtime

# Select the appropriate base image based on build argument
FROM pytorch/pytorch:2.7.1-cuda12.6-cudnn9-${BASE_IMAGE_VARIANT} AS base

# Label the image with the variant used
LABEL org.opencontainers.image.description="Protenix with PyTorch ${BASE_IMAGE_VARIANT} base image"
LABEL org.opencontainers.image.variant="${BASE_IMAGE_VARIANT}"

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        g++ \
        gcc \
        git \
        libc6-dev \
        make \
        postgresql \
        wget \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# PyTorch is pre-installed in the official base image
# Verify versions and install torchvision/torchaudio if needed
RUN pip3 install --no-cache-dir \
    torchvision==0.22.1 \
    torchaudio==2.7.1

RUN pip3 install --no-cache-dir \
    cuequivariance-ops-torch-cu12==0.6.0 \
    cuequivariance-torch==0.6.0

RUN pip3 --no-cache-dir install \
    scipy==1.16.1 \
    ml_collections==1.1.0 \
    tqdm==4.67.1 \
    pandas==2.3.1 \
    dm-tree==0.1.9 \
    PyYAML==6.0.2 \
    matplotlib==3.10.5 \
    ipywidgets==8.1.7 \
    py3Dmol==2.5.2 \
    rdkit==2023.9.6 \
    biopython==1.85 \
    biotite==1.4.0 \
    modelcif==1.4 \
    gemmi==0.6.7 \
    pdbeccdutils==0.8.6 \
    fair-esm==2.0.0 \
    scikit-learn==1.7.1 \
    scikit-learn-extra==0.3.0 \
    deepspeed==0.17.5 \
    triton==3.3.1 \
    optree==0.17.0 \
    protobuf==6.31.1 \
    icecream==2.1.7 \
    ipdb==0.13.13 \
    wandb==0.21.1 \
    posix_ipc==1.3.0 \
    numpy==1.26.4 \
    pydantic>=2.0.0

RUN git clone -b v3.5.1 https://github.com/NVIDIA/cutlass.git /opt/cutlass
ENV CUTLASS_PATH=/opt/cutlass

# ============================================================================
# STAGE 1: Base Protenix image (runtime or devel)
# This is the complete Protenix installation without weights
# ============================================================================
FROM base AS protenix-base
LABEL org.opencontainers.image.description="Protenix base image without weights"

# ============================================================================
# STAGE 2: Weights Downloader (separate stage for caching)
# This stage is cached by GitHub Actions for 7 days, reducing download frequency
# Cache persists across builds until expired or weights version changes
# ============================================================================
FROM alpine:latest AS weights-downloader

# Version arguments for weights management
# Change these to download different versions or from different sources
ARG WEIGHTS_VERSION=v0.5.0
ARG WEIGHTS_URL=https://af3-dev.tos-cn-beijing.volces.com/release_model/model_v0.5.0.pt
ARG WEIGHTS_MODEL_NAME=protenix_base_default_v0.5.0

RUN apk add --no-cache wget ca-certificates
WORKDIR /weights

# Download model weights with version tracking
# This layer is cached and reused across all builds for 7 days
RUN echo "Downloading Protenix weights version: ${WEIGHTS_VERSION}" && \
    echo "Model: ${WEIGHTS_MODEL_NAME}" && \
    echo "URL: ${WEIGHTS_URL}" && \
    mkdir -p ${WEIGHTS_MODEL_NAME}/ && \
    wget --no-check-certificate -q --show-progress --progress=bar:force \
        -O ${WEIGHTS_MODEL_NAME}/model.pt \
        ${WEIGHTS_URL} && \
    echo "Weights downloaded successfully" && \
    ls -lh ${WEIGHTS_MODEL_NAME}/ && \
    # Create version metadata file
    echo "{\"version\": \"${WEIGHTS_VERSION}\", \"model\": \"${WEIGHTS_MODEL_NAME}\", \"url\": \"${WEIGHTS_URL}\", \"download_date\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}" \
        > ${WEIGHTS_MODEL_NAME}/metadata.json

# ============================================================================
# STAGE 3: Final Image Selection
# Conditionally includes weights layer based on INCLUDE_WEIGHTS build arg
# ============================================================================
FROM protenix-base AS final

# Build arguments
ARG INCLUDE_WEIGHTS=false
ARG WEIGHTS_VERSION=v0.5.0
ARG WEIGHTS_MODEL_NAME=protenix_base_default_v0.5.0

# Create .protenix directory structure
RUN mkdir -p /root/.protenix/weights/

# Conditionally copy weights from downloader stage
# This creates a separate layer that adds ~1.4GB only when INCLUDE_WEIGHTS=true
# The COPY --from is efficient and uses cached layers
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

# Set environment variables and labels for weight tracking
ENV PROTENIX_WEIGHTS_INCLUDED=${INCLUDE_WEIGHTS}
ENV PROTENIX_WEIGHTS_VERSION=${WEIGHTS_VERSION}
ENV PROTENIX_WEIGHTS_MODEL=${WEIGHTS_MODEL_NAME}

# Labels for image identification
LABEL org.opencontainers.image.weights="${INCLUDE_WEIGHTS}"
LABEL org.opencontainers.image.weights.version="${WEIGHTS_VERSION}"
LABEL org.opencontainers.image.weights.model="${WEIGHTS_MODEL_NAME}"

# Note on caching:
# - GitHub Actions caches layers for 7 days of inactivity
# - Cache survives weekends and even 3-day weekends
# - After 7 days of no builds, weights will re-download
# - To update weights: change WEIGHTS_VERSION, WEIGHTS_URL, and WEIGHTS_MODEL_NAME
# - Each weights version gets its own cached layer