#!/bin/bash
# Robust training launcher for RTX 5090 (32GB VRAM).
# Adapted from A100 launch_training.sh with reduced batch sizes.
#
# Usage: launch_training_5090.sh <run_name> <prev_ckpt> <prev_ema> <max_steps> \
#                                <seed> <bio_dir> <train_csv> <train_pdb_list>

set -e

RUN_NAME="${1:?run_name required}"
PREV_CKPT="${2:?prev_ckpt required}"
PREV_EMA="${3:?prev_ema required}"
MAX_STEPS="${4:?max_steps required}"
SEED="${5:?seed required}"
BIO_DIR="${6:?bio_dir required}"
TRAIN_CSV="${7:?train_csv required}"
TRAIN_PDB="${8:?train_pdb_list required}"

NUM_DL_WORKERS="${NUM_DL_WORKERS:-2}"
LOAD_PARAMS_ONLY="${LOAD_PARAMS_ONLY:-false}"
SKIP_LOAD_STEP="${SKIP_LOAD_STEP:-false}"

OUTPUT_DIR="/data/training_output/${RUN_NAME}"
mkdir -p "$OUTPUT_DIR"
LOG_PRIMARY="${OUTPUT_DIR}/training.log"
LOG_BACKUP="/data/training_logs/${RUN_NAME}_training.log"
mkdir -p /data/training_logs

echo "=== ${RUN_NAME} ===" | tee "$LOG_PRIMARY" "$LOG_BACKUP"
echo "Resume: $PREV_CKPT" | tee -a "$LOG_PRIMARY" "$LOG_BACKUP"
echo "Steps: $MAX_STEPS | Seed: $SEED" | tee -a "$LOG_PRIMARY" "$LOG_BACKUP"
echo "Workers: $NUM_DL_WORKERS" | tee -a "$LOG_PRIMARY" "$LOG_BACKUP"
echo "Start: $(date)" | tee -a "$LOG_PRIMARY" "$LOG_BACKUP"
echo "" | tee -a "$LOG_PRIMARY" "$LOG_BACKUP"

cd /workspace && \
    stdbuf -oL -eL python3 -u runner/train.py \
        --run_name "$RUN_NAME" \
        --base_dir /data/training_output \
        --load_checkpoint_path "$PREV_CKPT" \
        --load_ema_checkpoint_path "$PREV_EMA" \
        --load_params_only "$LOAD_PARAMS_ONLY" \
        --skip_load_optimizer true \
        --skip_load_scheduler true \
        --skip_load_step "$SKIP_LOAD_STEP" \
        --max_steps "$MAX_STEPS" \
        --checkpoint_interval 500 \
        --log_interval 10 \
        --lr 0.0009 \
        --lr_scheduler cosine_annealing \
        --warmup_steps 200 \
        --finetune.lr 0.0009 \
        --finetune.lr_scheduler cosine_annealing \
        --finetune.warmup_steps 200 \
        --finetune.max_steps "$MAX_STEPS" \
        --finetune.min_lr_ratio 0.1 \
        --finetune.decay_every_n_steps 50000 \
        --data.weightedPDB_before2109_wopb_nometalc_0925.base_info.bioassembly_dict_dir "$BIO_DIR" \
        --data.weightedPDB_before2109_wopb_nometalc_0925.base_info.indices_fpath "$TRAIN_CSV" \
        --data.weightedPDB_before2109_wopb_nometalc_0925.base_info.pdb_list "$TRAIN_PDB" \
        --data.weightedPDB_before2109_wopb_nometalc_0925.base_info.random_sample_if_failed true \
        --data.weightedPDB_before2109_wopb_nometalc_0925.base_info.use_reference_chains_only false \
        --data.weightedPDB_before2109_wopb_nometalc_0925.cropping_configs.crop_size 384 \
        --data.num_dl_workers "$NUM_DL_WORKERS" \
        --data.epoch_size 10000 \
        --data.msa.enable_prot_msa false \
        --data.msa.enable_rna_msa false \
        --data.template.enable_prot_template false \
        --diffusion_batch_size 4 \
        --diffusion_chunk_size 2 \
        --train_crop_size 384 \
        --dtype bf16 \
        --ema_decay 0.999 \
        --seed "$SEED" \
        --use_wandb false \
        --eval_interval 9999 \
        --eval_first false \
    2>&1 | tee --output-error=warn -a "$LOG_PRIMARY" "$LOG_BACKUP"

sync
echo "" | tee -a "$LOG_PRIMARY" "$LOG_BACKUP"
echo "=== END ${RUN_NAME} at $(date) ===" | tee -a "$LOG_PRIMARY" "$LOG_BACKUP"
sync
