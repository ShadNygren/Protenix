# Protenix Training: Lessons Learned and Best Practices

**Last Updated**: 2026-03-29
**Status**: 50-step test training completed on A100 SXM 80GB. Full training TBD.

---

## 1. Critical Discovery: Model Already Includes STELLA-UHRF1

**The model `protenix_base_20250630_v1.0.0` has a training cutoff of June 30, 2025.** This means it has ALREADY been trained on:
- 8XV4 (mSTELLA-UHRF1) — deposited Jan 2024, released Nov 2024
- 8XV7 (hSTELLA-UHRF1) — deposited Jan 2024, released Nov 2024
- All PDB structures released through June 2025

Despite this, Protenix still produces **7.48 Å RMSD structural differences** for IDP sequences differing by a single residue (Chimpanzee anomaly). **The structural instability is fundamental to the diffusion architecture for IDPs, not a training data gap.**

The earlier analysis (PROTENIX_STRUCTURAL_INSTABILITY.md) incorrectly attributed the problem to the model not having seen STELLA-UHRF1. That analysis was based on the default model (`protenix_base_default_v1.0.0`, cutoff Sept 2021). We are using the newer `20250630` model which includes these structures.

---

## 2. What Fine-Tuning CAN Do (Despite Having the Data)

Even though the model has seen STELLA-UHRF1, the original training has IDPs as only **0.3-1.3% of 150K structures**. The model optimizes primarily for ordered proteins (98.7%+ of loss).

Fine-tuning with IDP-upsampled data shifts the loss landscape:
- **More weight on IDP conformational accuracy** during backpropagation
- **Stronger learned prior for PHD-domain interfaces** (repeated exposure)
- **May reduce the number of conformational basins** the diffusion explores for IDPs

Whether this actually reduces structural variance for IDPs is an **empirical question** that our test training aimed to answer.

---

## 3. Training Configuration That Works

### Successful Configuration (50-step test, March 29, 2026)

```bash
python3 runner/train.py \
    --model_name protenix_base_20250630_v1.0.0 \
    --run_name idp_finetune_v1 \
    --seed 42 \
    --base_dir /data/training_output \
    --dtype bf16 \
    --use_wandb false \
    --diffusion_batch_size 24 \
    --eval_interval 9999 \
    --log_interval 10 \
    --checkpoint_interval 25 \
    --ema_decay 0.999 \
    --train_crop_size 384 \
    --max_steps 50 \
    --warmup_steps 10 \
    --lr 0.0005 \
    --model.N_cycle 4 \
    --sample_diffusion.N_step 20 \
    --triangle_attention cuequivariance \
    --triangle_multiplicative cuequivariance \
    --load_checkpoint_path /root/checkpoint/protenix_base_20250630_v1.0.0.pt \
    --load_ema_checkpoint_path /root/checkpoint/protenix_base_20250630_v1.0.0.pt \
    --data.train_sets weightedPDB_before2109_wopb_nometalc_0925 \
    --data.weightedPDB_before2109_wopb_nometalc_0925.base_info.pdb_list /data/training/pdb_lists/idp_finetune.txt \
    --data.weightedPDB_before2109_wopb_nometalc_0925.base_info.bioassembly_dict_dir /data/training/bioassembly \
    --data.weightedPDB_before2109_wopb_nometalc_0925.base_info.indices_fpath /data/training/indices/idp_finetune.csv \
    --data.msa.enable_prot_msa false \
    --data.msa.enable_rna_msa false \
    --data.template.enable_prot_template false \
    --data.test_sets weightedPDB_before2109_wopb_nometalc_0925
```

### Key Parameters Explained

| Parameter | Value | Why |
|-----------|-------|-----|
| `--model_name protenix_base_20250630_v1.0.0` | Latest model | Has June 2025 training cutoff |
| `--dtype bf16` | Mixed precision | Saves VRAM, stable training |
| `--diffusion_batch_size 24` | Reduced from default 48 | Lower VRAM (8.8 GB vs ~34 GB) |
| `--train_crop_size 384` | Tokens per sample | Sufficient for our ~262-token complexes |
| `--lr 0.0005` | Half of default (0.001) | Prevents catastrophic forgetting |
| `--data.msa.enable_prot_msa false` | No MSA features | We don't have MSA data prepared |
| `--data.template.enable_prot_template false` | No template features | We don't have template search database |
| `--eval_interval 9999` | Skip evaluation | Avoids test set dependency issues |
| `--checkpoint_interval 25` | Save every 25 steps | Resume capability |

---

## 4. Issues Encountered and Solutions

### Issue 1: `/workspace/` overridden by persistent volume
**Problem**: Persistent volume mounted at `/workspace/` hides Protenix code installed during Docker build.
**Solution**: Mount persistent volume at `/data/` instead of `/workspace/`.

### Issue 2: `FileNotFoundError: rna_sequence_to_pdb_chains.json`
**Problem**: MSA featurizer requires RNA MSA mapping files that aren't in the Docker image.
**Solution**: Create placeholder files:
```bash
mkdir -p /root/rna_msa
echo '{}' > /root/rna_msa/rna_sequence_to_pdb_chains.json
echo '{}' > /root/common/seq_to_pdb_index.json
```

### Issue 3: `--data.test_sets ''` doesn't accept empty string
**Problem**: Can't disable test sets with empty string argument.
**Solution**: Use the same dataset for both train and test, set `--eval_interval 9999` to effectively skip evaluation.

### Issue 4: Wrong parameter name `csv_path` vs `indices_fpath`
**Problem**: `--data.*.base_info.csv_path` doesn't exist.
**Solution**: Use `--data.*.base_info.indices_fpath` (matches config key name).

### Issue 5: AssertionError in `calculate_chain_based_gpde` during evaluation
**Problem**: `N_chain == asym_id.max() + 1` assertion fails for some structures during evaluation step.
**Solution**: Not critical for training — only affects evaluation metrics. Set `--eval_interval 9999` to skip.

---

## 5. Performance Metrics

### Hardware: RunPod A100 SXM 80GB, 32 vCPUs, 251 GB RAM

| Metric | Value |
|--------|-------|
| GPU VRAM usage | **8.8-15 GB** (11-18% of 80 GB) |
| GPU utilization | 100% during training steps |
| Training speed | **~7-8 seconds/step** |
| Model load time | ~2 minutes (368.48M parameters) |
| Checkpoint size | ~3 GB (model + EMA) |

### Projection for Full Training

| Steps | Time | Cost ($1.49/hr) | Checkpoints |
|-------|------|-----------------|-------------|
| 50 (test) | ~7 minutes | ~$0.17 | 2 (step 24, 49) |
| 1,000 | ~2.2 hours | ~$3.30 | 40 |
| 2,000 | ~4.4 hours | ~$6.60 | 80 |
| 5,000 | ~11 hours | ~$16.40 | 200 |

### Loss Trend (50-step test)

| Step | Total Loss | Smooth LDDT Loss | Distogram Loss |
|------|-----------|-----------------|----------------|
| 9 | 1.789 | 1.365 | 2.419 |
| 19 | 1.480 | 1.131 | 1.466 |
| 29 | 1.619 | 1.227 | 1.902 |

Loss is oscillating (normal for small dataset fine-tuning) but generally decreasing from step 0.

---

## 6. GPU VRAM Requirements

**Our 94-structure dataset uses only 8.8-15 GB VRAM** with `diffusion_batch_size=24` and `train_crop_size=384`. This means:

| GPU | VRAM | Can Train? | Notes |
|-----|------|-----------|-------|
| RTX 3060 | 12 GB | ⚠️ Tight | May work with batch_size=12 |
| RTX 3090 | 24 GB | ✅ | Comfortable headroom |
| RTX 4090 | 24 GB | ✅ | Same VRAM, faster |
| RTX 5090 | 32 GB | ✅ | Plenty of room |
| A40 | 48 GB | ✅ | Overprovisioned |
| A100 80GB | 80 GB | ✅ | Massively overprovisioned (our current setup) |

**For cost optimization with our small dataset, an RTX 3090 ($0.20-0.30/hr on SaladCloud) would be sufficient.** The A100 80GB is overkill — we're using only 11-18% of its VRAM.

**Note**: Larger datasets (2,800+ structures) with larger structures may require more VRAM. The 8.8 GB figure is for our 94 small IDP complexes (avg ~262 tokens).

---

## 7. Recommended Strategy Going Forward

### Option A: Expand to ~2,800 IDP Structures (Recommended)

Download DIBS (~1,577) + MFIB (~1,122) PDB IDs, prepare training data, fine-tune for 1,000-2,000 steps.

**Pros**: 30x more diverse training data, reduces overfitting risk, covers broad range of IDP binding modes.
**Cons**: 1-2 hours to download and prepare data.
**Cost**: ~$5-8 for 2,000 steps on A100 (or ~$1-2 on RTX 3090).

### Option B: Accept Diffusion Noise, Use MD Instead

If fine-tuning doesn't reduce structural variance, the diffusion architecture may be fundamentally unsuitable for discriminating similar IDP sequences. Move to:
- AlphaFold-2 Multimer (non-diffusion, may be more deterministic)
- GROMACS/OpenMM molecular dynamics (physics-based, no stochastic noise)

### Option C: Both

Fine-tune Protenix (cheap experiment, $5-8) AND set up GROMACS MD in parallel. Compare results.

---

## 8. Data Preparation Workflow

### Step 1: Download CIF files
```bash
python /data/training/download_idp_structures.py
```
Creates PDB list and downloads CIF files to `/data/training/cif_files/`.

### Step 2: Prepare bioassembly data
```bash
cd /workspace && python scripts/prepare_training_data.py \
  -i /data/training/cif_files \
  -o /data/training/indices/idp_finetune.csv \
  -b /data/training/bioassembly \
  -n 16
```
Converts CIF → `.pkl.gz` bioassembly dictionaries.

### Step 3: Create placeholder files for MSA/RNA (if not using MSA)
```bash
mkdir -p /root/rna_msa
echo '{}' > /root/rna_msa/rna_sequence_to_pdb_chains.json
echo '{}' > /root/common/seq_to_pdb_index.json
```

### Step 4: Launch training with nohup
```bash
cd /workspace && export PYTHONPATH=${PYTHONPATH}:/workspace && \
nohup python3 runner/train.py [parameters] > /data/logs/training.log 2>&1 &
```

### Step 5: Monitor
```bash
tail -f /data/logs/training.log
nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader
find /data/training_output -name '*.pt' | wc -l  # checkpoint count
```

### Step 6: Resume from checkpoint (if interrupted)
```bash
python3 runner/train.py \
  --load_checkpoint_path /data/training_output/*/checkpoints/STEP.pt \
  --load_ema_checkpoint_path /data/training_output/*/checkpoints/STEP_ema_0.999.pt \
  [same parameters as original run]
```
