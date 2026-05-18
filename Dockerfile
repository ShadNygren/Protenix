# Build argument to select base image variant
# Options:
#   - runtime (default): smaller base, for production deployments
#   - devel: larger base, includes CUDA toolkit, compilers, debuggers for
#            development AND for pre-compiling fast_layer_norm_cuda_v2 during
#            the build (avoids the ~10min JIT cost on first inference)
# Usage: docker build --build-arg BASE_IMAGE_VARIANT=devel .
ARG BASE_IMAGE_VARIANT=runtime

# Build args for the base image version. Defaults target CUDA 12.8 for
# Blackwell GPU support (RTX 5090 sm_120, RTX PRO 6000 Blackwell sm_120,
# B300 sm_100/sm_120). Older CUDA 12.6 base still usable for non-Blackwell
# GPUs (A100, H100, RTX 4090, etc.) by passing
#   --build-arg PYTORCH_VERSION=2.7.1 --build-arg CUDA_VERSION=12.6
# at build time. Compatible PyTorch images on Docker Hub:
#   pytorch/pytorch:2.7.1-cuda12.6-cudnn9-{runtime,devel}
#   pytorch/pytorch:2.8.0-cuda12.8-cudnn9-{runtime,devel}
ARG PYTORCH_VERSION=2.8.0
ARG CUDA_VERSION=12.8

# ============================================================================
# STAGE 1: Base Protenix image (runtime or devel)
# ============================================================================
FROM pytorch/pytorch:${PYTORCH_VERSION}-cuda${CUDA_VERSION}-cudnn9-${BASE_IMAGE_VARIANT} AS base

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
# Includes:
#  - Build/dev tools: git, gcc/g++, make, libc6-dev
#  - Protenix-specific: postgresql, hmmer, kalign (MSA + database tools)
#  - SSH for cloud pod access (RunPod, Salad, etc.): openssh-{server,client}
#  - Cloud-ops + R2/S3 tools: fuse3, libfuse3-3, s3fs (S3-compat FUSE mount),
#    unzip/zip/p7zip-full (block ZIP extraction), rsync, jq (JSON queries),
#    curl (AWS CLI installer + general)
#  - Operator UX: htop, nvtop (GPU monitor), tmux (persistent sessions),
#    mosh (robust SSH alternative), less, vim-tiny, nano
#  - General: wget, ca-certificates
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
        curl \
        ca-certificates \
        openssh-server \
        openssh-client \
        openssl \
        fuse3 \
        libfuse3-3 \
        s3fs \
        unzip \
        zip \
        p7zip-full \
        rsync \
        jq \
        htop \
        nvtop \
        tmux \
        mosh \
        less \
        vim-tiny \
        nano \
        bc \
        file \
        iproute2 \
        traceroute \
        mtr-tiny \
        iperf3 \
        dnsutils \
        net-tools \
        pciutils \
        speedtest-cli \
        iputils-ping \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install AWS CLI v2 from official installer (apt's awscli is v1 and deprecated)
RUN ARCH=$(uname -m) && \
    case "${ARCH}" in \
        x86_64)  AWS_ARCH=x86_64 ;; \
        aarch64) AWS_ARCH=aarch64 ;; \
        *)       echo "Unsupported architecture: ${ARCH}"; exit 1 ;; \
    esac && \
    curl -sL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/awscliv2.zip /tmp/aws && \
    aws --version

# Install rclone (best-in-class S3-compatible sync + mount tool)
# Used for: rclone copy s3:bucket/key local/, rclone mount, rclone sync
RUN curl -sL https://rclone.org/install.sh | bash 2>&1 && rclone --version | head -1

# Configure SSH for RunPod (keys are injected at runtime by RunPod or docker-entrypoint.sh)
RUN mkdir -p /var/run/sshd /root/.ssh && \
    chmod 700 /root/.ssh && \
    echo "PermitRootLogin yes" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication no" >> /etc/ssh/sshd_config && \
    echo "PubkeyAuthentication yes" >> /etc/ssh/sshd_config && \
    echo "AuthorizedKeysFile /root/.ssh/authorized_keys" >> /etc/ssh/sshd_config && \
    echo "PubkeyAcceptedAlgorithms +ssh-rsa" >> /etc/ssh/sshd_config && \
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

# Operational dependencies for protenix-tools (secure_checkpoint, watchtower, etc.)
# Kept separate from requirements.txt so upstream Protenix updates don't fight us.
# pyrage:       age-encryption (passphrase mode) for checkpoint/structure encryption
# watchtower:   CloudWatch Logs handler for Python logging
# boto3:        AWS SDK (used by both checkpoint_watcher.py and watchtower)
RUN pip3 install --no-cache-dir \
        pyrage==1.2.* \
        watchtower==3.4.* \
        boto3==1.35.*

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

# ============================================================================
# Template Support for Inference
# Enables --use_template true with remote template fetching from PDBe
# Users can also SCP custom template CIF files to /root/mmcif/
# ============================================================================

# Download template search database (~12GB compressed)
# This enables hmmsearch-based template finding during inference
# Comment out this section if you don't need template support (saves ~15GB)
ARG INCLUDE_TEMPLATE_DB=false
RUN if [ "$INCLUDE_TEMPLATE_DB" = "true" ]; then \
        echo "Downloading template search database..." && \
        CDN="https://protenix.tos-cn-beijing.volces.com" && \
        wget --no-check-certificate -q --show-progress -c -P /root ${CDN}/search_database.tar.gz && \
        echo "Extracting search database..." && \
        tar xzf /root/search_database.tar.gz -C /root && \
        rm -f /root/search_database.tar.gz && \
        echo "Search database installed at /root/search_database/" && \
        ls -lh /root/search_database/; \
    else \
        echo "Template search database not included (use --build-arg INCLUDE_TEMPLATE_DB=true to include)"; \
    fi

# Download template metadata files (release dates, obsolete PDB mapping)
# These are small (~1MB) and always useful for template date filtering
RUN CDN="https://protenix.tos-cn-beijing.volces.com/common" && \
    wget --no-check-certificate -q -O /root/common/release_date_cache.json ${CDN}/release_date_cache.json && \
    wget --no-check-certificate -q -O /root/common/obsolete_to_successor.json ${CDN}/obsolete_to_successor.json && \
    echo "Template metadata files downloaded:" && \
    ls -lh /root/common/release_date_cache.json /root/common/obsolete_to_successor.json

# Create template directories for user-provided CIF files
# Users SCP template CIF files to /root/mmcif/ at runtime
# Protenix will also fetch templates from PDBe on demand (fetch_remote=true)
RUN mkdir -p /root/mmcif /root/mmcif_msa_template

# Create seq_to_pdb_index.json (empty — populated at runtime or by MSA pipeline)
RUN echo '{}' > /root/common/seq_to_pdb_index.json

# ============================================================================
# Placeholder files to prevent FileNotFoundError on training init
# Even with their corresponding features disabled (RNA MSA, eval suites),
# Protenix's data loaders try to open these files at startup.
# Background: PROTENIX_DOCKER_MISSING_FILES.md (downstream user project)
# ============================================================================

# RNA MSA placeholder
RUN mkdir -p /root/rna_msa && echo '{}' > /root/rna_msa/rna_sequence_to_pdb_chains.json

# Evaluation dataset directories (empty — users mount real data when needed)
RUN mkdir -p /root/indices /root/posebusters_bioassembly /root/posebusters_mmcif \
             /root/recentPDB_bioassembly

# Empty evaluation index CSVs with correct column headers so pandas doesn't crash
RUN echo '"entity_1_id","chain_1_id","mol_1_type","cluster_1_id","entity_2_id","chain_2_id","mol_2_type","cluster_2_id","cluster_id","pdb_id","assembly_id","release_date","num_tokens","num_prot_chains","resolution","type","mol_type_group","sub_mol_1_type","sub_mol_2_type","eval_type"' \
    > /root/indices/recentPDB_low_homology_maxtoken1536.csv && \
    cp /root/indices/recentPDB_low_homology_maxtoken1536.csv \
       /root/indices/posebusters_indices_mainchain_interface.csv

# Empty PDB list for evaluation
RUN touch /root/indices/recentPDB_low_homology_maxtoken1024_sample384_pdb_id.txt

# ============================================================================
# Generic Protenix operational tools shipped at /opt/protenix-tools/
# Available on PATH so users can call them directly:
#   checkpoint_watcher.py    — daemon: uploads each new <step>.pt + ema_*.pt
#                              pair to S3-compatible storage with sha256+md5
#                              metadata; idempotent state file. Works against
#                              AWS S3, Cloudflare R2, MinIO, RunPod S3, etc.
#                              by setting --env-file with the right endpoint.
#   find_latest_r2_checkpoint.py — disaster-recovery: lists S3 checkpoints/,
#                              returns + downloads highest-step pair on a
#                              fresh pod after an interruption.
#   r2_object_exists.py      — head_object check for R2/S3 (used by cleanup
#                              scripts to verify-before-delete).
#   vram_monitor.sh          — persistent 1-second VRAM polling daemon;
#                              daily-rotated CSV. Use to collect peak-VRAM
#                              telemetry across long training runs.
#   inspect_checkpoint_step.py — reads the `step` field stored inside a
#                              Protenix .pt file dict; verifies filename
#                              matches stored value.
#   extract_training_loss.py — parses Protenix training.log; reports per-step
#                              loss/distogram/pae/plddt/lddt metrics.
#   archive_discarded_artifacts.py — archive flawed/abandoned experimental
#                              artifacts to a separate S3 prefix with
#                              sha256+md5 metadata + WHY_ARCHIVED.md note.
#   stage_training_data.py     — downloads checkpoint + bioassembly ZIP +
#                              indices from R2/S3 to local staging dir.
#                              URI scheme (r2:// vs s3://) routes to the
#                              correct storage backend. Core of the
#                              lightweight "devel" image workflow.
#   auto_stage_and_train.sh    — unified startup: calls stage_training_data.py
#                              then run_salad_training.sh. Set as
#                              PROTENIX_STARTUP_SCRIPT for auto-launch.
#   runpod_runner.sh           — fully autonomous training runner. Launched
#                              by runpod_grabber.py on the laptop; handles
#                              creds, staging, eval placeholders, training,
#                              telemetry upload — zero human interaction.
#   runpod_grabber.py          — laptop-side script: polls RunPod API until
#                              a GPU pod is available, then auto-deploys
#                              creds + runner. Not used inside container.
# ============================================================================
COPY scripts/protenix-tools/ /opt/protenix-tools/
RUN chmod +x /opt/protenix-tools/*.py /opt/protenix-tools/*.sh
ENV PATH=/opt/protenix-tools:${PATH}

# Credential templates for users — copies, not actual credentials
COPY config/cloudflare-r2-template.env /etc/cloudflare-r2-template.env
COPY config/aws-credentials-template /etc/aws-credentials-template

# Copy entrypoint script and set permissions
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Set working directory
WORKDIR /workspace

# Set entrypoint for RunPod, Salad, k8s, ECS and other cloud environments.
# NO CMD: the entrypoint script's universal decision tree handles all cases:
#   - explicit args → exec them
#   - no args + TTY (`docker run -it`) → bash
#   - no args + no TTY (any orchestrator deploy) → sleep infinity
# Adding `CMD ["/bin/bash"]` here BREAKS the orchestrator case because the
# script then receives /bin/bash as $@ and execs it; bash exits in seconds
# without a TTY, triggering an Exited:0 crash loop on Salad/k8s/ECS/etc.
# Don't re-add CMD without first updating the entrypoint to special-case it.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
