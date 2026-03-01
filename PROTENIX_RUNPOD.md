# Protenix on RunPod - Operations Guide

## Quick Start

### 1. Launch Pod

Use the RunPod template with image:
```
ghcr.io/shadnygren/protenix:VHC-March2026-devel-with-weights
```

**Recommended GPU**: NVIDIA A40 (46GB VRAM) or better

### 2. Connect via SSH

```bash
# Direct SSH (requires exposed TCP port in RunPod template)
ssh root@<IP> -p <PORT> -i ~/.ssh/id_ed25519

# RunPod proxy SSH (always works, no exposed port needed)
ssh <podid>@ssh.runpod.io -i ~/.ssh/id_ed25519
```

On login you land in `/workspace` with `python3`, `nvcc`, and `protenix` on PATH.

### 3. Run a Prediction

```bash
cd /workspace
python3 runner/inference.py \
  --input_json_path /workspace/my_input.json \
  --dump_dir /workspace/output \
  --seeds 101 \
  --use_msa false
```

---

## Container Layout

| Path | Size | Persistence | Description |
|------|------|-------------|-------------|
| `/workspace/` | 50GB overlay | **Ephemeral** - lost on pod restart | Protenix source code, working directory |
| `/data/` | Network volume | **Persistent** across pod restarts | Store inputs, outputs, results here |
| `/root/` | overlay | Ephemeral | Home dir, weights, checkpoint symlink |
| `/root/.protenix/weights/` | ~1.4GB | Ephemeral (baked in image) | Pre-installed model weights |
| `/root/checkpoint/` | symlink | Ephemeral | Points to weights dir |
| `/dev/shm/` | 23GB | Ephemeral | Shared memory (tmpfs) |

### Key Insight: Use /data/ for Anything You Want to Keep

The `/data/` mount is a RunPod network volume (972TB shared cluster). It persists across pod stop/start cycles. Always save final results there:

```bash
# Good: results survive pod restart
python3 runner/inference.py --dump_dir /data/protenix_output ...

# Bad: results lost on pod restart
python3 runner/inference.py --dump_dir /workspace/output ...
```

---

## File Transfer with SCP

### Upload files to RunPod (from laptop)
```bash
# Single file
scp -P <PORT> -i ~/.ssh/id_ed25519 local_file.json root@<IP>:/workspace/

# Directory
scp -r -P <PORT> -i ~/.ssh/id_ed25519 ./my_inputs/ root@<IP>:/data/inputs/
```

### Download results from RunPod (to laptop)
```bash
# Single file
scp -P <PORT> -i ~/.ssh/id_ed25519 root@<IP>:/data/output/result.cif ./

# All CIF files
scp -P <PORT> -i ~/.ssh/id_ed25519 "root@<IP>:/data/output/*.cif" ./results/
```

### Via RunPod proxy (no exposed port needed)
SCP does NOT work through RunPod's proxy SSH (`ssh.runpod.io`). You must use the direct TCP connection with exposed port for SCP/SFTP.

---

## Protenix Input JSON Format

Each prediction requires a JSON file with this structure:

```json
[
  {
    "name": "my_prediction_name",
    "sequences": [
      {
        "proteinChain": {
          "id": "A",
          "sequence": "MKTLLILAVL...",
          "count": 1
        }
      },
      {
        "proteinChain": {
          "id": "B",
          "sequence": "QSAFPKRRVR...",
          "count": 1
        }
      }
    ]
  }
]
```

### Required Fields

| Field | Description |
|-------|-------------|
| `name` | Unique identifier for the prediction job |
| `sequences` | Array of entity dictionaries |
| `proteinChain.id` | Chain identifier (A, B, C...) |
| `proteinChain.sequence` | Amino acid sequence (one-letter codes) |
| `proteinChain.count` | Number of copies of this chain (usually 1) |

### Supported Entity Types

- `proteinChain` - protein sequences
- `dnaSequence` - DNA sequences
- `rnaSequence` - RNA sequences
- `ligand` - small molecules (CCD code or SMILES)
- `ion` - metal ions (CCD code)

### Multiple Predictions in One Run

The JSON is an array - include multiple prediction jobs:

```json
[
  {"name": "job1", "sequences": [...]},
  {"name": "job2", "sequences": [...]},
  {"name": "job3", "sequences": [...]}
]
```

---

## Output Structure

```
output/
├── ERR/                           # Error logs (if any)
│   └── job_name.txt
└── job_name/
    └── seed_101/
        └── predictions/
            ├── job_name_sample_0.cif                          # Structure prediction
            ├── job_name_sample_1.cif
            ├── job_name_sample_2.cif
            ├── job_name_sample_3.cif
            ├── job_name_sample_4.cif
            ├── job_name_summary_confidence_sample_0.json      # Confidence metrics
            ├── job_name_summary_confidence_sample_1.json
            ├── job_name_summary_confidence_sample_2.json
            ├── job_name_summary_confidence_sample_3.json
            └── job_name_summary_confidence_sample_4.json
```

### Confidence Metrics (per sample JSON)

| Metric | Range | Description |
|--------|-------|-------------|
| `plddt` | 0-100 | Per-residue confidence (higher = better) |
| `ptm` | 0-1 | Predicted TM-score (overall fold quality) |
| `iptm` | 0-1 | Interface pTM (protein-protein interaction quality) |
| `ranking_score` | 0-1 | Overall ranking (0.8*iptm + 0.2*ptm) |
| `gpde` | lower=better | Global pairwise distance error |
| `has_clash` | bool | Steric clash detected |
| `chain_plddt` | 0-1 | Per-chain pLDDT |
| `chain_iptm` | 0-1 | Per-chain interface pTM |

### Interpreting Results for STELLA-UHRF1

- **iptm > 0.6**: Strong predicted interaction
- **iptm 0.4-0.6**: Moderate interaction
- **iptm < 0.4**: Weak/no interaction predicted
- **ranking_score**: Use to compare variants (higher = better binding predicted)

---

## Inference Options

```bash
python3 runner/inference.py \
  --input_json_path INPUT.json \     # Required: input JSON
  --dump_dir OUTPUT_DIR \            # Output directory
  --seeds 101 \                      # Random seed(s), comma-separated for multiple
  --use_msa false \                  # true: search MSA databases (slower, more accurate)
  --use_template false \             # true: use PDB template search
  --model_name protenix_base_20250630_v1.0.0   # Model variant
```

### MSA Options

- `--use_msa false` - No MSA, fastest (~20s for small proteins), good for screening
- `--use_msa true` - Search MSA databases, better accuracy, requires database setup

### Multiple Seeds

Run with multiple seeds for sampling diversity:
```bash
--seeds 101,102,103
```
Each seed generates 5 samples, so 3 seeds = 15 total structures.

---

## Performance

### Benchmarked on NVIDIA A40 (46GB VRAM)

| Metric | Value | Notes |
|--------|-------|-------|
| **Protein** | UHRF1 PHD + mSTELLA Swap1 | 69 + 39 = 108 residues, 905 atoms |
| **Forward time** | 13.35s | 5 samples, 10 recycles each |
| **Total time (cold)** | ~124s | Includes model load + CUDA kernel compile |
| **Total time (warm)** | ~20s | Model already loaded in memory |
| **VRAM peak** | 3,271 MB (7.1%) | 46,068 MB available |
| **GPU utilization peak** | 57% | Average ~4.5% (idle during load) |
| **GPU temp peak** | 38°C | Barely warmed up |
| **GPU power peak** | 195W | Average ~51W |
| **RAM peak** | ~58 GB | Container sees host RAM via cgroups |

### GPU Sizing Guide

Based on VRAM usage of ~3.3 GB for 108 residues (no MSA):

| GPU | VRAM | Est. Max Residues | RunPod $/hr | Fit? |
|-----|------|-------------------|-------------|------|
| RTX 3060 | 12 GB | ~400 | Local | Yes |
| RTX 4000 Ada | 20 GB | ~700 | $0.24 | Yes |
| RTX A4000 | 16 GB | ~500 | $0.28 | Yes |
| A40 | 46 GB | ~2000+ | $0.76 | Overkill for small proteins |
| A100 | 80 GB | ~3000+ | $1.84 | Only for large complexes |

**Note**: VRAM scales roughly quadratically with token count. These estimates are for no-MSA mode.

### Performance Notes
- Model loading takes ~2 minutes on first run (cold start)
- CUDA kernel `fast_layer_norm_cuda_v2` compiles on first import (~2 min, devel image only)
- Subsequent predictions in the same session reuse the loaded model (~20s per prediction)
- Use `scripts/protenix_benchmark.py` to profile on different GPUs

### Container vs Host Resources
RunPod containers can see host CPU/RAM counts but are limited by cgroup settings. Use `cat /sys/fs/cgroup/memory.max` and `cat /sys/fs/cgroup/cpu.max` to see actual container limits. GPU VRAM is dedicated and accurate.

---

## SSH Connection Notes

### Direct SSH (exposed TCP port)
- Supports: SSH, SCP, SFTP, rsync
- Requires: RunPod template with "Expose TCP Ports" enabled
- Connection: `ssh root@<IP> -p <PORT> -i ~/.ssh/id_ed25519`
- Best for: file transfer, automated scripts

### RunPod Proxy SSH
- Supports: SSH only (interactive terminal)
- No SCP/SFTP support through proxy
- Connection: `ssh <podid>@ssh.runpod.io -i ~/.ssh/id_ed25519`
- Requires `-tt` flag for forced PTY allocation
- Best for: quick access when no exposed port

### For Claude Code automation
Use direct SSH without `-tt` flag:
```bash
# Run remote commands
ssh -o StrictHostKeyChecking=no root@<IP> -p <PORT> -i ~/.ssh/id_ed25519 "cd /workspace && python3 runner/inference.py ..."

# Transfer files
scp -P <PORT> -i ~/.ssh/id_ed25519 file root@<IP>:/workspace/
```

---

## Troubleshooting

### "command not found" for python3/nvcc
The `.bashrc` sets PATH on login. For non-interactive SSH commands, prefix with:
```bash
ssh ... "export PATH=/opt/conda/bin:/usr/local/cuda/bin:\$PATH && your_command"
```

### "entity type must be proteinChain"
Use `proteinChain` (not `protein`) in your input JSON.

### "KeyError: 'count'"
Each entity needs a `"count": 1` field.

### CUDA kernel compilation on first run
The `fast_layer_norm_cuda_v2` kernel compiles on first import (~2 min). This is normal for the devel image.

### Weights not found
Weights are at `/root/.protenix/weights/protenix_base_20250630_v1.0.0/model.pt` with a symlink at `/root/checkpoint/`. Ensure `PROTENIX_ROOT_DIR=/root` is set.

### Pod data lost after restart
Only `/data/` (network volume) persists. Always save important results to `/data/`.
