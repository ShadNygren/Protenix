#!/usr/bin/env bash
# auto_stage_and_train.sh
#
# Unified startup script: stages data from R2, then launches instrumented training.
# Designed to run as PROTENIX_STARTUP_SCRIPT in docker-entrypoint.sh.
#
# All configuration via env vars (set in RunPod/Salad template):
#
#   STAGE_CHECKPOINT_URI   r2://vh-protenix-training/base_model/protenix_base_20250630_v1.0.0.pt
#   STAGE_EMA_URI          (optional, empty for first run from base model)
#   STAGE_BIOASSEMBLY_URI  r2://vh-pdb-structures/bioassembly_crop384/idp_set.zip
#   STAGE_INDICES_URI      r2://vh-protenix-training/data/idp_v2/train_fold1.csv
#   STAGE_PDB_LIST_URI     r2://vh-protenix-training/data/idp_v2/train_all_pdb_ids.txt
#   STAGE_DIR              /workspace/training_data (default)
#
#   RUN_NAME               (auto-generated if unset)
#   MAX_STEPS              (required)
#   SEED                   (required)
#   NUM_WORKERS            2 (default)
#
# URI schemes:
#   r2://bucket/key   → Cloudflare R2 (uses CLOUDFLARE_R2_* creds)
#   s3://bucket/key   → AWS S3 (uses default boto3 creds)
#   file:///path      → local file
#
# Credentials: delivered to /dev/shm/secure/creds via SSH before container start,
# or set as env vars (CLOUDFLARE_R2_ACCESS_KEY_ID, etc.)

set -euo pipefail

SCRIPTS_DIR="${SCRIPTS_DIR:-/opt/protenix-tools}"
STAGE_DIR="${STAGE_DIR:-/workspace/training_data}"
LOG_DIR="${LOG_DIR:-/data}"

echo "================================================================"
echo "[auto] Protenix auto-stage-and-train"
echo "  started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "================================================================"

# Source creds if available (entrypoint may have already done this, but be safe)
if [ -r /dev/shm/secure/creds ]; then
    # shellcheck source=/dev/null
    source /dev/shm/secure/creds
fi

# ---- Phase 1: Stage training data from R2 ----

echo ""
echo "[auto] Phase 1: staging training data from R2"
python3 "$SCRIPTS_DIR/stage_training_data.py" \
    --staging-dir "$STAGE_DIR" \
    --skip-if-staged

# Read manifest to get staged paths
MANIFEST="$STAGE_DIR/staging_manifest.json"
if [ ! -f "$MANIFEST" ]; then
    echo "[auto] FATAL: staging manifest not found at $MANIFEST" >&2
    exit 2
fi

PREV_CKPT=$(python3 -c "import json; m=json.load(open('$MANIFEST')); print(m['paths'].get('checkpoint',''))")
PREV_EMA=$(python3 -c "import json; m=json.load(open('$MANIFEST')); print(m['paths'].get('ema',''))")
BIO_DIR=$(python3 -c "import json; m=json.load(open('$MANIFEST')); print(m['paths'].get('bioassembly_dir',''))")
TRAIN_CSV=$(python3 -c "import json; m=json.load(open('$MANIFEST')); print(m['paths'].get('indices_csv',''))")
TRAIN_PDB=$(python3 -c "import json; m=json.load(open('$MANIFEST')); print(m['paths'].get('pdb_list',''))")

if [ -z "$PREV_CKPT" ] || [ -z "$BIO_DIR" ] || [ -z "$TRAIN_CSV" ]; then
    echo "[auto] FATAL: staging manifest missing required paths" >&2
    echo "  checkpoint=$PREV_CKPT bio_dir=$BIO_DIR indices=$TRAIN_CSV" >&2
    exit 2
fi

# For the first run from base model, there's no EMA — launch_training.sh
# requires PREV_EMA to be a valid path, so point it to the checkpoint itself
# (Protenix will initialize EMA from the loaded model state).
if [ -z "$PREV_EMA" ]; then
    PREV_EMA="$PREV_CKPT"
    echo "[auto] no EMA checkpoint staged — using checkpoint as EMA source"
fi

# ---- Phase 2: Configure and launch training ----

echo ""
echo "[auto] Phase 2: launching instrumented training"

# Export paths for run_salad_training.sh
export PREV_CKPT PREV_EMA BIO_DIR TRAIN_CSV TRAIN_PDB
export RUN_NAME="${RUN_NAME:-auto_$(date -u +%Y%m%dT%H%M%SZ)}"
export MAX_STEPS="${MAX_STEPS:?MAX_STEPS env var is required}"
export SEED="${SEED:?SEED env var is required}"
export NUM_WORKERS="${NUM_WORKERS:-2}"
export SCRIPTS_DIR="${SCRIPTS_DIR}"
export LOG_DIR="${LOG_DIR}"

echo "  run_name:    $RUN_NAME"
echo "  checkpoint:  $PREV_CKPT"
echo "  ema:         $PREV_EMA"
echo "  max_steps:   $MAX_STEPS"
echo "  seed:        $SEED"
echo "  bio_dir:     $BIO_DIR"
echo "  indices:     $TRAIN_CSV"

bash "$SCRIPTS_DIR/run_salad_training.sh"

echo ""
echo "[auto] training launched — follow with:"
echo "  tail -f $LOG_DIR/training_output/$RUN_NAME/training.log"
echo "================================================================"
