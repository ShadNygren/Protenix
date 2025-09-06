# Docker Configuration for Protenix

## Overview

This fork uses **official PyTorch Docker base images** instead of the original Chinese registry base image for improved security, transparency, and accessibility.

## Base Images

### Current (Recommended)
```dockerfile
FROM pytorch/pytorch:2.7.1-cuda12.6-cudnn9-runtime
```
- **Source**: Official PyTorch/Meta repository
- **Size**: Optimized runtime image (~6GB)
- **Security**: Auditable, regularly updated
- **Access**: Available globally via Docker Hub

### Development Alternative
```dockerfile
FROM pytorch/pytorch:2.7.1-cuda12.6-cudnn9-devel
```
- **Use Case**: When building custom CUDA kernels
- **Size**: Larger (~12GB), includes CUDA development tools
- **Note**: Uncomment in Dockerfile if needed

### Original (Not Recommended)
```dockerfile
FROM vemlp-cn-beijing.cr.volces.com/preset-images/pytorch:2.7.1-cu12.6.3-py3.11-ubuntu22.04
```
- **Issues**: 
  - Unknown contents
  - Chinese registry may be inaccessible
  - Potential security concerns
  - No public audit trail

## GitHub Container Registry

Docker images are automatically built and pushed to GitHub Container Registry (ghcr.io).

### Access Images

Public images will be available at:
```bash
# Runtime images (3.3GB - for production)
docker pull ghcr.io/shadnygren/protenix:runtime
docker pull ghcr.io/shadnygren/protenix:latest  # Same as :runtime

# Development images (6.8GB - includes CUDA toolkit)
docker pull ghcr.io/shadnygren/protenix:devel

# Testing and release variants
docker pull ghcr.io/shadnygren/protenix:testing-runtime
docker pull ghcr.io/shadnygren/protenix:testing-devel
docker pull ghcr.io/shadnygren/protenix:release-runtime
docker pull ghcr.io/shadnygren/protenix:release-devel
```

### Branch Strategy

- `merged-updates` → Integration branch (no automatic Docker builds)
- `docker-pytorch` → Default branch, builds both runtime and devel images
- `testing` → Testing candidate (builds both variants)
- `release` → Production ready (builds both variants)

## Building Locally

### Runtime Image (3.3GB - Recommended for production)
```bash
docker build -t protenix:runtime-local .
# Or explicitly specify the runtime variant
docker build --build-arg BASE_IMAGE_VARIANT=runtime -t protenix:runtime-local .
```

### Development Image (6.8GB - For development/debugging)
```bash
docker build --build-arg BASE_IMAGE_VARIANT=devel -t protenix:devel-local .
```

## Running the Container

### Basic Run
```bash
docker run --gpus all -it protenix:local
```

### With Volume Mounts
```bash
docker run --gpus all -v $(pwd)/data:/data -it protenix:local
```

### Interactive Development
```bash
docker run --gpus all -v $(pwd):/workspace -it protenix:local-dev bash
```

## Key Changes from Upstream

1. **Base Image**: Official PyTorch instead of Chinese registry
2. **DeepSpeed**: Updated to 0.17.5 for Pydantic 2.x compatibility
3. **Pydantic**: Explicitly requires 2.0+ for compatibility
4. **Build Automation**: GitHub Actions for automated builds
5. **Registry**: GitHub Container Registry for transparency

## Security Considerations

- All base images are from official sources
- No unknown or unauditable components
- Regular security updates via official channels
- Transparent build process via GitHub Actions

## Compatibility

These Docker images maintain full compatibility with:
- Consumer GPUs (RTX 3090/4090) via Triton fallback
- Enterprise GPUs (A100/H100) with full Triton support
- CPU-only execution for testing

## Contributing

When modifying the Dockerfile:
1. Always preserve both base image options (comment/uncomment)
2. Test with both runtime and devel variants
3. Document any new dependencies
4. Ensure GitHub Actions workflow remains compatible

---

*Note: This configuration prioritizes security and transparency while maintaining full functionality with the Protenix codebase.*