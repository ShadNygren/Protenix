#!/usr/bin/env bash
# deploy_and_restart.sh — Deploy training harness scripts AND restart processes.
#
# This script enforces the invariant that deployment = code running in processes.
# Deploying without restarting is forbidden. A fix on disk that isn't in memory
# provides zero protection.
#
# Usage (from laptop):
#   ./deploy_and_restart.sh <pod_host> <pod_port> [--dry-run]
#
# What it does:
#   1. Copies all training scripts to the pod
#   2. Verifies deployed files match local files (sha256)
#   3. Stops chain_runner (which stops training + companions)
#   4. Restarts chain_runner (loads new code, resumes from latest boundary)
#   5. Verifies all processes are running new code
#
# The training run in progress WILL be interrupted. This is intentional.
# Lost compute from a restart is always less than the cost of running broken code.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SSH_KEY="$HOME/.ssh/id_ed25519"
REMOTE_SCRIPTS="/data/scripts"
REMOTE_SELECT="/data/scripts/select_next_training_run.py"

# Scripts to deploy (order matters: dependencies first)
SCRIPTS=(
    "select_next_training_run.py"
    "audit_state_continuity.py"
    "audit_ema_continuity.py"
    "audit_checkpoint_state.py"
    "checkpoint_watcher.py"
    "training_monitor.py"
    "sidecar_log_mirror.sh"
    "chain_runner.py"
)

# Also deploy from the UHRF1 repo
UHRF1_SCRIPTS_DIR="$SCRIPT_DIR/../../../UHRF1_inhibition_by_STELLA_for_cancer_therapy/scripts"

die() { echo "FATAL: $*" >&2; exit 1; }
info() { echo "[deploy] $*"; }
warn() { echo "[deploy] WARNING: $*" >&2; }

# Parse args
POD_HOST="${1:-}"
POD_PORT="${2:-}"
DRY_RUN=false
if [[ "${3:-}" == "--dry-run" ]]; then DRY_RUN=true; fi

if [[ -z "$POD_HOST" || -z "$POD_PORT" ]]; then
    echo "Usage: $0 <pod_host> <pod_port> [--dry-run]"
    echo "Example: $0 82.221.170.242 26725"
    exit 1
fi

SSH_CMD="ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no -p $POD_PORT -i $SSH_KEY root@$POD_HOST"
SCP_CMD="scp -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no -P $POD_PORT -i $SSH_KEY"

# Verify SSH connectivity
info "Testing SSH connection to $POD_HOST:$POD_PORT..."
$SSH_CMD 'echo ok' >/dev/null 2>&1 || die "Cannot SSH to pod"
info "SSH OK"

# Step 1: Deploy all scripts
info "=== STEP 1: Deploy scripts ==="
for script in "${SCRIPTS[@]}"; do
    local_path="$SCRIPT_DIR/$script"
    if [[ ! -f "$local_path" ]]; then
        # Try UHRF1 scripts dir
        local_path="$UHRF1_SCRIPTS_DIR/$script"
    fi
    if [[ ! -f "$local_path" ]]; then
        warn "Script not found: $script (checked protenix-tools/ and UHRF1/scripts/)"
        continue
    fi
    local_sha=$(sha256sum "$local_path" | cut -d' ' -f1)
    info "  $script (sha256: ${local_sha:0:16}...)"
    if [[ "$DRY_RUN" == true ]]; then
        info "  [dry-run] would scp $local_path -> $REMOTE_SCRIPTS/$script"
    else
        $SCP_CMD "$local_path" "root@$POD_HOST:$REMOTE_SCRIPTS/$script"
    fi
done

# Deploy select_next_training_run.py from UHRF1 repo
SELECT_LOCAL="$UHRF1_SCRIPTS_DIR/select_next_training_run.py"
if [[ -f "$SELECT_LOCAL" ]]; then
    local_sha=$(sha256sum "$SELECT_LOCAL" | cut -d' ' -f1)
    info "  select_next_training_run.py (sha256: ${local_sha:0:16}...)"
    if [[ "$DRY_RUN" != true ]]; then
        $SCP_CMD "$SELECT_LOCAL" "root@$POD_HOST:$REMOTE_SELECT"
    fi
fi

# Step 2: Verify deployed files match local
info "=== STEP 2: Verify deployed versions ==="
MISMATCH=0
for script in "${SCRIPTS[@]}"; do
    local_path="$SCRIPT_DIR/$script"
    if [[ ! -f "$local_path" ]]; then
        local_path="$UHRF1_SCRIPTS_DIR/$script"
    fi
    [[ ! -f "$local_path" ]] && continue
    local_sha=$(sha256sum "$local_path" | cut -d' ' -f1)
    remote_sha=$($SSH_CMD "sha256sum $REMOTE_SCRIPTS/$script 2>/dev/null | cut -d' ' -f1" 2>/dev/null || echo "MISSING")
    if [[ "$local_sha" != "$remote_sha" ]]; then
        warn "MISMATCH: $script (local=$local_sha, remote=$remote_sha)"
        MISMATCH=1
    else
        info "  ✓ $script matches"
    fi
done

if [[ "$SELECT_LOCAL" && -f "$SELECT_LOCAL" ]]; then
    local_sha=$(sha256sum "$SELECT_LOCAL" | cut -d' ' -f1)
    remote_sha=$($SSH_CMD "sha256sum $REMOTE_SELECT 2>/dev/null | cut -d' ' -f1" 2>/dev/null || echo "MISSING")
    if [[ "$local_sha" != "$remote_sha" ]]; then
        warn "MISMATCH: select_next_training_run.py"
        MISMATCH=1
    else
        info "  ✓ select_next_training_run.py matches"
    fi
fi

if [[ $MISMATCH -eq 1 ]]; then
    die "Version mismatch after deploy — aborting restart"
fi

if [[ "$DRY_RUN" == true ]]; then
    info "[dry-run] Would now stop chain_runner and restart. Exiting."
    exit 0
fi

# Step 3: Stop chain_runner (which will stop training + companions via exit handling)
info "=== STEP 3: Stop chain_runner ==="
# Find the PYTHON chain_runner process — NOT the bash wrapper (which has
# the same string in its command line: `bash -c 'source ... && python3 -u
# chain_runner.py ...'`). Signalling the bash wrapper leaves the python
# child orphaned (reparented to init), not killed. The bug:
# `pgrep -f "python3.*chain_runner.py" | head -1` returned the bash PID
# first because both PIDs matched the pattern.
#
# Fix: use `pgrep -af` to get "PID full_command_line", then filter for
# lines whose first command token is `python3`/`python` (not `bash`).
CHAIN_PID=$($SSH_CMD 'pgrep -af "chain_runner\.py" | awk "\$2 ~ /^python[0-9.]*$/ {print \$1; exit}"' 2>/dev/null || echo "")
if [[ -n "$CHAIN_PID" ]]; then
    info "  Sending SIGINT to chain_runner PYTHON PID $CHAIN_PID"
    # SIGINT triggers the KeyboardInterrupt handler that gracefully
    # stops training, companions, and releases lock.
    $SSH_CMD "kill -INT $CHAIN_PID" 2>/dev/null || true
    info "  Waiting up to 60s for graceful shutdown..."
    for i in $(seq 1 60); do
        if ! $SSH_CMD "kill -0 $CHAIN_PID 2>/dev/null" 2>/dev/null; then
            info "  chain_runner stopped after ${i}s"
            break
        fi
        sleep 1
    done
    # Check if still alive
    if $SSH_CMD "kill -0 $CHAIN_PID 2>/dev/null" 2>/dev/null; then
        warn "chain_runner didn't stop gracefully, sending SIGKILL"
        $SSH_CMD "kill -9 $CHAIN_PID" 2>/dev/null || true
        sleep 2
    fi
    # Also clean up the bash wrapper if still around (no-op if python
    # child already exited and bash followed).
    $SSH_CMD 'pgrep -af "chain_runner\.py" | awk "\$2 ~ /^bash$/ {print \$1}" | xargs -r kill -9' 2>/dev/null || true
else
    info "  No running chain_runner python process found"
fi

# Stale lock cleanup — chain_runner.py refuses to start if a lock file
# exists, but SIGKILL leaves the lock behind. Empty lock files from a
# previous failed-startup are also a problem. Safe to remove here only
# because we've confirmed no chain_runner process is alive above.
LOCK_HOLDER=$($SSH_CMD 'pgrep -af "chain_runner\.py" | awk "\$2 ~ /^python[0-9.]*$/" | head -1' 2>/dev/null || echo "")
if [[ -z "$LOCK_HOLDER" ]]; then
    $SSH_CMD 'rm -f /data/chain_runner.lock' 2>/dev/null || true
    info "  Cleared any stale lock file"
fi

# Kill any orphaned training processes
TRAIN_PIDS=$($SSH_CMD 'pgrep -f "runner/train.py" 2>/dev/null' || echo "")
if [[ -n "$TRAIN_PIDS" ]]; then
    info "  Killing orphaned training processes: $TRAIN_PIDS"
    $SSH_CMD "kill -9 $TRAIN_PIDS" 2>/dev/null || true
    sleep 2
fi

# Kill any orphaned companions
for companion in checkpoint_watcher training_monitor sidecar_log_mirror; do
    COMP_PID=$($SSH_CMD "pgrep -f '$companion' 2>/dev/null" || echo "")
    if [[ -n "$COMP_PID" ]]; then
        info "  Killing orphaned $companion PID $COMP_PID"
        $SSH_CMD "kill $COMP_PID" 2>/dev/null || true
    fi
done
sleep 3

# Step 4: Restart chain_runner
info "=== STEP 4: Restart chain_runner ==="
info "  chain_runner will auto-detect progress from checkpoints and resume"
$SSH_CMD 'source /dev/shm/secure/creds && cd /data/scripts && nohup python3 -u chain_runner.py > /data/chain_runner.stdout 2>&1 &'
# Give chain_runner enough time to clear startup (read lock, print version
# banner, decide on resume strategy). If we check too early we may catch
# it before a FATAL exit (e.g. stale lock) and falsely report success.
sleep 15

# Step 5: Verify new processes
info "=== STEP 5: Verify new processes ==="
# Target the PYTHON chain_runner explicitly, NOT the bash wrapper.
# Old code: `pgrep -f "python3.*chain_runner.py" | head -1` returned the
# bash wrapper PID first — which is always alive briefly even when the
# python child has FATAL-exited (e.g. lock file collision) — leading to
# false-positive "✓ running" reports.
NEW_CHAIN_PID=$($SSH_CMD 'pgrep -af "chain_runner\.py" | awk "\$2 ~ /^python[0-9.]*$/ {print \$1; exit}"' 2>/dev/null || echo "")
if [[ -z "$NEW_CHAIN_PID" ]]; then
    # Try to surface the cause from chain_runner.stdout — most common
    # causes are stale lock file or version-banner sha mismatch.
    LAST_LOG=$($SSH_CMD "strings /data/chain_runner.stdout 2>/dev/null | tail -10" 2>/dev/null || echo "")
    warn "chain_runner python process not found after 15s. Last stdout:"
    echo "$LAST_LOG" >&2
    die "chain_runner did not start (or exited immediately)!"
fi
info "  ✓ chain_runner PYTHON running (PID $NEW_CHAIN_PID)"

# Wait a bit for companions to start
sleep 10
WATCHER_PID=$($SSH_CMD 'pgrep -af checkpoint_watcher | awk "\$2 ~ /^python[0-9.]*$/ && /-u? \/data/ {print \$1; exit}" | head -1' 2>/dev/null || echo "")
[[ -z "$WATCHER_PID" ]] && WATCHER_PID=$($SSH_CMD 'pgrep -f checkpoint_watcher | head -1' 2>/dev/null || echo "")
MONITOR_PID=$($SSH_CMD 'pgrep -f training_monitor | head -1' 2>/dev/null || echo "")

[[ -n "$WATCHER_PID" ]] && info "  ✓ checkpoint_watcher running (PID $WATCHER_PID)" || warn "checkpoint_watcher not yet started"
[[ -n "$MONITOR_PID" ]] && info "  ✓ training_monitor running (PID $MONITOR_PID)" || warn "training_monitor not yet started (may be staging data)"

info ""
info "=== DEPLOYMENT COMPLETE ==="
info "All scripts deployed and processes restarted."
info "chain_runner will resume from the latest boundary checkpoint."
info ""
info "Check progress: ssh root@$POD_HOST -p $POD_PORT -i $SSH_KEY 'tail -20 /data/chain_runner.stdout'"
