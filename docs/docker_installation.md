### Run with Docker

## GPU Requirements

⚠️ **Important**: Our Docker images require **NVIDIA Driver 560.28.03 or newer** for CUDA 12.6 compatibility.

**Check your driver version:**
```bash
nvidia-smi  # Should show Driver Version: 560.28.03 or higher
```

**Supported GPUs** (with compatible drivers):
- Consumer: RTX 3090, RTX 4090
- Data Center: A40, A100, H100, H200, L4, L40

**Cloud Provider Notes:**
- ✅ AWS/GCP/Azure: Generally have up-to-date drivers
- ⚠️ RunPod RTX 4090: May have older drivers - use A40/A100/H100 instances instead
- ⚠️ Smaller cloud providers: Verify driver version before deployment

## Installation Steps

1. **Install Docker with GPU Support**

    Install required components:
    * Install [Docker](https://www.docker.com/) if not already installed
    * Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
    * Verify GPU support:
        ```bash
        docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu22.04 nvidia-smi
        ```

2. **Choose and Pull a Docker Image**

    We provide four optimized variants:
    
    | Variant | Size | Use Case |
    |---------|------|----------|
    | `runtime` | 3.3GB | Production deployment |
    | `runtime_weights` | 4.7GB | Production with pre-installed weights |
    | `devel` | 6.8GB | Development with CUDA toolkit |
    | `devel_weights` | 8.2GB | Development with pre-installed weights |

    ```bash
    # For production with pre-installed weights (recommended)
    docker pull ghcr.io/shadnygren/protenix:runtime_weights
    
    # For development
    docker pull ghcr.io/shadnygren/protenix:devel_weights
    ```

3. **Clone Repository** (optional for local development)
    ```bash
    git clone https://github.com/ShadNygren/Protenix.git
    cd Protenix
    ```

4. **Run Docker Container**
    ```bash
    # Production use (with pre-installed weights)
    docker run --gpus all -it ghcr.io/shadnygren/protenix:runtime_weights
    
    # Development with local code mounting
    docker run --gpus all -it \
        -v $(pwd):/workspace \
        -v /dev/shm:/dev/shm \
        ghcr.io/shadnygren/protenix:devel_weights \
        /bin/bash
    ```

After running these commands, you'll be inside the container's environment with Protenix ready to use.

## Troubleshooting

**CUDA Version Error:**
```
nvidia-container-cli: requirement error: unsatisfied condition: cuda>=12.6
```
**Solution**: Update your NVIDIA driver to 560.28.03 or newer, or use a cloud instance with compatible drivers.

**Out of Memory:**
- Ensure `/dev/shm` is mounted with sufficient space
- Use `--shm-size=8g` flag if needed