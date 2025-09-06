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
# Base images (without weights - download at runtime)
docker pull ghcr.io/shadnygren/protenix:runtime       # 3.3GB - for production
docker pull ghcr.io/shadnygren/protenix:devel         # 6.8GB - includes CUDA toolkit
docker pull ghcr.io/shadnygren/protenix:latest        # Same as :runtime

# Images with pre-installed weights (ready to use, no download needed)
docker pull ghcr.io/shadnygren/protenix:runtime_weights      # ~4.7GB (3.3GB + 1.4GB weights)
docker pull ghcr.io/shadnygren/protenix:runtime_plus_weights # Alternative tag for clarity
docker pull ghcr.io/shadnygren/protenix:devel_weights        # ~8.2GB (6.8GB + 1.4GB weights)  
docker pull ghcr.io/shadnygren/protenix:devel_plus_weights   # Alternative tag for clarity

# Testing and release variants
docker pull ghcr.io/shadnygren/protenix:testing-runtime
docker pull ghcr.io/shadnygren/protenix:testing-runtime_weights
docker pull ghcr.io/shadnygren/protenix:testing-devel
docker pull ghcr.io/shadnygren/protenix:testing-devel_weights
docker pull ghcr.io/shadnygren/protenix:release-runtime
docker pull ghcr.io/shadnygren/protenix:release-runtime_weights
docker pull ghcr.io/shadnygren/protenix:release-devel
docker pull ghcr.io/shadnygren/protenix:release-devel_weights
```

### Branch Strategy

- `merged-updates` → Integration branch (no automatic Docker builds)
- `docker-pytorch` → Default branch, builds both runtime and devel images
- `testing` → Testing candidate (builds both variants)
- `release` → Production ready (builds both variants)

## Docker Layer Architecture

### Multi-Stage Build Strategy
The Dockerfile uses a multi-stage build approach for efficient layer caching:

1. **Base Stage**: PyTorch runtime or devel base image
2. **Protenix Base**: Complete Protenix installation without weights
3. **Weights Downloader**: Separate Alpine-based stage that downloads weights (cached for 7 days)
4. **Final Stage**: Conditionally includes weights layer based on build argument

### Caching Benefits
- **Weights are downloaded once** and cached by GitHub Actions for 7 days
- **All 4 variants share** the same cached weights layer
- **Survives weekends** and even 3-day weekends (7-day retention)
- **Reduces load** on ByteDance's Chinese servers
- **Faster builds** after initial download

### Weights Versioning
To update to new weights (e.g., v0.6.0):
```yaml
# In .github/workflows/docker-build.yml, update:
WEIGHTS_VERSION=v0.6.0
WEIGHTS_URL=https://af3-dev.tos-cn-beijing.volces.com/release_model/model_v0.6.0.pt
WEIGHTS_MODEL_NAME=protenix_base_default_v0.6.0
```

## Building Locally

### Base Images (without weights)
```bash
# Runtime image (3.3GB)
docker build --build-arg BASE_IMAGE_VARIANT=runtime -t protenix:runtime .

# Development image (6.8GB)
docker build --build-arg BASE_IMAGE_VARIANT=devel -t protenix:devel .
```

### Images with Pre-installed Weights
```bash
# Runtime + weights (~4.7GB total)
docker build \
  --build-arg BASE_IMAGE_VARIANT=runtime \
  --build-arg INCLUDE_WEIGHTS=true \
  -t protenix:runtime_plus_weights .

# Development + weights (~8.2GB total)
docker build \
  --build-arg BASE_IMAGE_VARIANT=devel \
  --build-arg INCLUDE_WEIGHTS=true \
  -t protenix:devel_plus_weights .
```

### Custom Weights Version
```bash
# Build with specific weights version
docker build \
  --build-arg BASE_IMAGE_VARIANT=runtime \
  --build-arg INCLUDE_WEIGHTS=true \
  --build-arg WEIGHTS_VERSION=v0.6.0 \
  --build-arg WEIGHTS_URL=https://your-server.com/model_v0.6.0.pt \
  --build-arg WEIGHTS_MODEL_NAME=protenix_custom_v0.6.0 \
  -t protenix:runtime_custom_weights .
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