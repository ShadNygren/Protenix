# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] - VHC-Main

### Added
- Test cases for installation and compatibility issues by Shad Nygren (@ShadNygren)
  - `test_installation.py` - Tests for DeepSpeed/Pydantic compatibility (issue #182)
  - `test_triton_compatibility.py` - Tests for Triton GPU compatibility (issue #185)
  - `test_esm_loading.py` - Tests for ESM weights loading with PyTorch 2.6+ (issue #176)
- CHANGELOG.md to track project changes (Apache 2.0 compliance)
- Multi-stage Docker build with optional pre-installed weights (v1.0.0)
- RunPod cloud compatibility with docker-entrypoint.sh
- OpenSSH server for RunPod SSH key injection
- .dockerignore for clean Docker builds
- Comprehensive security scanning framework (Trivy, SBOM, CodeCov)
- SECURITY.md documentation
- Enterprise README with badges and security highlights

### Fixed
- Issue #182: DeepSpeed and Pydantic v2.x compatibility by upgrading to DeepSpeed 0.17.5
- Issue #185: Triton kernel compatibility with consumer GPUs (RTX 3090/4090)
  - Added PyTorch fallback when Triton is unavailable or unsupported
  - Enables Protenix to run on consumer hardware without code changes
  - Modified `protenix/model/tri_attention/__init__.py` to provide transparent fallback
- Inference robustness:
  - Fixed invalid MSA success assertion in FASTA flow (`runner/batch_inference.py`).
  - Added explicit input JSON validation for non-empty top-level list (`runner/inference.py`).
  - Hardened model-name parsing to avoid crashes on unexpected naming (`runner/inference.py`, `runner/batch_inference.py`).
- CI/CD workflow fixes for coverage, security scanning, and SBOM generation

### Changed
- Docker base image switched from Chinese registry to official PyTorch images
  - Security: Using auditable, official PyTorch base images
  - Accessibility: Available globally via Docker Hub
  - Added GitHub Actions workflow for automated Docker builds to ghcr.io
  - Created DOCKER.md documentation for Docker configuration
- Timezone set to UTC instead of Asia/Shanghai
- Parameterized Dockerfile with BASE_IMAGE_VARIANT build arg (runtime/devel)
- Removed ByteDance hiring/promotional content from README
- Removed WeChat badge, updated contact section for enterprise fork
- Updated weight download references to Protenix v1.0.0

### Upstream (ByteDance Protenix v1.0.0)
- Protenix-v1 model release with Template/RNA MSA features
- Improved training dynamics and inference performance
- Major code restructuring (data/, model/ subpackages)
- Removed openfold_local dependency
- Added template search and RNA MSA search runners
- Updated requirements with newer package versions

### Contributors
- Shad Nygren, Virtual Hipster Corporation (@ShadNygren)

## [v0.6.1] - 2025-08-20

### Fixed
- ESM models loading in PyTorch 2.6 and newer versions (commit dbbee14)

## [v0.6.0] - Previous releases

See commit history for earlier changes.
