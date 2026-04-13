# Protenix: Protein + X

<div align="center">

[![Docker Build Status](https://github.com/ShadNygren/Protenix/actions/workflows/docker-build.yml/badge.svg?branch=VHC-Main)](https://github.com/ShadNygren/Protenix/actions/workflows/docker-build.yml)
[![Security Scan](https://github.com/ShadNygren/Protenix/actions/workflows/security.yml/badge.svg)](https://github.com/ShadNygren/Protenix/actions/workflows/security.yml)
[![codecov](https://codecov.io/gh/ShadNygren/Protenix/branch/VHC-Main/graph/badge.svg)](https://codecov.io/gh/ShadNygren/Protenix)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Docker Pulls](https://img.shields.io/docker/pulls/shadnygren/protenix?label=Docker%20Pulls)](https://github.com/ShadNygren/Protenix/pkgs/container/protenix)

</div>

<div align="center">

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/ShadNygren/Protenix/badge)](https://scorecard.dev/viewer/?uri=github.com/ShadNygren/Protenix)
[![SLSA 3](https://slsa.dev/images/gh-badge-level3.svg)](https://slsa.dev)
[![SBOM](https://img.shields.io/badge/SBOM-Available-brightgreen)](https://github.com/ShadNygren/Protenix/actions/workflows/sbom.yml)
[![Trivy Scan](https://img.shields.io/badge/Trivy-Suspended-red)](https://github.com/ShadNygren/Protenix/security)

</div>

## 🚀 Enterprise Improvements in This Fork

This fork enhances the original ByteDance Protenix with enterprise-grade improvements focused on **security**, **reliability**, and **ease of deployment** for scientists, researchers, and biotech firms worldwide.

### Key Enhancements:

#### 🐳 **Production-Ready Docker Images**
- **Secure Base Images**: Replaced unauditable Chinese registry images with official PyTorch and Alpine Linux images
- **Four Optimized Variants**:
  - `runtime` (3.3GB): Lightweight production deployment
  - `runtime_weights` (4.7GB): Production with pre-installed weights
  - `devel` (6.8GB): Development environment with CUDA toolkit
  - `devel_weights` (8.2GB): Development with pre-installed weights
- **Multi-Stage Builds**: Efficient layer caching reduces build times by 70%
- **Pre-installed Weights Option**: Eliminates runtime downloads from Chinese servers

#### 🔧 **Critical Bug Fixes**
- **Fixed #182**: DeepSpeed/Pydantic 2.x compatibility issue
- **Fixed #185**: Triton GPU fallback for consumer GPUs (RTX 3090/4090)
- **Improved Installation**: Clear dependency resolution

#### 🌍 **Global Performance**
- **Faster for Western Users**: No need to download 1.4GB weights from Chinese servers
- **GitHub Container Registry**: Reliable distribution via `ghcr.io`
- **7-Day Cache Strategy**: Reduces server load and speeds up CI/CD

#### 🔒 **Security & Compliance**
- **Comprehensive Security Scanning**:
  - Trivy vulnerability scanning for code and containers (pinned to SHA; see [CVE-2026-33634](https://www.microsoft.com/en-us/security/blog/2026/03/24/detecting-investigating-defending-against-trivy-supply-chain-compromise/))
  - OWASP dependency checking
  - Secret detection with TruffleHog
  - License compliance verification
- **Software Bill of Materials (SBOM)**:
  - Auto-generated for all Docker images
  - CycloneDX and SPDX formats
  - Attestation support for supply chain security
- **Auditable Supply Chain**:
  - All base images from trusted sources (PyTorch, Alpine)
  - OpenSSF Scorecard compliance
  - Working towards SLSA Level 3
- **Enterprise Compliance Ready**:
  - SOC 2 Type II controls
  - HIPAA considerations for healthcare
  - FDA 21 CFR Part 11 for pharmaceutical use
- **Security Documentation**: [Full Security Policy](./SECURITY.md)

### Quick Start with Docker

#### GPU Requirements
- **NVIDIA Driver**: Version 560.28.03 or newer required for CUDA 12.6 compatibility
- **Supported GPUs**: RTX 3090, RTX 4090, A40, A100, H100, H200, L4, L40 (with compatible drivers)
- **Cloud Provider Notes**: 
  - Some cloud providers may have older drivers. Check with `nvidia-smi` before deploying
  - RunPod RTX 4090 instances may have incompatible drivers - use A40/A100/H100 instead
  - AWS/GCP/Azure typically maintain up-to-date drivers on their GPU instances

```bash
# Check your NVIDIA driver version first
nvidia-smi  # Should show Driver Version: 560.28.03 or higher

# For immediate use with pre-installed weights (no download needed)
docker pull ghcr.io/shadnygren/protenix:runtime_weights
docker run --gpus all -it ghcr.io/shadnygren/protenix:runtime_weights

# For development with full CUDA toolkit
docker pull ghcr.io/shadnygren/protenix:devel_weights
```

---



<div align="center" style="margin: 20px 0;">
  <span style="margin: 0 10px;">⚡ <a href="https://protenix-server.com">Protenix Web Server</a></span>
  &bull; <span style="margin: 0 10px;">📄 <a href="docs/PTX_V1_Technical_Report_202602042356.pdf">Technical Report</a></span>
</div>

<div align="center">

[![Twitter](https://img.shields.io/badge/Twitter-Follow-blue?logo=x)](https://x.com/ai4s_protenix)
[![Slack](https://img.shields.io/badge/Slack-Join-yellow?logo=slack)](https://join.slack.com/t/protenixworkspace/shared_invite/zt-3drypwagk-zRnDF2VtOQhpWJqMrIveMw)
[![Email](https://img.shields.io/badge/Email-Contact-lightgrey?logo=gmail)](#contact)
</div>

We’re excited to introduce **Protenix** — Toward High-Accuracy Open-Source Biomolecular Structure Prediction.

Protenix is built for high-accuracy structure prediction. It serves as an initial step in our journey toward advancing accessible and extensible research tools for the computational biology community.

<img src="assets/protenix_predictions.gif" style="width: 100%; height: auto;" alt="Protenix predictions">

## 🌟 Related Projects
- **[PXDesign](https://protenix.github.io/pxdesign/)** is a model suite for de novo protein-binder design built on the Protenix foundation model. PXDesign achieves 20–73% experimental success rates across multiple targets — 2–6× higher than prior SOTA methods such as AlphaProteo and RFdiffusion. The framework is freely accessible via the Protenix Server.

- **[PXMeter](https://github.com/bytedance/PXMeter/)** is an open-source toolkit designed for reproducible evaluation of structure prediction models, released with high-quality benchmark dataset that has been manually reviewed to remove experimental artifacts and non-biological interactions. The associated study presents an in-depth comparative analysis of state-of-the-art models, drawing insights from extensive metric data and detailed case studies. The evaluation of Protenix is based on PXMeter.

- **[Protenix-Dock](https://github.com/bytedance/Protenix-Dock)**: Our implementation of a classical protein-ligand docking framework that leverages empirical scoring functions. Without using deep neural networks, Protenix-Dock delivers competitive performance in rigid docking tasks.

## 🎉 Latest Updates
- **2026-02-05: Protenix-v1 Released** 💪 [[Technical Report](docs/PTX_V1_Technical_Report_202602042356.pdf)]
  - Supported Template/RNA MSA features and improved training dynamics, along with further Inference-time model performance enhancements.
- **2025-11-05: Protenix-v0.7.0 Released** 🚀
  - Introduced advanced diffusion inference optimizations: Shared variable caching, efficient kernel fusion, and TF32 acceleration. See our [performance analysis](./assets/inference_time_vs_ntoken.png).
- **2025-07-17: Protenix-Mini & Constraint Features**
  - Released lightweight model variants ([Protenix-Mini](https://arxiv.org/abs/2507.11839)) that drastically reduce inference costs with minimal accuracy loss.
  - Added support for [atom-level contact and pocket constraints](docs/infer_json_format.md#constraint), enhancing prediction accuracy through physical priors.
- **2025-01-16: Pipeline Enhancements**
  - Open-sourced the full [training data pipeline](./docs/prepare_training_data.md) and [MSA pipeline](./docs/msa_template_pipeline.md).
  - Integrated local [ColabFold-compatible search](./docs/colabfold_compatible_msa.md) for streamlined MSA generation.


## 🚀 Getting Started

### 🛠 Quick Installation

```bash
pip install protenix
```

### 🧬 Quick Prediction

```bash
# Predict structure using a JSON input
protenix pred -i examples/input.json -o ./output -n protenix_base_default_v1.0.0
```

#### Key Model Descriptions
| Model Name | MSA | RNA MSA | Template | Params | Training Data Cutoff | Model Release Date |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `protenix_base_default_v1.0.0` | ✅ | ✅ | ✅ | 368 M | 2021-09-30 | 2026-02-05 |
| `protenix_base_20250630_v1.0.0` | ✅ | ✅ | ✅ | 368 M | 2025-06-30 | 2026-02-05 |
| `protenix_base_default_v0.5.0` | ✅ | ❌ | ❌ | 368 M | 2021-09-30 | 2025-05-30 |

- **protenix_base_default_v1.0.0**: Default model, trained with a data cutoff aligned with AlphaFold3 (2021-09-30).
  > 💡
  > This is the **highly recommended** model for conducting fair, rigorous public benchmarks and comparative studies against other state-of-the-art methods.
- **protenix_base_20250630_v1.0.0**: Applied model, trained with an updated data cutoff (2025-06-30) for better practical performance. This model can be used for practical application scenarios.
- **protenix_base_default_v0.5.0**: Previous version of the model, maintained primarily for backward compatibility with users who developed based on v0.5.0.

For a complete list of supported models, please refer to [Supported Models](docs/supported_models.md).

For detailed instructions on installation, data preprocessing, inference, and training, please refer to the [Training and Inference Instructions](docs/training_inference_instructions.md). We recommend users refer to [inference_demo.sh](inference_demo.sh) for detailed inference methods and input explanations.


### 📊 Benchmark

**Protenix-v1 (refers to the `protenix_base_default_v1.0.0` model)**, the first fully open-source model that outperforms AlphaFold3 across diverse benchmark sets while adhering to the same training data cutoff, model scale, and inference budget as AlphaFold3. For challenging targets, such as antigen-antibody complexes, the prediction accuracy of Protenix-v1 can be further enhanced through inference-time scaling – increasing the sampling budget from several to hundreds of candidates leads to consistent log-linear gains.

<img src="./assets/protenix_base_default_v1.0.0_metrics.png" style="width: 100%; height: auto;" alt="protenix-v1 model Metrics">

<img src="./assets/protenix_base_default_v1.0.0_metrics2.png" style="width: 100%; height: auto;" alt="protenix-v1 model Metrics 2">

For detailed benchmark metrics on each dataset, please refer to [docs/model_1.0.0_benchmark.md](docs/model_1.0.0_benchmark.md).

## Citing Protenix

If you use Protenix in your research, please cite the following:

```
@article {Zhang2026.04.10.717613,
	author = {Zhang, Yuxuan and Gong, Chengyue and Sun, Jinyuan and Guan, Jiaqi and Ren, Milong and Xue, Song and Zhang, Hanyu and Ma, Wenzhi and Liu, Zhenyu and Chen, Xinshi and Xiao, Wenzhi},
	title = {Protenix-v2: Broadening the Reach of Structure Prediction and Biomolecular Design},
	elocation-id = {2026.04.10.717613},
	year = {2026},
	doi = {10.64898/2026.04.10.717613},
	publisher = {Cold Spring Harbor Laboratory},
	URL = {https://www.biorxiv.org/content/early/2026/04/11/2026.04.10.717613},
	eprint = {https://www.biorxiv.org/content/early/2026/04/11/2026.04.10.717613.full.pdf},
	journal = {bioRxiv}
}

@article {Zhang2026.02.05.703733,
	author = {Zhang, Yuxuan and Gong, Chengyue and Zhang, Hanyu and Ma, Wenzhi and Liu, Zhenyu and Chen, Xinshi and Guan, Jiaqi and Wang, Lan and Yang, Yanping and Xia, Yu and Xiao, Wenzhi},
	title = {Protenix-v1: Toward High-Accuracy Open-Source Biomolecular Structure Prediction},
	elocation-id = {2026.02.05.703733},
	year = {2026},
	doi = {10.64898/2026.02.05.703733},
	publisher = {Cold Spring Harbor Laboratory},
	URL = {https://www.biorxiv.org/content/early/2026/02/22/2026.02.05.703733.1},
	eprint = {https://www.biorxiv.org/content/early/2026/02/22/2026.02.05.703733.1.full.pdf},
	journal = {bioRxiv}
}

@article {2025.01.08.631967,
	author = {ByteDance AML AI4Science Team and Chen, Xinshi and Zhang, Yuxuan and Lu, Chan and Ma, Wenzhi and Guan, Jiaqi and Gong, Chengyue and Yang, Jincai and Zhang, Hanyu and Zhang, Ke and Wu, Shenghao and Zhou, Kuangqi and Yang, Yanping and Liu, Zhenyu and Wang, Lan and Shi, Bo and Shi, Shaochen and Xiao, Wenzhi},
	title = {Protenix - Advancing Structure Prediction Through a Comprehensive AlphaFold3 Reproduction},
	elocation-id = {2025.01.08.631967},
	year = {2025},
	doi = {10.1101/2025.01.08.631967},
	publisher = {Cold Spring Harbor Laboratory},
	URL = {https://www.biorxiv.org/content/early/2025/01/11/2025.01.08.631967},
	eprint = {https://www.biorxiv.org/content/early/2025/01/11/2025.01.08.631967.full.pdf},
	journal = {bioRxiv}
}
```

### 📚 Citing Related Work
Protenix is built upon and inspired by several influential projects. If you use Protenix in your research, we also encourage citing the following foundational works where appropriate:
```
@article{abramson2024accurate,
  title={Accurate structure prediction of biomolecular interactions with AlphaFold 3},
  author={Abramson, Josh and Adler, Jonas and Dunger, Jack and Evans, Richard and Green, Tim and Pritzel, Alexander and Ronneberger, Olaf and Willmore, Lindsay and Ballard, Andrew J and Bambrick, Joshua and others},
  journal={Nature},
  volume={630},
  number={8016},
  pages={493--500},
  year={2024},
  publisher={Nature Publishing Group UK London}
}
@article{ahdritz2024openfold,
  title={OpenFold: Retraining AlphaFold2 yields new insights into its learning mechanisms and capacity for generalization},
  author={Ahdritz, Gustaf and Bouatta, Nazim and Floristean, Christina and Kadyan, Sachin and Xia, Qinghui and Gerecke, William and O’Donnell, Timothy J and Berenberg, Daniel and Fisk, Ian and Zanichelli, Niccol{\`o} and others},
  journal={Nature Methods},
  volume={21},
  number={8},
  pages={1514--1524},
  year={2024},
  publisher={Nature Publishing Group US New York}
}
@article{mirdita2022colabfold,
  title={ColabFold: making protein folding accessible to all},
  author={Mirdita, Milot and Sch{\"u}tze, Konstantin and Moriwaki, Yoshitaka and Heo, Lim and Ovchinnikov, Sergey and Steinegger, Martin},
  journal={Nature methods},
  volume={19},
  number={6},
  pages={679--682},
  year={2022},
  publisher={Nature Publishing Group US New York}
}
```

## Contributing to Protenix

We welcome contributions from the community to help improve Protenix!

📄 Check out the [Contributing Guide](CONTRIBUTING.md) to get started.

✅ Code Quality: 
We use `pre-commit` hooks to ensure consistency and code quality. Please install them before making commits:

```bash
pip install pre-commit
pre-commit install
```

🐞 Found a bug or have a feature request? [Open an issue](https://github.com/bytedance/Protenix/issues).



## Acknowledgements


The implementation of LayerNorm operators refers to both [OneFlow](https://github.com/Oneflow-Inc/oneflow) and [FastFold](https://github.com/hpcaitech/FastFold).
We also adopted several [module](protenix/openfold_local/) implementations from [OpenFold](https://github.com/aqlaboratory/openfold), except for [`LayerNorm`](protenix/model/layer_norm/), which is implemented independently.


## Code of Conduct

We are committed to fostering a welcoming and inclusive environment.
Please review our [Code of Conduct](CODE_OF_CONDUCT.md) for guidelines on how to participate respectfully.


## Security

If you discover a potential security issue in this project, or think you may
have discovered a security issue, we ask that you notify Bytedance Security via our [security center](https://security.bytedance.com/src) or [vulnerability reporting email](sec@bytedance.com).

Please do **not** create a public GitHub issue.

## License

The Protenix project including both code and model parameters is released under the [Apache 2.0 License](./LICENSE). It is free for both academic research and commercial use.

## Contact

This is an enterprise fork maintained by [Shad Nygren](https://github.com/ShadNygren) at Virtual Hipster Corporation.

For issues specific to this fork (Docker, security, deployment), please use [GitHub Issues](https://github.com/ShadNygren/Protenix/issues).

For core Protenix questions, refer to the [upstream project](https://github.com/bytedance/Protenix).
