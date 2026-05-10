#!/bin/bash
# Persistent 1-second VRAM monitor for the GPU.
# Writes to /data/vram_monitor_persistent.csv with rotation (one file per day).
#
# Usage:
#   nohup bash /data/scripts/vram_monitor.sh > /data/vram_monitor.log 2>&1 &
#
# Survives any specific training run; intended to accumulate samples across
# the entire pod lifetime. Restart with: pkill -f vram_monitor.sh
#
# Per CLAUDE.md "GPU VRAM Monitoring" rule: need 100K+ samples (24+ hours
# at 1-sec polling) before claiming a GPU's peak VRAM is sufficient.
#
# CSV columns: timestamp_iso, vram_used_mib, vram_total_mib, gpu_util_pct,
#              gpu_temp_c, power_w

set -u

OUT_DIR="${1:-/data/vram_monitor}"
mkdir -p "$OUT_DIR"

CURRENT_DAY=""
CURRENT_FILE=""

write_header() {
    echo "timestamp_iso,vram_used_mib,vram_total_mib,gpu_util_pct,gpu_temp_c,power_w" > "$1"
}

while true; do
    DAY=$(date -u +%Y-%m-%d)
    if [ "$DAY" != "$CURRENT_DAY" ]; then
        CURRENT_DAY="$DAY"
        CURRENT_FILE="$OUT_DIR/vram_$DAY.csv"
        if [ ! -f "$CURRENT_FILE" ]; then
            write_header "$CURRENT_FILE"
        fi
    fi

    TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    METRICS=$(nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw \
                         --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
    if [ -n "$METRICS" ]; then
        echo "$TS,$METRICS" >> "$CURRENT_FILE"
    fi
    sleep 1
done
