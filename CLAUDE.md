# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Fork Development Context

**CRITICAL**: This is a FORK of ByteDance's actively maintained Protenix project. ByteDance processes PRs rapidly (35 closed, 0 open), requiring special development practices:

### Fork Constraints
1. **Daily Upstream Sync**: Always fetch and rebase from upstream/main
2. **48-Hour PR Rule**: Submit PRs within 48 hours to avoid conflicts
3. **Small Changes**: Keep PRs under 500 lines, 10 files maximum
4. **Upstream First**: Prioritize changes that can be contributed back
5. **Isolation**: Keep fork-specific features in `fork_features/` directory

### Our Fork Focus
- **Priority 1**: Fix bugs affecting community (#182, #185, #176, #186)
- **Priority 2**: Improve usability and documentation
- **Priority 3**: Add enterprise features (isolated in fork_features/)
- **Priority 4**: Two-pass prediction system for drug discovery

## Project Overview

Protenix is a trainable, open-source PyTorch reproduction of AlphaFold 3 for high-accuracy biomolecular structure prediction. The project includes both training and inference pipelines for predicting protein structures and biomolecular interactions.

## Development Commands

### Installation
```bash
# Standard installation
pip3 install protenix

# For CPU-only development
python3 setup.py develop --cpu
```

### Running Inference
```bash
# Convert PDB/CIF to JSON
protenix tojson --input <pdb_or_cif_file> --out_dir ./output

# Run MSA search (optional)
protenix msa --input <json_or_fasta_file> --out_dir ./output

# Run prediction
protenix predict --input <json_file> --out_dir ./output --seeds 101 --model_name "protenix_base_default_v0.5.0"

# Or use the demo script
bash inference_demo.sh
```

### Training
```bash
# Train from scratch
bash train_demo.sh

# Fine-tune on specific dataset
bash finetune_demo.sh

# For distributed training (e.g., 4 GPUs)
torchrun --nproc_per_node=4 runner/train.py
```

### Testing
```bash
# Run individual test files
python -m unittest tests.test_utils
python -m unittest tests.test_diffusion_transformer
# Note: The project uses unittest framework for testing
```

### Code Quality
```bash
# Install pre-commit hooks (required before making commits)
pip install pre-commit
pre-commit install

# Run flake8 linting
flake8 .
```

## Code Architecture

### Key Directories
- `protenix/`: Core library code
  - `model/`: Neural network architectures (Pairformer, Diffusion, Confidence modules)
  - `data/`: Data loading, featurization, MSA processing
  - `metrics/`: Evaluation metrics (LDDT, RMSD, clash detection)
  - `openfold_local/`: Adapted OpenFold components
- `runner/`: Training and inference scripts
- `configs/`: Configuration files for models and data
- `scripts/`: Utility scripts for data preparation
- `tests/`: Unit tests using unittest framework

### Important Components
- **Diffusion Module**: Handles structure generation through iterative refinement
- **Pairformer**: Processes pairwise representations for structure prediction  
- **MSA Pipeline**: Processes multiple sequence alignments for evolutionary information
- **Constraint System**: Supports atom-level contact and pocket constraints for guided predictions

### Model Variants
- `protenix_base_default_v0.5.0`: Standard model with MSA
- `protenix_mini_esm_v0.5.0`: Lightweight model using ESM features only
- Mini models use reduced network blocks and fewer ODE steps for efficiency

### Data Flow
1. Input structures (PDB/CIF) → JSON format conversion
2. Optional MSA search for evolutionary features
3. Feature extraction and tokenization
4. Model inference with diffusion process
5. Structure output with confidence metrics

## Environment Variables
- `LAYERNORM_TYPE`: Controls LayerNorm implementation (`fast_layernorm` or `torch`)
- `PROTENIX_DATA_ROOT_DIR`: Override default data directory location

## Fork Development Workflow

### Before Starting Any Work
```bash
# Add upstream remote (first time only)
git remote add upstream https://github.com/bytedance/Protenix

# Daily sync routine
git fetch upstream
git checkout main
git rebase upstream/main
git push origin main --force-with-lease
```

### Creating a PR for Upstream
```bash
# Create feature branch
git checkout -b fix/issue-182-pip-install
# Work quickly (24-48 hours max)
# Make small, focused changes
# Submit PR to upstream immediately
```

### Fork-Specific Features
```bash
# Isolate in fork directory
mkdir -p fork_features/enterprise/
# Develop features that won't be upstreamed
```

## Testing Requirements
- Always run `python -m unittest discover tests/` (upstream tests)
- Add tests in `tests_fork/` for fork features
- Never mix test data with production code
- See [TESTING.md](./TESTING.md) for details

## Key Configuration Points
- Training uses BF16 mixed precision by default
- Default crop size: 384 tokens for training
- EMA decay: 0.999 for model averaging
- Diffusion steps: 200 for training, 20 for evaluation