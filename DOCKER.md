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

- `VHC-Main` → Fork's main development branch (triggers Docker build and all CI/CD)

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
| `PYTORCH_VERSION` | `2.8.0` | `2.7.1`, `2.8.0`, etc. | PyTorch version (must match a `pytorch/pytorch:` Docker Hub tag) |
| `CUDA_VERSION` | `12.8` | `12.6`, `12.8`, etc. | CUDA version (must match the chosen PyTorch image) |
| `INCLUDE_WEIGHTS` | `false` | `true`, `false` | Pre-install Protenix v1.0.0 model weights |
| `WEIGHTS_VERSION` | `v1.0.0` | Any version | Model weights version |
| `WEIGHTS_MODEL_NAME` | `protenix_base_default_v1.0.0` | Model name | Weights directory name |
| `INCLUDE_TEMPLATE_DB` | `false` | `true`, `false` | Pre-install template search database (~12 GB, for `--use_template true` inference) |

## Cloud-ops + R2/S3 Tools

In addition to PyTorch and Protenix itself, this image ships with everything you need to operate Protenix training on a cloud GPU and stream artifacts to S3-compatible object storage (AWS S3, Cloudflare R2, MinIO, RunPod S3, Backblaze B2, Wasabi, etc.):

| Tool | Purpose |
|---|---|
| AWS CLI v2 | Standard `aws s3 ...` operations against any S3-compatible endpoint |
| `rclone` | High-performance sync, mount, copy with retry/parallelism |
| `s3fs` (FUSE) | Mount an S3-compatible bucket as a regular filesystem |
| `boto3` | Python SDK for scripting cloud operations |
| `fuse3 / libfuse3-3` | Required by s3fs and rclone mount |
| `htop`, `nvtop` | CPU and GPU monitoring during long runs |
| `tmux`, `mosh` | Persistent / robust SSH sessions |
| `jq` | Querying JSON outputs (configs, manifests, eval results) |
| `unzip`, `zip`, `p7zip-full` | Working with block ZIPs in object storage |
| `rsync`, `wget`, `curl` | General data transfer |

Bring your own credentials with the templates at `/etc/cloudflare-r2-template.env` (env-var style) and `/etc/aws-credentials-template` (`~/.aws/credentials` style).

## Generic Protenix Operational Tools

The image ships with utility scripts at `/opt/protenix-tools/` (also on `PATH`):

| Tool | What it does |
|---|---|
| `checkpoint_watcher.py` | Polls `<run>/checkpoints/` and uploads each new `<step>.pt` + `_ema_*.pt` pair to your S3-compatible bucket within ~30 seconds of save, with sha256+md5 in object metadata. Idempotent state file. Designed for interruptible cloud GPUs (Salad Low-priority, AWS spot, GCP preemptible). |
| `find_latest_r2_checkpoint.py` | Disaster recovery: lists `s3://<bucket>/checkpoints/`, returns + downloads the highest-step pair on a fresh pod after eviction. |
| `r2_object_exists.py` | `head_object` check; exits 0 (yes) / 2 (no) / 1 (error). Used by cleanup scripts to verify-before-delete. |
| `vram_monitor.sh` | Persistent 1-second VRAM polling daemon with daily-rotated CSV. Use it to collect peak-VRAM telemetry across long runs (don't claim "fits on GPU X" without ≥100K samples). |
| `inspect_checkpoint_step.py` | Reads the `step` field stored inside a Protenix `.pt` file dict and verifies the filename matches. |
| `extract_training_loss.py` | Parses `training.log` and reports per-step loss / distogram / pae / plddt / lddt. |
| `archive_discarded_artifacts.py` | Archive flawed/abandoned experimental artifacts to a separate S3 prefix with sha256+md5 metadata + `WHY_ARCHIVED.md`. Encodes the principle that experimental data is never deleted, only moved to an archive prefix. |

All scripts use `argparse`, are S3-provider-agnostic (point them at any endpoint), and are committed in `scripts/protenix-tools/`.

## Key Changes from Upstream

1. **Base Image**: Official PyTorch instead of Chinese registry
2. **CUDA 12.8 default**: For Blackwell GPU support (RTX 5090, RTX PRO Blackwell line, B300). Build with `--build-arg PYTORCH_VERSION=2.7.1 --build-arg CUDA_VERSION=12.6` if you only need pre-Blackwell.
3. **Timezone**: UTC instead of Asia/Shanghai
4. **Multi-Stage Build**: Efficient layer caching with separate weights stage
5. **RunPod Support**: docker-entrypoint.sh with cloud environment detection
6. **SSH Server**: Pre-configured for RunPod key injection
7. **R2/S3 cloud-ops tools** (new 2026-05): AWS CLI v2, rclone, s3fs (FUSE), htop, nvtop, tmux, mosh, jq
8. **Generic Protenix utility scripts** (new 2026-05) at `/opt/protenix-tools/` for checkpoint streaming, disaster recovery, VRAM monitoring
9. **Placeholder data files**: empty placeholders prevent training-init `FileNotFoundError` when MSA / RNA / eval features are disabled
10. **Build Automation**: GitHub Actions builds all variants
11. **Registry**: GitHub Container Registry for transparency

## Security Considerations

- All base images are from official sources (PyTorch, Alpine)
- No unknown or unauditable components
- Regular security updates via official channels
- Transparent build process via GitHub Actions
- Trivy vulnerability scanning on every build
- SBOM generation for supply chain transparency
- Credential templates ship with placeholder values only — no real credentials baked into the image
- Storage URI scheme: see notes in `config/cloudflare-r2-template.env` for why we use `CLOUDFLARE_R2_*` prefixed env vars instead of `AWS_*` (avoids collision with real AWS credentials)

## Compatibility

This image targets a broad GPU matrix. The default base (PyTorch 2.8.0 + CUDA 12.8) supports compute capabilities sm_80 through sm_120:

| GPU | Architecture | sm | VRAM | CUDA 12.6 | CUDA 12.8 |
|---|---|---|---|---|---|
| RTX 3090 | Ampere | sm_86 | 24 GB | ✅ | ✅ |
| RTX 4090 | Ada Lovelace | sm_89 | 24 GB | ✅ | ✅ |
| RTX A4000 / A5000 / A6000 | Ampere | sm_86 | 16/24/48 GB | ✅ | ✅ |
| A30 | Ampere | sm_80 | 24 GB | ✅ | ✅ |
| A40 | Ampere | sm_86 | 48 GB | ✅ | ✅ |
| L40 / L40S | Ada Lovelace | sm_89 | 48 GB | ✅ | ✅ |
| A100 80 GB SXM | Ampere | sm_80 | 80 GB | ✅ | ✅ |
| H100 / H200 SXM | Hopper | sm_90 | 80 / 141 GB | ✅ | ✅ |
| **RTX 5090** | **Blackwell 2.0** | **sm_120** | **32 GB** | ❌ | **✅** |
| **RTX PRO 4500 Blackwell** | **Blackwell 2.0** | **sm_120** | **32 GB** | ❌ | **✅** |
| **RTX PRO 6000 Blackwell** | **Blackwell 2.0** | **sm_120** | **96 GB** | ❌ | **✅** |
| **RTX PRO {2000,4000,5000} Blackwell** | **Blackwell** | **sm_120** | varies | ❌ | **✅** |
| **B300 (Blackwell Ultra)** | **Blackwell Ultra** | **sm_100 / sm_120** | **288 GB HBM3e** | ❌ | **✅** |

Cloud platform support: RunPod (production-tested), Salad (Low-priority interruptible — use `checkpoint_watcher.py` daemon for resume), AWS, GCP, Azure. CPU-only execution works for non-training tasks like inference smoke tests.

## GPU Driver Requirements

- For CUDA 12.6 base: NVIDIA driver 560.28.03 or newer
- For CUDA 12.8 base (default): NVIDIA driver 570.x or newer
- See [Docker Installation Guide](docs/docker_installation.md) for details

---

*This configuration prioritizes security, transparency, multi-GPU compatibility (especially Blackwell), and cloud-ops ergonomics while maintaining full functionality with the Protenix codebase.*
