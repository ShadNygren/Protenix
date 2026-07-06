#!/usr/bin/env bash
# run_salad_training.sh
#
# Consolidated launcher for an instrumented Protenix training run on a Salad
# RTX 5090 container. Starts (in this order):
#   1. ram_monitor.py (1 Hz)        — system + cgroup + /dev/shm + train RSS
#   2. vram_monitor.sh (1 Hz)       — GPU VRAM
#   3. sidecar_log_mirror.sh (60s)  — rsync local logs/CSVs to R2 ops/ prefix
#   4. checkpoint_watcher.py        — uploads checkpoints + heartbeat to R2
#   5. launch_training.sh           — actual train.py run (foreground in
#                                     a setsid-detached session)
#   6. training_monitor.py          — OHLC aggregation of per-step loss metrics
#
# Every long-running process is started with setsid + nohup + disown so a
# dropped SSH connection does NOT take down the run. Stdout of each is captured
# in /data/<name>.log; sidecar mirrors those to R2 every minute.
#
# REQUIRES:
#   - /dev/shm/secure/creds exists with CLOUDFLARE_R2_* env vars
#   - /data partition writable (Salad: /workspace mapped to /data)
#   - scripts dir staged at /workspace/scripts/ (this script, ram_monitor.py,
#     checkpoint_watcher.py, vram_monitor.sh, sidecar_log_mirror.sh)
#   - training data staged: /workspace/training_data/idp_v2/{bioassembly,indices}
#   - Protenix repo at /workspace (cd /workspace && python3 runner/train.py works)

set -u

# ---------- defaults (override via env or args) ----------
RUN_NAME="${RUN_NAME:-instrumented_v22_$(date -u +%Y%m%dT%H%M%SZ)}"
PREV_CKPT="${PREV_CKPT:-/workspace/training_output/_seed/14998.pt}"
PREV_EMA="${PREV_EMA:-/workspace/training_output/_seed/14998_ema_0.999.pt}"
MAX_STEPS="${MAX_STEPS:-5000}"
SEED="${SEED:-0}"  # use select_next_training_run.py to determine correct seed
NUM_WORKERS="${NUM_WORKERS:-2}"
BIO_DIR="${BIO_DIR:-/workspace/training_data/idp_v2/bioassembly}"
TRAIN_CSV="${TRAIN_CSV:-/workspace/training_data/idp_v2/indices/train_fold1.csv}"
TRAIN_PDB="${TRAIN_PDB:-/workspace/training_data/idp_v2/indices/train_all_pdb_ids.txt}"
CONTAINER_ID="${CONTAINER_ID:-$(hostname)}"
R2_PREFIX="${R2_PREFIX:-uhrf1_stella_20260519}"
SCRIPTS_DIR="${SCRIPTS_DIR:-/workspace/scripts}"
LOG_DIR="${LOG_DIR:-/data}"

OHLC_CSV="$LOG_DIR/training_ohlc.csv"

mkdir -p "$LOG_DIR" "$LOG_DIR/training_logs" "$LOG_DIR/training_output"

# Source creds (R2 + DEK)
if [ ! -r /dev/shm/secure/creds ]; then
    echo "[run] /dev/shm/secure/creds missing — deliver via SSH heredoc first" >&2
    exit 2
fi
# shellcheck source=/dev/null
source /dev/shm/secure/creds

# Sanity checks
for f in "$PREV_CKPT" "$PREV_EMA" "$BIO_DIR" "$TRAIN_CSV" "$TRAIN_PDB"; do
    if [ ! -e "$f" ]; then
        echo "[run] required path missing: $f" >&2
        exit 2
    fi
done
for s in ram_monitor.py vram_monitor.sh checkpoint_watcher.py sidecar_log_mirror.sh launch_training.sh training_monitor.py; do
    if [ ! -f "$SCRIPTS_DIR/$s" ]; then
        echo "[run] required script missing: $SCRIPTS_DIR/$s" >&2
        exit 2
    fi
done

echo "================================================================"
echo "[run] starting instrumented training"
echo "  run_name:  $RUN_NAME"
echo "  resume:    $PREV_CKPT (step $(basename "$PREV_CKPT" .pt))"
echo "  max_steps: $MAX_STEPS"
echo "  seed:      $SEED"
echo "  workers:   $NUM_WORKERS"
echo "  bio_dir:   $BIO_DIR ($(find "$BIO_DIR" -maxdepth 1 -name '*.pkl.gz' | wc -l) files)"
echo "  r2_prefix: $R2_PREFIX"
echo "================================================================"

# 1. ram_monitor.py — 1 Hz system + cgroup + /dev/shm + train RSS
RAM_CSV="$LOG_DIR/ram_monitor.csv"
echo "[run] starting ram_monitor → $RAM_CSV"
setsid nohup python3 "$SCRIPTS_DIR/ram_monitor.py" \
    --out "$RAM_CSV" \
    --interval 1 \
    --pattern 'runner/train\.py|checkpoint_watcher\.py|sidecar_log_mirror|ram_monitor|vram_monitor' \
    </dev/null >"$LOG_DIR/ram_monitor.stdout" 2>&1 &
RAM_PID=$!
disown $RAM_PID 2>/dev/null || true
echo "[run]   pid=$RAM_PID"

# 2. vram_monitor.sh — 1 Hz GPU VRAM
VRAM_CSV="$LOG_DIR/vram_monitor.csv"
echo "[run] starting vram_monitor → $VRAM_CSV"
setsid nohup bash "$SCRIPTS_DIR/vram_monitor.sh" "$VRAM_CSV" 1 \
    </dev/null >"$LOG_DIR/vram_monitor.stdout" 2>&1 &
VRAM_PID=$!
disown $VRAM_PID 2>/dev/null || true
echo "[run]   pid=$VRAM_PID"

# 3. sidecar log mirror — every 60s rsync logs + CSVs to R2 ops/ prefix
echo "[run] starting sidecar_log_mirror → s3://vh-protenix-training/ops/$R2_PREFIX/"
setsid nohup bash "$SCRIPTS_DIR/sidecar_log_mirror.sh" \
    --prefix "$R2_PREFIX" \
    --interval 60 \
    --add "$LOG_DIR/training_output/$RUN_NAME/training.log" \
    --add "$LOG_DIR/training_logs/${RUN_NAME}_training.log" \
    --add "$RAM_CSV" \
    --add "$VRAM_CSV" \
    --add "$LOG_DIR/checkpoint_watcher.log" \
    --add "$LOG_DIR/ram_monitor.stdout" \
    --add "$LOG_DIR/vram_monitor.stdout" \
    --add "$OHLC_CSV" \
    --add "$LOG_DIR/training_monitor.stdout" \
    </dev/null >"$LOG_DIR/sidecar.log" 2>&1 &
SIDECAR_PID=$!
disown $SIDECAR_PID 2>/dev/null || true
echo "[run]   pid=$SIDECAR_PID"

# 4. checkpoint_watcher — uploads each .pt + EMA pair to R2 immediately,
#    writes heartbeat to R2 ops/<prefix>/heartbeat.json each poll cycle (30s)
echo "[run] starting checkpoint_watcher (heartbeat + prefix-override)"
setsid nohup python3 "$SCRIPTS_DIR/checkpoint_watcher.py" \
    --env-file /dev/shm/secure/creds \
    --runs-root "$LOG_DIR/training_output" \
    --state-file "$LOG_DIR/checkpoint_watcher_state.json" \
    --poll-interval 30 \
    --prefix-override "$R2_PREFIX" \
    --heartbeat \
    </dev/null >"$LOG_DIR/checkpoint_watcher.log" 2>&1 &
WATCHER_PID=$!
disown $WATCHER_PID 2>/dev/null || true
echo "[run]   pid=$WATCHER_PID"

# Brief pause for the monitors to start writing data before training jams the loop
sleep 2

# 5. launch the training itself, in its own setsid session so SSH disconnect
#    doesn't kill it. Pass workers via env so launch_training.sh can read it.
echo "[run] starting training (this is the foreground process)"
echo "[run]   log: $LOG_DIR/launch_training_wrapper.log"
export NUM_DL_WORKERS="$NUM_WORKERS"
setsid nohup bash "$SCRIPTS_DIR/launch_training.sh" \
    "$RUN_NAME" \
    "$PREV_CKPT" \
    "$PREV_EMA" \
    "$MAX_STEPS" \
    "$SEED" \
    "$BIO_DIR" \
    "$TRAIN_CSV" \
    "$TRAIN_PDB" \
    </dev/null >"$LOG_DIR/launch_training_wrapper.log" 2>&1 &
TRAIN_PID=$!
disown $TRAIN_PID 2>/dev/null || true
echo "[run]   pid=$TRAIN_PID"

# 6. training_monitor.py — OHLC aggregation of per-step loss from wrapper log
#    train.py stdout goes to launch_training_wrapper.log (not training.log inside
#    the Protenix-created run dir, which gets a second timestamp appended to its name)
TRAINING_LOG="$LOG_DIR/launch_training_wrapper.log"
echo "[run] starting training_monitor → $OHLC_CSV"
echo "[run]   watching: $TRAINING_LOG"
setsid nohup python3 "$SCRIPTS_DIR/training_monitor.py" \
    --log "$TRAINING_LOG" \
    --out "$OHLC_CSV" \
    --poll-interval 5 \
    </dev/null >"$LOG_DIR/training_monitor.stdout" 2>&1 &
MONITOR_PID=$!
disown $MONITOR_PID 2>/dev/null || true
echo "[run]   pid=$MONITOR_PID"

# Write a manifest so post-mortem can find every PID we started
cat > "$LOG_DIR/run_manifest.json" <<EOF
{
  "started_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "run_name": "$RUN_NAME",
  "container_id": "$CONTAINER_ID",
  "r2_prefix": "$R2_PREFIX",
  "pids": {
    "ram_monitor": $RAM_PID,
    "vram_monitor": $VRAM_PID,
    "sidecar_log_mirror": $SIDECAR_PID,
    "checkpoint_watcher": $WATCHER_PID,
    "training_wrapper": $TRAIN_PID,
    "training_monitor": $MONITOR_PID
  },
  "config": {
    "max_steps": $MAX_STEPS,
    "seed": $SEED,
    "num_dl_workers": $NUM_WORKERS,
    "prev_ckpt": "$PREV_CKPT",
    "bio_dir": "$BIO_DIR",
    "train_csv": "$TRAIN_CSV"
  }
}
EOF
echo "[run] manifest: $LOG_DIR/run_manifest.json"

echo "================================================================"
echo "[run] all 6 processes launched. Watch with:"
echo "  tail -f $LOG_DIR/launch_training_wrapper.log"
echo "  tail -f $LOG_DIR/checkpoint_watcher.log"
echo "  tail -f $OHLC_CSV"
echo "  tail -f $RAM_CSV"
echo "  ps -ef | grep -E 'train|watcher|monitor|sidecar'"
echo "[run] R2 heartbeat: s3://vh-protenix-training/ops/$R2_PREFIX/heartbeat.json"
echo "================================================================"
