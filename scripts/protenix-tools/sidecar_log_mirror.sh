#!/usr/bin/env bash
# sidecar_log_mirror.sh
#
# Solve the visibility hole that hid the 2026-05-14 03:38 UTC training crash:
# Salad's "Container Logs" only captures PID-1 (entrypoint) stdout. Anything
# launched over SSH writes to the SSH session's tty, not entrypoint stdout, so
# Salad portal sees nothing useful once the entrypoint banner has printed.
#
# This sidecar mirrors local log files + monitor CSVs to R2 every N seconds so
# that even if the container dies abruptly, the LAST minute of every log is
# already in R2 and available for post-mortem.
#
# Run as a daemon under setsid+nohup+disown alongside train.py:
#   setsid nohup bash /opt/protenix-tools/sidecar_log_mirror.sh \
#       --prefix salad_testing/<container-id> \
#       --interval 60 \
#       --add /data/training.log \
#       --add /data/ram_monitor.csv \
#       --add /data/vram_monitor.csv \
#       --add /data/checkpoint_watcher.log \
#       </dev/null >/data/sidecar.log 2>&1 &
#   disown
#
# Files that don't exist yet are simply skipped each cycle (no error). Files
# that grow are re-uploaded each cycle — R2 cost is trivial for log-sized
# objects (<10 MB).

set -u

BUCKET="${SIDECAR_BUCKET:-vh-protenix-training}"
INTERVAL=60
PREFIX=""
FILES=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)   PREFIX="$2"; shift 2 ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        --add)      FILES+=("$2"); shift 2 ;;
        --bucket)   BUCKET="$2"; shift 2 ;;
        *)          echo "[sidecar] unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$PREFIX" ]; then
    echo "[sidecar] --prefix is required (e.g., salad_testing/<container-id>)" >&2
    exit 2
fi
if [ "${#FILES[@]}" -eq 0 ]; then
    echo "[sidecar] no --add files specified; nothing to mirror" >&2
    exit 2
fi

# Use a python helper that talks to R2 via boto3 — no AWS CLI profile needed,
# just CLOUDFLARE_R2_* env vars (which the entrypoint sources from
# /dev/shm/secure/creds).
upload() {
    local src="$1"
    local key="$2"
    if [ ! -f "$src" ]; then
        return 0
    fi
    python3 - "$src" "$BUCKET" "$key" <<'PYEOF'
import os, sys, boto3
from botocore.config import Config
src, bucket, key = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["CLOUDFLARE_R2_ENDPOINT"],
        aws_access_key_id=os.environ["CLOUDFLARE_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CLOUDFLARE_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    s3.upload_file(src, bucket, key)
except Exception as e:
    print(f"[sidecar] upload {src} -> {key} FAILED: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
}

echo "[sidecar] starting"
echo "[sidecar]   bucket:   $BUCKET"
echo "[sidecar]   prefix:   ops/$PREFIX/"
echo "[sidecar]   interval: ${INTERVAL}s"
echo "[sidecar]   files:    ${FILES[*]}"
echo "[sidecar]   started:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Write a "sidecar started" marker so the operator can confirm it actually ran
START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
START_HOSTNAME="$(hostname)"
START_PID="$$"
{
    printf '{"started_at":"%s","hostname":"%s","pid":%s,"interval_s":%s,"files":[' \
        "$START_TS" "$START_HOSTNAME" "$START_PID" "$INTERVAL"
    sep=""
    for f in "${FILES[@]}"; do
        printf '%s"%s"' "$sep" "$f"
        sep=","
    done
    printf ']}\n'
} > /tmp/sidecar_started.json
upload /tmp/sidecar_started.json "ops/$PREFIX/sidecar_started.json"

while true; do
    CYCLE_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    for f in "${FILES[@]}"; do
        # Mirror to a stable name so consumers can poll one URL and always get
        # the latest contents (no timestamp in the key — newest overwrites).
        bn="$(basename "$f")"
        upload "$f" "ops/$PREFIX/$bn"
    done
    # Cycle marker — separate small object so post-mortem can see "sidecar
    # was alive at T even if individual mirror uploads failed."
    echo "{\"t\":\"$CYCLE_TS\",\"pid\":$$}" > /tmp/sidecar_alive.json
    upload /tmp/sidecar_alive.json "ops/$PREFIX/sidecar_alive.json"
    sleep "$INTERVAL"
done
