# Protenix Testing Documentation

## Testing Philosophy

### Core Principles
1. **Real Data Default**: Production code NEVER contains test data or mocks
2. **Strict Isolation**: All test data confined to dedicated test harnesses
3. **Deterministic Results**: No random test data - only fixed, versioned fixtures
4. **Fork Compatibility**: All tests must pass on both fork and upstream
5. **Upstream First**: Test changes that will be contributed upstream
6. **Bug Fix = Test Case**: Every bug fix PR must include a test that fails before and passes after

### CRITICAL: Internal Files Must Never Be in PRs
**These files must NEVER be included in upstream PRs:**
- `ARCHITECTURE.md`, `CLAUDE.md`, `DESIGN.md`, `ROADMAP.md`, `STRATEGY.md`, `TESTING.md`
- Any file with `*SHAD*` in the filename
- `fork_features/` directory
- `tests_fork/` directory

## Fork Testing Strategy

Since Protenix is an actively maintained upstream project with rapid PR merging (35 closed, 0 open), our testing must:

### Maintain Upstream Compatibility
```bash
# Before any development
git fetch upstream
git rebase upstream/main

# Run upstream tests first
python -m unittest discover tests/

# Then run our additional tests
python -m unittest discover tests_fork/
```

### Test Categories for Fork Development

#### 1. Upstream-Compatible Tests
Tests that can be contributed back:
- Bug fix validations
- Performance regression tests  
- Additional edge cases
- Documentation examples

#### 2. Fork-Specific Tests
Tests for our enhancements:
- Two-pass prediction system
- Enterprise features
- Cloud deployment
- Monitoring endpoints

## Current Test Structure (ByteDance)

### Existing Tests
ByteDance uses `unittest` framework with the following tests:
```
tests/                      # Upstream tests (we can ADD to these)
├── __init__.py
├── test_attention_pair_bias.py    # AttentionPairBias module
├── test_condition_transition.py   # ConditionedTransitionBlock
├── test_diffusion_transformer.py  # Diffusion transformer
├── test_frame.py                  # Frame operations
├── test_local_rearrange.py        # Local rearrangement
├── test_lr_schedule.py            # Learning rate scheduling
├── test_utils.py                  # Utility functions
└── test_weighted_rigid_align.py   # Weighted rigid alignment
```

### ByteDance Test Pattern
```python
import unittest
import torch

class TestModuleName(unittest.TestCase):
    def setUp(self):
        self._start_time = time.time()
        # Initialize test conditions
        
    def tearDown(self):
        # Log test duration
        elapsed = time.time() - self._start_time
        print(f"Test took {elapsed:.3f}s")
        
    def test_functionality(self):
        # Test with deterministic inputs
        x = torch.rand([batch_size, ...])
        result = module(x)
        self.assertEqual(result.shape, expected_shape)
```

## Our Test Structure

### Directory Organization
```
tests/                      # Upstream tests (we ADD tests here for PRs)

tests_fork/                 # Our additional tests
├── harness/               # Test infrastructure ONLY
│   ├── __init__.py
│   ├── fixtures/          # Static, deterministic test data
│   │   ├── proteins.py    # Known protein sequences
│   │   ├── structures.py  # Reference PDB structures  
│   │   ├── expected.py    # Expected outputs
│   │   └── VERSION        # Fixture version tracking
│   ├── mocks/            # Mock services (NEVER in production)
│   │   ├── msa_mock.py
│   │   ├── model_mock.py
│   │   └── __init__.py
│   └── validators/       # Result validators
│       ├── structure_validator.py
│       ├── metric_validator.py
│       └── __init__.py
├── unit/                 # Unit tests (50%)
│   ├── test_featurizer.py
│   ├── test_tokenizer.py
│   └── test_diffusion.py
├── integration/          # Integration tests (30%)
│   ├── test_pipeline.py
│   ├── test_api.py
│   └── test_cache.py
└── e2e/                  # End-to-end tests (20%)
    ├── test_full_prediction.py
    ├── test_two_pass.py
    └── test_constraints.py
```

## Test Data Management

### CRITICAL: Production/Test Separation
```python
# ❌ NEVER DO THIS in production code:
class Predictor:
    def predict(self, sequence):
        if self.test_mode:  # WRONG!
            return mock_result
        return real_prediction

# ✅ CORRECT approach:
# production/predictor.py
class Predictor:
    def predict(self, sequence):
        # Only real logic, no test references
        return self.model.predict(sequence)

# tests_fork/harness/mocks/predictor_mock.py  
class MockPredictor:
    """Test harness ONLY - never imported in production"""
    def predict(self, sequence):
        return self.fixtures.expected_results[sequence]
```

### Test Fixtures

#### Static Protein Fixtures
```python
# tests_fork/harness/fixtures/proteins.py
"""
Fixed test proteins with known structures
Version: 1.0.0
Last Updated: 2025-01-01
"""

class TestProteins:
    # Small protein for quick tests
    UBIQUITIN = {
        'sequence': 'MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG',
        'pdb_id': '1UBQ',
        'length': 76,
        'expected_plddt': 0.92,
        'expected_rmsd': 1.1,
        'test_category': 'small_globular'
    }
    
    # Antibody-antigen complex
    ANTIBODY_COMPLEX = {
        'heavy_chain': 'EVQLVESGGGLVQPGGSLRLSCAASGFTFSDYYMSWVRQAPGKGLEWVS',
        'light_chain': 'DIQMTQSPSSLSASVGDRVTITCRASQSISSYLNWYQQKPGKAPKLLIY',  
        'antigen': 'MKTAYDELAAEAFLEENTPILHTYDDNSTFTRYLEVNCCVNFQKAVELA',
        'pdb_id': '1IGT',
        'expected_interface_lddt': 0.85,
        'expected_binding_rmsd': 2.3,
        'test_category': 'protein_complex'
    }
    
    # Protein-ligand complex
    KINASE_INHIBITOR = {
        'protein': 'MENFQKVEKIGEGTYGVVYKARNKLTGEVVALKKIRLDTETEGVPSTA',
        'ligand_smiles': 'CC1=C2C=C(C=CC2=NN1)C3=CC(=CN=C3)OCC(C)C',
        'pdb_id': '1ATP',
        'expected_docking_score': -8.5,
        'expected_ligand_rmsd': 1.8,
        'test_category': 'protein_ligand'
    }
```

#### Version Control for Test Data
```python
# tests_fork/harness/fixtures/VERSION
TEST_DATA_VERSION = "1.0.0"
COMPATIBLE_WITH_UPSTREAM = "v0.5.0"
LAST_SYNC_DATE = "2025-01-01"

# Fixture changes must increment version
# Breaking changes require major version bump
```

## Priority Bug Fix Tests

### Tests Needed for Current Issues

#### Issue #182: Pip Installation Error
```python
# tests/test_installation.py
import unittest
import subprocess
import sys

class TestInstallation(unittest.TestCase):
    """Test that package installs correctly without Pydantic errors"""
    
    def test_deepspeed_pydantic_compatibility(self):
        """Verify DeepSpeed and Pydantic versions are compatible"""
        try:
            import deepspeed
            import pydantic
            # Should not raise TypeError about json_schema_input_schema
            from deepspeed.runtime.config import DeepSpeedConfig
            self.assertTrue(True)
        except TypeError as e:
            if "json_schema_input_schema" in str(e):
                self.fail(f"Pydantic/DeepSpeed compatibility issue: {e}")
```

#### Issue #185: Triton Version Compatibility
```python
# tests/test_triton_compatibility.py
import unittest
import torch

class TestTritonCompatibility(unittest.TestCase):
    """Test Triton kernel compatibility across GPU types"""
    
    def test_triton_import(self):
        """Verify Triton components import correctly"""
        try:
            from protenix.model.tri_attention import op
            self.assertTrue(True)
        except ImportError as e:
            if "Not Supported" in str(e):
                self.skipTest("Triton not supported on this GPU")
                
    def test_fallback_to_torch(self):
        """Verify torch fallback works when Triton fails"""
        # Test that trimul_kernel="torch" works as fallback
        from protenix.model.modules import triangular_multiplicative_update
        # Should work with torch backend
```

#### Issue #176: ESM Weights Loading Error
```python
# tests/test_esm_loading.py
import unittest

class TestESMLoading(unittest.TestCase):
    """Test ESM model loading with PyTorch 2.6+"""
    
    def test_esm_weights_load(self):
        """Verify ESM weights load correctly in PyTorch 2.6+"""
        import torch
        if torch.__version__ >= "2.6":
            # Test loading ESM weights doesn't fail
            from protenix.data.compute_esm import load_esm_model
            model = load_esm_model()
            self.assertIsNotNone(model)
```

## Test Implementation

### Unit Tests (50%)

#### Test Individual Components
```python
# tests_fork/unit/test_featurizer.py
import unittest
from tests_fork.harness.fixtures import TestProteins

class TestFeaturizer(unittest.TestCase):
    """Test featurizer with deterministic inputs"""
    
    @classmethod
    def setUpClass(cls):
        cls.fixtures = TestProteins()
        
    def test_sequence_features(self):
        """Test feature extraction for known sequence"""
        from protenix.data.featurizer import Featurizer
        
        featurizer = Featurizer()
        features = featurizer.extract(self.fixtures.UBIQUITIN['sequence'])
        
        # Validate against known values
        self.assertEqual(features.shape[0], 76)
        self.assertAlmostEqual(features.mean(), 0.123, places=3)
```

### Integration Tests (30%)

#### Test Component Interactions
```python
# tests_fork/integration/test_pipeline.py
class TestPipeline(unittest.TestCase):
    """Test full pipeline stages"""
    
    def test_json_to_features(self):
        """Test JSON parsing through featurization"""
        # Use static test structure
        test_json = self.load_fixture('test_structure.json')
        
        # Process through pipeline
        features = pipeline.process(test_json)
        
        # Validate intermediate outputs
        self.assertIsNotNone(features['msa'])
        self.assertIsNotNone(features['template'])
        self.assertEqual(features['tokens'].shape[0], 384)
```

### End-to-End Tests (20%)

#### Test Complete Predictions
```python
# tests_fork/e2e/test_full_prediction.py
class TestFullPrediction(unittest.TestCase):
    """Test complete prediction workflow"""
    
    def test_known_structure_prediction(self):
        """Predict structure with known solution"""
        # Use real PDB structure
        test_pdb = '1UBQ'
        
        # Run prediction
        result = predict_structure(test_pdb)
        
        # Compare to experimental structure
        rmsd = calculate_rmsd(result, experimental_structure)
        self.assertLess(rmsd, 2.0)  # Must be within 2 Angstroms
```

## Testing for Fork Development

### Branch Testing Strategy
```bash
# Feature branch testing
git checkout -b feature/two-pass-prediction

# Run tests before changes (baseline)
python -m pytest tests/ --benchmark-save=baseline

# Make changes

# Run tests after changes
python -m pytest tests/ tests_fork/ --benchmark-compare=baseline

# Ensure no regression
python scripts/check_regression.py
```

### Pre-PR Testing Checklist
```bash
#!/bin/bash
# pre_pr_test.sh

echo "Running pre-PR test suite..."

# 1. Sync with upstream
git fetch upstream
git rebase upstream/main

# 2. Run upstream tests
echo "Testing upstream compatibility..."
python -m unittest discover tests/

# 3. Run our tests
echo "Testing fork features..."  
python -m unittest discover tests_fork/

# 4. Check performance
echo "Running performance benchmarks..."
python tests_fork/benchmarks/performance.py

# 5. Validate no test data in production
echo "Checking for test data leakage..."
grep -r "TestProteins\|MockPredictor\|test_fixtures" protenix/ && exit 1

# 6. Type checking
echo "Running type checks..."
mypy protenix/

# 7. Linting
echo "Running linters..."
flake8 protenix/ tests_fork/

echo "All checks passed!"
```

## Performance Testing

### Benchmark Suite
```python
# tests_fork/benchmarks/performance.py
class PerformanceBenchmarks:
    """Track performance metrics across versions"""
    
    PERFORMANCE_TARGETS = {
        'mini_inference_time': 60,  # seconds
        'base_inference_time': 300,  # seconds
        'memory_usage_gb': 16,
        'gpu_utilization': 0.8
    }
    
    def benchmark_inference_speed(self):
        """Measure inference time for standard proteins"""
        times = []
        for protein in self.STANDARD_SET:
            start = time.time()
            predict(protein)
            times.append(time.time() - start)
        
        return {
            'mean': np.mean(times),
            'p95': np.percentile(times, 95),
            'p99': np.percentile(times, 99)
        }
```

## Continuous Integration

### GitHub Actions Workflow
```yaml
# .github/workflows/test.yml
name: Test Suite

on:
  pull_request:
    branches: [main, develop]
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: [3.11, 3.12]
        
    steps:
    - uses: actions/checkout@v3
    
    - name: Fetch upstream
      run: |
        git remote add upstream https://github.com/bytedance/Protenix
        git fetch upstream
        
    - name: Check merge compatibility
      run: |
        git merge-tree $(git merge-base HEAD upstream/main) HEAD upstream/main
        
    - name: Run upstream tests
      run: python -m pytest tests/
      
    - name: Run fork tests
      run: python -m pytest tests_fork/
      
    - name: Check test isolation
      run: |
        # Ensure no test code in production
        ! grep -r "test_fixtures\|MockPredictor" protenix/
        
    - name: Performance regression
      run: python tests_fork/benchmarks/regression_check.py
```

## Test Utilities

### Structure Comparison
```python
# tests_fork/harness/validators/structure_validator.py
class StructureValidator:
    """Validate predicted structures"""
    
    @staticmethod
    def calculate_rmsd(predicted, reference):
        """Calculate RMSD between structures"""
        # Implementation using biotite or similar
        pass
    
    @staticmethod  
    def validate_confidence(structure, min_plddt=0.7):
        """Check confidence scores"""
        return structure.plddt.mean() > min_plddt
    
    @staticmethod
    def check_clashes(structure, threshold=0.4):
        """Detect atomic clashes"""
        # Check for overlapping atoms
        pass
```

## Testing Guidelines

### Do's and Don'ts

#### ✅ DO:
- Use deterministic test data from fixtures
- Test both success and failure paths
- Validate against experimental structures
- Keep tests fast and isolated
- Document expected values
- Version test data
- Run upstream tests first

#### ❌ DON'T:
- Use random test data
- Import test code in production
- Modify upstream test files
- Create flaky tests
- Hard-code paths
- Skip tests without documentation
- Assume test environment

## Test Coverage Requirements

### Minimum Coverage Targets
- Overall: 80%
- Core algorithms: 90%
- API endpoints: 95%
- Error paths: 75%
- Fork features: 85%

### Coverage Reporting
```bash
# Generate coverage report
pytest --cov=protenix --cov=tests_fork tests_fork/

# Check coverage gates
python scripts/check_coverage.py --min-coverage=80
```

## Troubleshooting Tests

### Common Issues

#### Test Data Not Found
```python
# Always use absolute paths for fixtures
import os
FIXTURE_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')
```

#### Upstream Test Failures
```bash
# Isolate upstream issues
git checkout upstream/main
python -m pytest tests/  # If fails, upstream issue

# Test our changes only
git checkout feature/branch
python -m pytest tests_fork/  # Our tests only
```

#### Performance Regression
```python
# Compare with baseline
pytest tests_fork/benchmarks/ --benchmark-compare=0001
# Where 0001 is the baseline benchmark number
```

## Test Maintenance

### Weekly Tasks
1. Sync with upstream tests
2. Update fixture versions if needed
3. Review and fix flaky tests
4. Update benchmark baselines

### Monthly Tasks  
1. Full regression suite
2. Coverage analysis
3. Performance trend analysis
4. Test optimization

### Release Tasks
1. Full test suite on multiple Python versions
2. GPU vs CPU compatibility
3. Docker container tests
4. Cross-platform validation