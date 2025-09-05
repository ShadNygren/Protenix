# Protenix Design Documentation

## Design Philosophy

Protenix follows a modular, extensible design that prioritizes:
1. **Accuracy First**: Default to highest accuracy, with explicit opt-in for speed optimizations
2. **Progressive Enhancement**: Start simple, add complexity when needed
3. **Clear Trade-offs**: Make performance vs accuracy choices transparent
4. **Enterprise Readiness**: Production-grade reliability, security, and scalability
5. **Fork Compatibility**: Maintain upstream compatibility to enable contribution back
6. **Real Data Only**: Business logic uses only real data; test data strictly isolated

## Core Design Patterns

### 1. Two-Pass Prediction Strategy

#### Design Rationale:
Balances computational efficiency with accuracy for drug discovery workflows.

#### Implementation:
```python
class TwoPassPredictor:
    """
    Pass 1: Rapid screening of candidates
    Pass 2: High-accuracy validation of promising targets
    """
    
    def screen_candidates(self, structures: List[Structure]) -> List[Candidate]:
        """
        Uses Protenix-Mini with:
        - 2-5 diffusion steps
        - BF16 precision
        - Batch processing
        - Returns top 10% by confidence score
        """
        
    def validate_candidates(self, candidates: List[Candidate]) -> List[Result]:
        """
        Uses Protenix-Base with:
        - 20+ diffusion steps
        - FP32 precision
        - Multiple seeds ensemble
        - Constraint-guided refinement
        """
```

### 2. Modular Pipeline Architecture

#### Component Isolation:
Each pipeline stage is independently replaceable:

```
[Input] → [Parser] → [Featurizer] → [Model] → [Output]
   ↓         ↓           ↓            ↓          ↓
Custom    Format      Feature      Model      Format
Handler   Plugin     Extension    Variant    Converter
```

#### Benefits:
- Easy A/B testing of components
- Gradual upgrades without system-wide changes
- Custom implementations for specific use cases

### 3. Precision-Aware Computation

#### Design Principle:
Different components require different numerical precision.

#### Implementation Strategy:
```python
class PrecisionManager:
    """Manages precision levels across pipeline stages"""
    
    PRECISION_MAP = {
        'input_processing': 'fp32',      # Maintain input accuracy
        'msa_processing': 'fp16',        # Acceptable for alignments
        'model_forward': 'bf16',         # Training standard
        'diffusion_critical': 'fp32',    # Coordinate generation
        'confidence_head': 'fp32',       # Scoring accuracy
        'output_coordinates': 'fp64'     # Final structure precision
    }
```

### 4. Constraint System Design

#### Hierarchical Constraints:
```
Global Constraints (Structure-wide)
    ↓
Residue Constraints (Contact maps)
    ↓
Atom Constraints (Specific interactions)
    ↓
Pocket Constraints (Binding sites)
```

#### Constraint Application:
- Soft constraints during diffusion (gradual enforcement)
- Hard constraints for final validation
- Weighted combination for multi-constraint scenarios

### 5. Caching Architecture

#### Multi-Level Cache:
```
L1: Result Cache (15 min, exact matches)
    ↓
L2: Feature Cache (24 hours, computed features)
    ↓
L3: MSA Cache (persistent, sequence alignments)
    ↓
L4: Model Cache (persistent, weight checkpoints)
```

#### Cache Key Design:
```python
def generate_cache_key(input_data):
    """Deterministic key generation for cache lookup"""
    return hash(
        sequence + 
        model_version + 
        precision_mode + 
        constraint_hash + 
        seed
    )
```

## Error Handling Design

### Graceful Degradation:
```python
class PredictionPipeline:
    def predict(self, structure):
        try:
            # Attempt high-accuracy prediction
            return self.predict_full(structure)
        except GPUMemoryError:
            # Fall back to mini model
            logger.warning("Falling back to Mini model due to memory constraints")
            return self.predict_mini(structure)
        except MSATimeoutError:
            # Continue without MSA
            logger.warning("MSA timeout, using ESM-only features")
            return self.predict_esm_only(structure)
```

### Recovery Strategies:
1. **Memory Issues**: Automatic batch size reduction
2. **MSA Failures**: Fallback to ESM embeddings
3. **Constraint Conflicts**: Relaxation with warnings
4. **Timeout Scenarios**: Progressive quality reduction

## API Design

### RESTful Interface:
```yaml
endpoints:
  /predict/screen:
    method: POST
    description: Fast screening mode
    params:
      - structures: array
      - batch_size: integer
      - precision: bf16|fp16
    
  /predict/validate:
    method: POST
    description: High-accuracy validation
    params:
      - structure: object
      - constraints: array
      - seeds: array
      - ensemble_size: integer
      
  /predict/status/{job_id}:
    method: GET
    description: Check prediction status
    
  /predict/result/{job_id}:
    method: GET
    description: Retrieve results
```

### Response Format:
```json
{
  "job_id": "uuid",
  "status": "completed",
  "mode": "screening|validation",
  "results": {
    "structure": "pdb_string",
    "confidence": {
      "plddt": 0.95,
      "pae": 0.85,
      "ranking_score": 0.92
    },
    "metrics": {
      "rmsd": 1.2,
      "lddt": 0.89,
      "clash_score": 0.01
    },
    "metadata": {
      "model_version": "v0.5.0",
      "precision": "fp32",
      "diffusion_steps": 20,
      "computation_time": 120.5
    }
  }
}
```

## Database Design

### Structure Storage:
```sql
-- Prediction results table
CREATE TABLE predictions (
    id UUID PRIMARY KEY,
    input_hash VARCHAR(64) INDEX,
    structure_pdb TEXT,
    confidence_scores JSONB,
    metrics JSONB,
    model_version VARCHAR(20),
    created_at TIMESTAMP,
    computation_time FLOAT,
    cache_expires_at TIMESTAMP
);

-- Feature cache table
CREATE TABLE feature_cache (
    sequence_hash VARCHAR(64) PRIMARY KEY,
    features BYTEA,  -- Compressed numpy arrays
    feature_version VARCHAR(20),
    created_at TIMESTAMP,
    expires_at TIMESTAMP
);

-- MSA cache table  
CREATE TABLE msa_cache (
    sequence_hash VARCHAR(64) PRIMARY KEY,
    msa_data BYTEA,  -- Compressed alignment
    search_db VARCHAR(50),
    created_at TIMESTAMP
);
```

## Configuration Management

### Hierarchical Configuration:
```yaml
# base_config.yaml
model:
  version: "v0.5.0"
  precision: "fp32"
  diffusion_steps: 20

# screening_config.yaml
extends: base_config
model:
  variant: "mini"
  precision: "bf16"
  diffusion_steps: 5

# validation_config.yaml  
extends: base_config
model:
  ensemble_size: 5
  use_constraints: true
  use_templates: true
```

## Monitoring Design

### Key Metrics:
```python
METRICS = {
    # Performance metrics
    'prediction_latency': Histogram,
    'gpu_utilization': Gauge,
    'memory_usage': Gauge,
    'batch_size': Histogram,
    
    # Quality metrics
    'confidence_scores': Histogram,
    'rmsd_distribution': Histogram,
    'constraint_violations': Counter,
    
    # System health
    'cache_hit_rate': Gauge,
    'msa_timeout_rate': Counter,
    'fallback_rate': Counter,
    'error_rate': Counter
}
```

### Alerting Rules:
- GPU memory > 90% for 5 minutes
- Prediction latency p99 > 10 minutes
- Error rate > 1% in 5-minute window
- Cache hit rate < 20%

## Security Design

### Input Validation:
```python
class InputValidator:
    MAX_SEQUENCE_LENGTH = 2048
    MAX_FILE_SIZE = 100_000_000  # 100MB
    ALLOWED_FORMATS = ['pdb', 'cif', 'json']
    
    def validate(self, input_data):
        # Size checks
        # Format validation
        # Malicious pattern detection
        # Rate limiting per user
```

### Access Control:
```python
class AccessControl:
    ROLES = {
        'basic': {
            'max_daily_predictions': 100,
            'allowed_models': ['mini'],
            'max_batch_size': 10
        },
        'premium': {
            'max_daily_predictions': 10000,
            'allowed_models': ['mini', 'base'],
            'max_batch_size': 100
        },
        'enterprise': {
            'max_daily_predictions': None,
            'allowed_models': ['all'],
            'max_batch_size': 1000,
            'priority_queue': True
        }
    }
```

## Deployment Design

### Fork-Aware Container Structure:
```dockerfile
# Base image with CUDA support
FROM nvidia/cuda:12.1-runtime-ubuntu22.04 AS base

# Upstream-compatible layer
FROM base AS upstream
COPY --from=builder /app/protenix /app/protenix

# Fork additions layer  
FROM upstream AS fork
COPY --from=builder /app/fork_features /app/fork_features

# Model variants
FROM fork AS mini
COPY models/mini /models/

FROM fork AS base
COPY models/base /models/
```

This layered approach allows us to:
1. Easily sync upstream changes
2. Keep fork features isolated
3. Build both upstream-compatible and enhanced versions

### Orchestration:
```yaml
# kubernetes deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: protenix-inference
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: protenix
        resources:
          limits:
            nvidia.com/gpu: 1
            memory: "32Gi"
          requests:
            nvidia.com/gpu: 1
            memory: "16Gi"
```

## Fork Development Strategy

### Fork Constraints
As an actively maintained upstream project (35 closed PRs, 0 open), we must:
1. **Minimize Divergence**: Keep changes small and focused
2. **Upstream First**: Design features for upstream contribution
3. **Feature Branches**: One feature per branch for clean PRs
4. **Rapid Integration**: Submit PRs quickly to avoid conflicts
5. **Compatibility Layer**: Isolate fork-specific features

### Branch Strategy
```
upstream/main (ByteDance official)
    ↓ fetch daily
main (our fork tracking upstream)
    ↓
develop (integration branch)
    ├── feature/bug-fix-XXX (for upstream PR)
    ├── feature/enhancement-YYY (for upstream PR)
    └── fork/enterprise-features (our additions)
```

### Change Categories

#### Upstream-Compatible (Priority 1)
Changes that should be contributed back immediately:
- Bug fixes
- Performance improvements
- Documentation improvements  
- Additional tests
- General features

#### Fork-Specific (Priority 2)
Changes we maintain separately:
- Enterprise authentication
- Cloud deployment configs
- Monitoring/billing features
- Custom integrations

### PR Strategy
```python
class ChangeManager:
    """Manage changes for upstream contribution"""
    
    def categorize_change(self, change):
        if change.type in ['bug_fix', 'performance', 'docs']:
            return 'upstream_immediate'
        elif change.type in ['feature'] and change.is_general:
            return 'upstream_proposed'
        else:
            return 'fork_only'
    
    def prepare_pr(self, change):
        # Keep PRs small and focused
        if change.lines_changed > 500:
            return self.split_into_smaller_prs(change)
        return change
```

## Testing Strategy

Testing is critical for fork development. See [TESTING.md](./TESTING.md) for comprehensive testing documentation including:
- Test data isolation principles
- Fork-specific test structure  
- Upstream compatibility testing
- Performance benchmarking
- CI/CD integration