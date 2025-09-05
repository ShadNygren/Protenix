# Protenix Architecture Documentation

## System Overview

Protenix implements a multi-tier architecture for biomolecular structure prediction, based on AlphaFold 3 principles with additional optimizations for production deployment. As a fork of an actively maintained upstream project, the architecture emphasizes modularity and compatibility.

## Core Architecture Components

### 1. Data Pipeline Layer
```
Input → Parser → Featurizer → Tokenizer → Model → Output
```

#### Components:
- **Input Processing**: Handles PDB/CIF/JSON formats with validation
- **MSA Pipeline**: Parallel MSA generation with ColabFold integration
- **Feature Extraction**: ESM embeddings, structural features, constraint processing
- **Tokenization**: Unified token representation for proteins, ligands, nucleic acids

### 2. Model Architecture

#### Primary Networks:
- **Pairformer Stack**: Processes pairwise representations with attention mechanisms
- **Diffusion Module**: Iterative structure refinement (200 steps training, 20 steps inference)
- **Confidence Head**: Generates pLDDT, pAE, and ranking scores
- **Template Embedder**: Optional template structure incorporation

#### Model Variants:
```
┌─────────────────────────────────────┐
│         Protenix-Base              │
│    (Full accuracy, MSA-enabled)    │
└─────────────────────────────────────┘
                 ↓
┌─────────────────────────────────────┐
│         Protenix-Mini              │
│  (Reduced blocks, 1-2 ODE steps)   │
└─────────────────────────────────────┘
                 ↓
┌─────────────────────────────────────┐
│       Protenix-Mini-ESM            │
│     (ESM-only, no MSA needed)      │
└─────────────────────────────────────┘
```

### 3. Compute Optimization Layer

#### Kernel Implementations:
- **LayerNorm**: Fast custom CUDA kernels vs PyTorch fallback
- **Triangle Attention**: Triton/CuEquivariance/DeepSpeed options
- **Memory Management**: Gradient checkpointing, mixed precision (BF16/FP32)

#### Distributed Training:
- PyTorch DDP support
- DeepSpeed integration
- EMA (Exponential Moving Average) for stable training

### 4. Inference Pipeline

#### Two-Pass Strategy:
```
Pass 1: Screening Mode
├── Mini model variant
├── Reduced diffusion steps (2-5)
├── BF16 precision
└── Batch processing

Pass 2: High-Accuracy Mode
├── Full base model
├── Complete diffusion steps (20+)
├── FP32 precision
├── Constraint-guided prediction
└── Multiple seeds ensemble
```

### 5. Deployment Architecture

#### Container Strategy:
```
┌──────────────────────────────┐
│     Frontend Service         │
│   (API Gateway/Load Balancer)│
└──────────────────────────────┘
              ↓
┌──────────────────────────────┐
│    Inference Orchestrator    │
│  (Job Queue/Result Cache)    │
└──────────────────────────────┘
              ↓
┌──────────────────────────────┐
│     Model Serving Pods       │
│  ┌────────┐  ┌────────┐     │
│  │ Mini   │  │ Base   │     │
│  │ Model  │  │ Model  │     │
│  └────────┘  └────────┘     │
└──────────────────────────────┘
              ↓
┌──────────────────────────────┐
│     Storage Backend          │
│  (Model Weights/MSA Cache)   │
└──────────────────────────────┘
```

## Data Flow Architecture

### Input Processing:
1. **Structure Parsing**: PDB/CIF → Internal representation
2. **Feature Generation**: 
   - Primary sequence features
   - MSA features (optional)
   - Template features (optional)
   - Constraint features (contacts/pockets)
3. **Batching**: Dynamic batching based on token count (<768 for mini, <2048 for base)

### Model Processing:
1. **Embedding Generation**: Token embeddings + positional encoding
2. **Pairformer Processing**: 48 blocks for base, 12 for mini
3. **Diffusion Refinement**: Iterative coordinate generation
4. **Confidence Prediction**: Structure quality metrics

### Output Generation:
1. **Structure Assembly**: Token coordinates → full structure
2. **Format Conversion**: Internal → PDB/CIF output
3. **Metrics Calculation**: LDDT, RMSD, clash detection
4. **Result Caching**: 15-minute cache for repeated queries

## Memory Architecture

### Training Memory Requirements:
- **Base Model**: ~40GB VRAM (BF16), ~80GB (FP32)
- **Mini Model**: ~15GB VRAM (BF16), ~30GB (FP32)
- **Batch Size Impact**: Linear scaling with token count

### Inference Memory Requirements:
- **Base Model**: ~20GB VRAM (BF16)
- **Mini Model**: ~8GB VRAM (BF16)
- **MSA Cache**: ~450GB for full database

## Scalability Considerations

### Horizontal Scaling:
- Model replicas for parallel inference
- Distributed MSA generation
- Result caching layer

### Vertical Scaling:
- GPU memory for larger complexes
- CPU cores for MSA search
- Storage for feature caches

## Security Architecture

### Input Validation:
- File format validation
- Size limits enforcement
- Malicious input detection

### Process Isolation:
- Container-based isolation
- Resource limits (CPU/Memory/GPU)
- Network segmentation

### Data Protection:
- Encrypted storage for sensitive structures
- Secure API authentication
- Audit logging for predictions

## Fork Architecture Considerations

### Upstream Compatibility Layer
To maintain compatibility with ByteDance's rapidly evolving codebase:

```
┌─────────────────────────────────────┐
│         Fork Extensions              │
│  (Enterprise features, monitoring)   │
└─────────────────────────────────────┘
                ↓ implements
┌─────────────────────────────────────┐
│      Compatibility Interface         │
│   (Stable API for fork features)     │
└─────────────────────────────────────┘
                ↓ wraps
┌─────────────────────────────────────┐
│        Core Protenix (Upstream)      │
│   (Minimal modifications only)       │
└─────────────────────────────────────┘
```

### Modular Extension Points
Design fork features as plugins to minimize merge conflicts:

1. **Authentication Plugin**: Injected at API layer
2. **Monitoring Plugin**: Observes without modifying core
3. **Caching Plugin**: Wraps existing functions
4. **Deployment Plugin**: External configuration only

### Change Isolation Strategy
```python
# Fork features isolated in separate modules
fork_features/
├── enterprise/
│   ├── auth.py           # Authentication layer
│   ├── monitoring.py      # Metrics collection
│   └── billing.py         # Usage tracking
├── cloud/
│   ├── k8s_deploy.py      # Kubernetes configs
│   └── scaling.py         # Auto-scaling logic
└── compatibility/
    ├── wrapper.py         # Upstream API wrapper
    └── adapter.py         # Version adaptation
```

## Performance Optimization Points

### Critical Paths:
1. **MSA Generation**: Can be pre-computed and cached
2. **Feature Extraction**: Parallelizable across sequences
3. **Model Inference**: GPU-bound, benefits from batching
4. **Diffusion Steps**: Trade-off between speed and accuracy

### Optimization Strategies:
- Feature pre-computation
- Model quantization (INT8 exploration)
- Dynamic batching
- Adaptive diffusion steps
- Result caching

### Fork-Safe Optimizations
Optimizations that won't conflict with upstream:
- External caching layer
- Deployment configurations
- Monitoring instrumentation
- Resource management
- API extensions