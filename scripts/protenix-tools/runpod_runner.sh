#!/usr/bin/env bash
# runpod_runner.sh — Fully autonomous training runner for RunPod pods.
#
# Launched by runpod_grabber.py via SSH (nohup). Runs the complete training
# pipeline without any human interaction:
#
#   Phase 0: Load credentials
#   Phase 1: Verify environment (GPU, PyTorch, Protenix)
#   Phase 2: Set up AWS CLI profile for R2
#   Phase 3: Fix eval placeholders (Protenix requires non-empty eval CSVs)
#   Phase 4: Read training plan or use defaults
#   Phase 5: Start telemetry uploader (background)
#   Phase 6: For each run in the plan:
#            a. Stage data from R2 (checkpoint, bioassemblies, indices)
#            b. Launch training with all monitors
#            c. Wait for training to complete
#            d. Upload final telemetry
#
# Usage:
#   runpod_runner.sh                          # single run from env vars
#   runpod_runner.sh --plan /data/plan.json   # chain of runs from plan
#
# Training plan JSON format:
#   [
#     {
#       "run_name": "idp_fold1_run23",
#       "checkpoint_uri": "r2://vh-protenix-training/checkpoints/...",
#       "ema_uri": "",
#       "bioassembly_uri": "r2://vh-pdb-structures/bioassembly_crop384/idp_set.zip",
#       "indices_uri": "r2://vh-protenix-training/data/idp_v2/train_fold1.csv",
#       "pdb_list_uri": "r2://vh-protenix-training/data/idp_v2/train_all_pdb_ids.txt",
#       "max_steps": 114999,
#       "seed": 23
#     }
#   ]

set -uo pipefail

LOG_DIR="${LOG_DIR:-/data}"
SCRIPTS_DIR="${SCRIPTS_DIR:-/opt/protenix-tools}"
STAGE_DIR="${STAGE_DIR:-/data/training_data}"
TELEMETRY_INTERVAL="${TELEMETRY_INTERVAL:-300}"
PLAN_FILE=""
START_TIME=$(date +%s)
POD_ID=$(hostname)

log() { echo "[runner $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
die() { log "FATAL: $*"; upload_telemetry_once "FATAL: $*"; exit 2; }

# ---- Parse args ----
while [[ $# -gt 0 ]]; do
    case $1 in
        --plan) PLAN_FILE="$2"; shift 2 ;;
        *) shift ;;
    esac
done

mkdir -p "$LOG_DIR" "$LOG_DIR/training_logs" "$LOG_DIR/training_output"

log "================================================================"
log "Protenix autonomous runner starting"
log "  pod:       $POD_ID"
log "  plan:      ${PLAN_FILE:-none (single run from env vars)}"
log "  stage_dir: $STAGE_DIR"
log "  log_dir:   $LOG_DIR"
log "================================================================"

# ---- Phase 0: Load credentials ----
log "Phase 0: Loading credentials"
if [ -f /dev/shm/secure/creds ]; then
    # shellcheck source=/dev/null
    source /dev/shm/secure/creds
    log "  R2 creds loaded (key=${CLOUDFLARE_R2_ACCESS_KEY_ID:0:4}...)"
else
    die "No credentials at /dev/shm/secure/creds — grabber should have delivered these"
fi

# ---- Phase 1: Verify environment ----
log "Phase 1: Verifying environment"
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1) || true
GPU_MEM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader 2>/dev/null | head -1) || true
log "  GPU: ${GPU_NAME:-unknown} (${GPU_MEM:-unknown})"

PYTORCH_VER=$(python3 -c "import torch; print(torch.__version__)" 2>/dev/null) || die "PyTorch not found"
CUDA_VER=$(python3 -c "import torch; print(torch.version.cuda)" 2>/dev/null) || true
log "  PyTorch: $PYTORCH_VER, CUDA: ${CUDA_VER:-none}"

python3 -c "import protenix" 2>/dev/null || die "protenix not importable — volume mount may be hiding /workspace"
log "  protenix: importable"

# ---- Phase 2: Set up AWS CLI profile for R2 ----
log "Phase 2: Configuring AWS CLI profile for R2"
mkdir -p ~/.aws
cat > ~/.aws/credentials << AWSEOF
[cloudflare-r2]
aws_access_key_id = ${CLOUDFLARE_R2_ACCESS_KEY_ID}
aws_secret_access_key = ${CLOUDFLARE_R2_SECRET_ACCESS_KEY}
AWSEOF
chmod 600 ~/.aws/credentials

# Verify R2 access
R2_TEST=$(aws s3 ls s3://vh-protenix-training/ \
    --profile cloudflare-r2 \
    --endpoint-url "$CLOUDFLARE_R2_ENDPOINT" \
    --region auto 2>&1 | head -1) || true
if [[ "$R2_TEST" == *"PRE"* ]] || [[ "$R2_TEST" == *"202"* ]]; then
    log "  R2 access verified"
else
    die "R2 access failed: $R2_TEST"
fi

# ---- Phase 3: Fix eval placeholders ----
# Protenix crashes if eval CSVs are empty even with eval disabled — it builds
# eval datasets at init regardless of eval_interval/eval_first settings.
# The eval CSV rows must pass find_eval_chain_interface filtering, so we use
# REAL rows from the training CSV (not synthetic data).
fix_eval_placeholders() {
    local bio_dir="$1"
    local train_csv="$2"
    log "  Fixing eval placeholders from $train_csv + $bio_dir"

    python3 -c "
import csv, os, sys, shutil

train_csv = '$train_csv'
bio_dir = '$bio_dir'
eval_csv_1 = '/root/indices/recentPDB_low_homology_maxtoken1536.csv'
eval_csv_2 = '/root/indices/posebusters_indices_mainchain_interface.csv'
pdb_list = '/root/indices/recentPDB_low_homology_maxtoken1024_sample384_pdb_id.txt'
bio_dict_dir = '/root/common/bioassembly_dict'

os.makedirs(bio_dict_dir, exist_ok=True)

# Read training CSV and find rows with eval_type in EvaluationChainInterface
# The 'type' column has 'interface'/'chain'; the eval filter uses 'eval_type'
EVAL_CHAIN_INTERFACE = {
    'intra_ligand', 'intra_dna', 'intra_rna', 'intra_prot',
    'ligand_prot', 'rna_prot', 'dna_prot', 'prot_prot',
    'antibody_antigen', 'antibody',
}
with open(train_csv) as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    candidates = []
    seen_pdbs = set()
    for row in reader:
        et = row.get('eval_type', '')
        pdb_id = row.get('pdb_id', '')
        if et in EVAL_CHAIN_INTERFACE and pdb_id and pdb_id not in seen_pdbs:
            bio_path = os.path.join(bio_dir, f'{pdb_id}.pkl.gz')
            if os.path.exists(bio_path):
                candidates.append((row, pdb_id, bio_path))
                seen_pdbs.add(pdb_id)
                if len(candidates) >= 3:
                    break

if not candidates:
    print('  WARNING: No valid eval candidates found in training CSV', file=sys.stderr)
    sys.exit(0)

# Write eval CSVs with real rows
pdb_ids = []
for eval_csv in [eval_csv_1, eval_csv_2]:
    with open(eval_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        w.writeheader()
        for row, pdb_id, bio_path in candidates:
            w.writerow(row)
            if pdb_id not in pdb_ids:
                pdb_ids.append(pdb_id)
                # Symlink bioassembly so eval dataset can find it
                dest = os.path.join(bio_dict_dir, f'{pdb_id}.pkl.gz')
                if not os.path.exists(dest):
                    os.symlink(bio_path, dest)

# Write PDB ID list for eval
with open(pdb_list, 'w') as f:
    for pdb_id in pdb_ids:
        f.write(pdb_id + '\n')

print(f'  Eval CSVs written with {len(candidates)} rows (eval_types pass filter): {pdb_ids}')
" 2>&1
}

# ---- Phase 4: Telemetry uploader ----
upload_telemetry_once() {
    local extra_msg="${1:-}"
    local prefix="s3://vh-protenix-training/telemetry/${POD_ID}"
    local r2_args="--profile cloudflare-r2 --endpoint-url $CLOUDFLARE_R2_ENDPOINT --region auto"

    # Upload runner log
    aws s3 cp "$LOG_DIR/runner.log" "${prefix}/runner.log" \
        $r2_args --quiet 2>/dev/null || true

    # Upload training logs
    for f in "$LOG_DIR"/training_output/*/training.log; do
        [ -f "$f" ] || continue
        local run_name
        run_name=$(basename "$(dirname "$f")")
        aws s3 cp "$f" "${prefix}/${run_name}/training.log" \
            $r2_args --quiet 2>/dev/null || true
    done

    # Upload VRAM/RAM CSVs
    for f in "$LOG_DIR"/vram_monitor.csv "$LOG_DIR"/ram_monitor.csv; do
        [ -f "$f" ] || continue
        aws s3 cp "$f" "${prefix}/$(basename "$f")" \
            $r2_args --quiet 2>/dev/null || true
    done

    # Write and upload status beacon
    python3 -c "
import json, time, glob, os
status = {
    'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'uptime_s': int(time.time() - $START_TIME),
    'pod': '$POD_ID',
    'gpu': '${GPU_NAME:-unknown}',
    'extra': '$extra_msg',
}
logs = sorted(glob.glob('$LOG_DIR/training_output/*/training.log'))
if logs:
    status['run_name'] = os.path.basename(os.path.dirname(logs[-1]))
    with open(logs[-1]) as f:
        lines = f.readlines()
    for line in reversed(lines[-100:]):
        if 'step' in line.lower() and 'loss' in line.lower():
            status['latest_log_line'] = line.strip()[:200]
            break
json.dump(status, open('/tmp/runner_status.json', 'w'), indent=2)
" 2>/dev/null || true
    [ -f /tmp/runner_status.json ] && \
        aws s3 cp /tmp/runner_status.json "${prefix}/status.json" \
            $r2_args --quiet 2>/dev/null || true
}

start_telemetry_loop() {
    while true; do
        sleep "$TELEMETRY_INTERVAL"
        upload_telemetry_once ""
    done
}

log "Phase 4: Starting telemetry uploader (interval=${TELEMETRY_INTERVAL}s)"
start_telemetry_loop &
TELEMETRY_PID=$!
trap "kill $TELEMETRY_PID 2>/dev/null || true" EXIT
log "  telemetry PID=$TELEMETRY_PID"

# ---- Phase 5: Read training plan ----
log "Phase 5: Reading training plan"

if [ -n "$PLAN_FILE" ] && [ -f "$PLAN_FILE" ]; then
    NUM_RUNS=$(python3 -c "import json; print(len(json.load(open('$PLAN_FILE'))))")
    log "  $NUM_RUNS runs from $PLAN_FILE"
else
    NUM_RUNS=0
    log "  No plan file — using env var defaults for single run"
fi

# ---- Phase 6: Execute training runs ----

execute_single_run() {
    local run_idx="$1"
    local total_runs="$2"
    local checkpoint_uri="$3"
    local ema_uri="$4"
    local bioassembly_uri="$5"
    local indices_uri="$6"
    local pdb_list_uri="$7"
    local max_steps="$8"
    local seed="$9"
    local run_name="${10}"

    log "================================================================"
    log "Run $run_idx/$total_runs: $run_name"
    log "  checkpoint: $checkpoint_uri"
    log "  bioassembly: $bioassembly_uri"
    log "  indices: $indices_uri"
    log "  max_steps: $max_steps, seed: $seed"
    log "================================================================"

    # Stage training data
    log "  Staging training data..."
    export STAGE_CHECKPOINT_URI="$checkpoint_uri"
    export STAGE_EMA_URI="$ema_uri"
    export STAGE_BIOASSEMBLY_URI="$bioassembly_uri"
    export STAGE_INDICES_URI="$indices_uri"
    export STAGE_PDB_LIST_URI="$pdb_list_uri"

    python3 "$SCRIPTS_DIR/stage_training_data.py" \
        --staging-dir "$STAGE_DIR" \
        --skip-if-staged \
        2>&1 | while IFS= read -r line; do log "  [stage] $line"; done

    # Read manifest
    local manifest="$STAGE_DIR/staging_manifest.json"
    if [ ! -f "$manifest" ]; then
        log "ERROR: staging manifest not found at $manifest"
        return 1
    fi

    local prev_ckpt prev_ema bio_dir train_csv train_pdb
    prev_ckpt=$(python3 -c "import json; m=json.load(open('$manifest')); print(m['paths'].get('checkpoint',''))")
    prev_ema=$(python3 -c "import json; m=json.load(open('$manifest')); print(m['paths'].get('ema',''))")
    bio_dir=$(python3 -c "import json; m=json.load(open('$manifest')); print(m['paths'].get('bioassembly_dir',''))")
    train_csv=$(python3 -c "import json; m=json.load(open('$manifest')); print(m['paths'].get('indices_csv',''))")
    train_pdb=$(python3 -c "import json; m=json.load(open('$manifest')); print(m['paths'].get('pdb_list',''))")

    if [ -z "$prev_ckpt" ] || [ -z "$bio_dir" ] || [ -z "$train_csv" ]; then
        log "ERROR: staging manifest missing required paths"
        return 1
    fi

    # Handle missing EMA (first run from base model)
    if [ -z "$prev_ema" ]; then
        prev_ema="$prev_ckpt"
        log "  No EMA staged — using checkpoint as EMA source"
    fi

    # Verify bioassembly count
    local bio_count
    bio_count=$(find "$bio_dir" -maxdepth 1 -name '*.pkl.gz' | wc -l)
    log "  Bioassemblies staged: $bio_count files"
    if [ "$bio_count" -eq 0 ]; then
        log "ERROR: No bioassembly files found in $bio_dir"
        return 1
    fi

    # Fix eval placeholders (only needed once, but idempotent)
    fix_eval_placeholders "$bio_dir" "$train_csv"

    # Detect base-model checkpoint (weights-only, no step/optimizer)
    local is_base_model="false"
    if python3 -c "
import torch, sys
ckpt = torch.load('$prev_ckpt', map_location='cpu', weights_only=False)
sys.exit(0 if 'step' not in ckpt else 1)
" 2>/dev/null; then
        is_base_model="true"
        log "  Detected base-model checkpoint (no step key) — using load_params_only=true"
    fi

    # Export vars for run_salad_training.sh
    export PREV_CKPT="$prev_ckpt"
    export PREV_EMA="$prev_ema"
    export BIO_DIR="$bio_dir"
    export TRAIN_CSV="$train_csv"
    export TRAIN_PDB="$train_pdb"
    export RUN_NAME="$run_name"
    export MAX_STEPS="$max_steps"
    export SEED="$seed"
    export NUM_WORKERS="${NUM_WORKERS:-2}"
    export SCRIPTS_DIR="$SCRIPTS_DIR"
    export LOG_DIR="$LOG_DIR"
    export CONTAINER_ID="$POD_ID"
    export R2_PREFIX="runpod/${POD_ID}"
    export LOAD_PARAMS_ONLY="$is_base_model"
    export SKIP_LOAD_STEP="$is_base_model"

    # Launch training with all monitors
    log "  Launching training..."
    bash "$SCRIPTS_DIR/run_salad_training.sh" 2>&1 | while IFS= read -r line; do
        log "  [train] $line"
    done

    # Training was launched in background by run_salad_training.sh.
    # Wait for the training process to finish by watching for the final
    # checkpoint or the training log to show completion.
    log "  Waiting for training to complete..."
    local train_log="$LOG_DIR/training_output/$run_name/training.log"
    local wait_start
    wait_start=$(date +%s)
    local max_wait=172800  # 48 hours max per run

    # Grace period: train.py takes 30-90s to start (data loading, model init).
    # pgrep before then would falsely conclude the process exited.
    log "  Waiting 60s for train.py to initialize..."
    sleep 60

    while true; do
        local elapsed=$(( $(date +%s) - wait_start ))
        if [ "$elapsed" -gt "$max_wait" ]; then
            log "  WARNING: Training exceeded ${max_wait}s timeout"
            break
        fi

        # Check if training process is still running
        if ! pgrep -f "runner/train.py.*--run_name $run_name" >/dev/null 2>&1; then
            # Process exited — check if it completed successfully.
            # Training log formats:
            #   Progress bar: "[step 62: 62/9999]"
            #   Metrics:      "Step 9 train metrics: {...'loss.avg':...}"
            if [ -f "$train_log" ] && grep -qiP '[Ss]tep\s+\d+\s+train\s+metrics' "$train_log" 2>/dev/null; then
                local final_step
                final_step=$(grep -oiP '[Ss]tep\s+\K\d+(?=\s+train\s+metrics)' "$train_log" 2>/dev/null | tail -1)
                log "  Training completed (final step: ${final_step:-?})"
            elif [ -f "$train_log" ] && grep -qi 'error\|traceback\|exception' "$train_log" 2>/dev/null; then
                log "  ERROR: Training crashed. Last 5 lines of log:"
                tail -5 "$train_log" 2>/dev/null | while IFS= read -r l; do log "    $l"; done
                return 1
            else
                log "  Training process exited with no step output (check log)"
                return 1
            fi
            break
        fi

        # Print progress every 5 minutes
        if [ $((elapsed % 300)) -lt 30 ]; then
            local last_step=""
            if [ -f "$train_log" ]; then
                last_step=$(grep -oP '\[step \K\d+' "$train_log" 2>/dev/null | tail -1)
            fi
            log "  Still training... elapsed=${elapsed}s step=${last_step:-?}/${max_steps}"
            upload_telemetry_once "training run $run_idx/$total_runs step=${last_step:-?}"
        fi

        sleep 30
    done

    upload_telemetry_once "run $run_idx/$total_runs completed"
    log "  Run $run_name finished (elapsed $(($(date +%s) - wait_start))s)"
}

# ---- Execute the plan ----

if [ "$NUM_RUNS" -gt 0 ]; then
    for i in $(seq 0 $((NUM_RUNS - 1))); do
        eval "$(python3 -c "
import json
plan = json.load(open('$PLAN_FILE'))
r = plan[$i]
print(f'R_CKPT={chr(34)}{r[\"checkpoint_uri\"]}{chr(34)}')
print(f'R_EMA={chr(34)}{r.get(\"ema_uri\", \"\")}{chr(34)}')
print(f'R_BIO={chr(34)}{r[\"bioassembly_uri\"]}{chr(34)}')
print(f'R_IDX={chr(34)}{r[\"indices_uri\"]}{chr(34)}')
print(f'R_PDB={chr(34)}{r.get(\"pdb_list_uri\", \"\")}{chr(34)}')
print(f'R_STEPS={r[\"max_steps\"]}')
print(f'R_SEED={r[\"seed\"]}')
print(f'R_NAME={chr(34)}{r[\"run_name\"]}{chr(34)}')
")"

        execute_single_run "$((i+1))" "$NUM_RUNS" \
            "$R_CKPT" "$R_EMA" "$R_BIO" "$R_IDX" "$R_PDB" \
            "$R_STEPS" "$R_SEED" "$R_NAME" || {
            log "ERROR: Run $((i+1)) failed, continuing to next run"
            continue
        }
    done
else
    # Single run from env vars
    execute_single_run "1" "1" \
        "${STAGE_CHECKPOINT_URI:-r2://vh-protenix-training/base_model/protenix_base_20250630_v1.0.0.pt}" \
        "${STAGE_EMA_URI:-}" \
        "${STAGE_BIOASSEMBLY_URI:-r2://vh-pdb-structures/bioassembly_crop384/idp_set.zip}" \
        "${STAGE_INDICES_URI:-r2://vh-protenix-training/data/idp_v2/train_fold1.csv}" \
        "${STAGE_PDB_LIST_URI:-r2://vh-protenix-training/data/idp_v2/train_all_pdb_ids.txt}" \
        "${MAX_STEPS:-4999}" \
        "${SEED:-71}" \
        "${RUN_NAME:-auto_${POD_ID}_$(date -u +%Y%m%dT%H%M%SZ)}" || {
        log "ERROR: Single run failed"
    }
fi

# ---- Final telemetry ----
log "================================================================"
log "All runs complete. Uploading final telemetry..."
upload_telemetry_once "ALL RUNS COMPLETE"
sleep 5
log "Runner finished at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
log "  Total elapsed: $(( $(date +%s) - START_TIME ))s"
log "================================================================"
