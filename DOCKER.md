# Docker Configuration for Protenix

## Overview

This fork uses **official PyTorch Docker base images** instead of the original Chinese registry base image for improved security, transparency, and accessibility. It features a multi-stage build with optional pre-installed weights.

## Architecture

The Dockerfile uses a **3-stage build**:

1. **`base`** - PyTorch runtime or devel image with system deps and Python packages
2. **`weights-downloader`** - Alpine-based stage that downloads Protenix v1.0.0 model weights
3. **`final`** - Conditionally includes weights, copies source code, sets up RunPod entrypoint

## Image Variants

| Variant | Tag | Size | Use Case |
|---------|-----|------|----------|
| Runtime | `runtime` | ~3.3GB | Production deployment |
| Runtime + Weights | `runtime-with-weights` | ~4.7GB | Production, no runtime download |
| Devel | `devel` | ~6.8GB | Development with CUDA toolkit |
| Devel + Weights | `devel-with-weights` | ~8.2GB | Development, no runtime download |

## Base Images

### Runtime (Default)
```dockerfile
FROM pytorch/pytorch:2.7.1-cuda12.6-cudnn9-runtime
```
- **Source**: Official PyTorch/Meta repository
- **Size**: Optimized runtime image (~3.3GB)
- **Security**: Auditable, regularly updated
- **Access**: Available globally via Docker Hub

### Development
```dockerfile
FROM pytorch/pytorch:2.7.1-cuda12.6-cudnn9-devel
```
- **Use Case**: Building custom CUDA kernels, debugging
- **Size**: Larger (~6.8GB), includes CUDA development tools

### Original (Not Recommended)
```dockerfile
FROM vemlp-cn-beijing.cr.volces.com/preset-images/pytorch:2.7.1-cu12.6.3-py3.11-ubuntu22.04
```
- **Issues**: Unknown contents, Chinese registry may be inaccessible, no public audit trail

## GitHub Container Registry

Docker images are automatically built and pushed to GitHub Container Registry (ghcr.io).

### Pull Images

```bash
# Runtime (recommended for most users)
docker pull ghcr.io/shadnygren/protenix:runtime

# Runtime with pre-installed weights (no Chinese CDN download needed)
docker pull ghcr.io/shadnygren/protenix:runtime-with-weights

# Development with CUDA toolkit
docker pull ghcr.io/shadnygren/protenix:devel

# Development with weights
docker pull ghcr.io/shadnygren/protenix:devel-with-weights

# Latest (same as runtime)
docker pull ghcr.io/shadnygren/protenix:latest
```

### Branch Strategy

- `VHC-March2026` → Main development branch (triggers Docker build)
- `release` → Production ready (triggers Docker build)

## Building Locally

### Runtime Image (Recommended)
```bash
docker build -t protenix:runtime .
```

### Development Image
```bash
docker build --build-arg BASE_IMAGE_VARIANT=devel -t protenix:devel .
```

### With Pre-installed Weights
```bash
# Runtime with weights
docker build --build-arg INCLUDE_WEIGHTS=true -t protenix:runtime-weights .

# Devel with weights
docker build --build-arg BASE_IMAGE_VARIANT=devel --build-arg INCLUDE_WEIGHTS=true -t protenix:devel-weights .
```

## Running the Container

### Basic Run
```bash
docker run --gpus all -it protenix:runtime
```

### With Volume Mounts
```bash
docker run --gpus all -v $(pwd)/data:/data -it protenix:runtime
```

### Interactive Development
```bash
docker run --gpus all -v $(pwd):/workspace -it protenix:devel bash
```

### On RunPod
The container automatically detects RunPod environment and:
- Starts SSH daemon for RunPod's key injection
- Keeps container alive with sleep loop
- Displays GPU and PyTorch info on startup

```bash
# RunPod will use the entrypoint automatically
docker run --gpus all ghcr.io/shadnygren/protenix:runtime-with-weights
```

## Build Arguments

| Argument | Default | Options | Description |
|----------|---------|---------|-------------|
| `BASE_IMAGE_VARIANT` | `runtime` | `runtime`, `devel` | PyTorch base image variant |
| `INCLUDE_WEIGHTS` | `false` | `true`, `false` | Pre-install Protenix v1.0.0 model weights |
| `WEIGHTS_VERSION` | `v1.0.0` | Any version | Model weights version |
| `WEIGHTS_MODEL_NAME` | `protenix_base_default_v1.0.0` | Model name | Weights directory name |

## Key Changes from Upstream

1. **Base Image**: Official PyTorch instead of Chinese registry
2. **Timezone**: UTC instead of Asia/Shanghai
3. **Multi-Stage Build**: Efficient layer caching with separate weights stage
4. **RunPod Support**: docker-entrypoint.sh with cloud environment detection
5. **SSH Server**: Pre-configured for RunPod key injection
6. **Build Automation**: GitHub Actions builds all 4 variants
7. **Registry**: GitHub Container Registry for transparency

## Security Considerations

- All base images are from official sources (PyTorch, Alpine)
- No unknown or unauditable components
- Regular security updates via official channels
- Transparent build process via GitHub Actions
- Trivy vulnerability scanning on every build
- SBOM generation for supply chain transparency

## Compatibility

These Docker images maintain full compatibility with:
- Consumer GPUs (RTX 3090/4090) via Triton fallback
- Enterprise GPUs (A40/A100/H100/H200) with full Triton support
- Cloud platforms: RunPod, AWS, GCP, Azure
- CPU-only execution for testing

## GPU Requirements

- **NVIDIA Driver**: 560.28.03 or newer for CUDA 12.6
- See [Docker Installation Guide](docs/docker_installation.md) for details

---

*This configuration prioritizes security and transparency while maintaining full functionality with the Protenix codebase.*
