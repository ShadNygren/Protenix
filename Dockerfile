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

# Build argument to optionally include pre-downloaded weights
# Options:
#   - false (default): No weights included, download at runtime
#   - true: Download and include weights during build (adds ~1.4GB)
ARG INCLUDE_WEIGHTS=false

# Download and install model weights if INCLUDE_WEIGHTS is true
# This downloads from ByteDance's servers and installs to the expected location
RUN if [ "$INCLUDE_WEIGHTS" = "true" ]; then \
        echo "Downloading Protenix v0.5.0 weights (1.4GB)..." && \
        mkdir -p /root/.protenix/weights/protenix_base_default_v0.5.0/ && \
        wget -q --show-progress --progress=bar:force \
            -O /root/.protenix/weights/protenix_base_default_v0.5.0/model.pt \
            https://af3-dev.tos-cn-beijing.volces.com/release_model/model_v0.5.0.pt && \
        echo "Weights installed successfully" && \
        ls -lh /root/.protenix/weights/protenix_base_default_v0.5.0/; \
    fi

# Set environment variable to indicate weights are pre-installed
ARG WEIGHTS_LABEL=without-weights
ENV PROTENIX_WEIGHTS_INCLUDED=${WEIGHTS_LABEL}
LABEL org.opencontainers.image.weights="${WEIGHTS_LABEL}"