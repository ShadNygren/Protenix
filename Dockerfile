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
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
# Copy requirements.txt first to leverage Docker cache
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt -i https://pypi.org/simple

# Clone CUTLASS
RUN git clone -b v3.5.1 https://github.com/NVIDIA/cutlass.git /opt/cutlass
