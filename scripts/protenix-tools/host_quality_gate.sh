#!/usr/bin/env bash
# host_quality_gate.sh — Early detection of unsuitable Salad/cloud hosts
#
# Run this FIRST in the Docker entrypoint, BEFORE downloading any data.
# If the host fails the quality gate, the container exits to trigger
# platform reallocation to a different physical host.
#
# Every host attempt (pass or fail) is logged to R2 for statistics:
#   s3://vh-protenix-training/host_registry/<container_id>.json
#
# Checks (in order of speed):
#   1. WSL2 detection (instant — uname check)
#   2. CPU clock speed (instant — /proc/cpuinfo)
#   3. CPU single-thread benchmark (< 2 seconds)
#   4. Memory bandwidth benchmark (< 3 seconds)
#   5. PCIe link width/gen for GPU (instant — nvidia-smi)
#
# Exit codes:
#   0 = host passes all checks, proceed with training
#   1 = host REJECTED, platform should reallocate
#   2 = script error (treat as pass to avoid false rejections)
#
# Environment variables:
#   GATE_MIN_MHZ=3400        Minimum CPU MHz (rejects VMs with locked clocks)
#   GATE_MIN_GFLOPS=120      Minimum numpy matmul GFLOPS
#   GATE_MIN_MEMBW=20        Minimum memory bandwidth GB/s
#   GATE_MIN_PCIE_WIDTH=8    Minimum PCIe link width
#   GATE_SKIP=1              Skip all checks (for debugging)
#   GATE_LOG=/workspace/host_quality_gate.log
#
# Usage:
#   bash /opt/protenix-tools/host_quality_gate.sh
#   # Returns 0 (pass) or 1 (reject)

set -euo pipefail

# Skip gate entirely if requested (for debugging)
if [ "${GATE_SKIP:-0}" = "1" ]; then
    echo "[quality-gate] GATE_SKIP=1, skipping all checks"
    exit 0
fi

# Configurable thresholds
GATE_MIN_MHZ="${GATE_MIN_MHZ:-3400}"
GATE_MIN_GFLOPS="${GATE_MIN_GFLOPS:-120}"
GATE_MIN_MEMBW="${GATE_MIN_MEMBW:-20}"
GATE_MIN_PCIE_WIDTH="${GATE_MIN_PCIE_WIDTH:-8}"
GATE_LOG="${GATE_LOG:-/workspace/host_quality_gate.log}"

# Ensure log directory exists
mkdir -p "$(dirname "$GATE_LOG")" 2>/dev/null || true

log() {
    local msg="[quality-gate $(date -u +%H:%M:%S)] $*"
    echo "$msg"
    echo "$msg" >> "$GATE_LOG" 2>/dev/null || true
}

# Upload host record to R2 (best-effort, non-blocking)
upload_host_record() {
    local status="$1"
    local reason="$2"
    local details="$3"
    local container_id
    container_id=$(hostname)
    local timestamp
    timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    local record_file="/tmp/host_record_${container_id}.json"

    cat > "$record_file" <<RECORD
{
  "container_id": "$container_id",
  "timestamp": "$timestamp",
  "status": "$status",
  "reason": "$reason",
  "kernel": "$(uname -r)",
  "cpu_model": "$(grep 'model name' /proc/cpuinfo 2>/dev/null | head -1 | cut -d: -f2 | xargs)",
  "cpu_mhz": "$(grep 'cpu MHz' /proc/cpuinfo 2>/dev/null | head -1 | cut -d: -f2 | xargs)",
  "cpu_cores": "$(grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo 0)",
  "mem_total_mb": "$(awk '/MemTotal/ {printf "%.0f", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)",
  "gpu_name": "$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo unknown)",
  "pcie_gen": "$(nvidia-smi --query-gpu=pcie.link.gen.current --format=csv,noheader 2>/dev/null || echo unknown)",
  "pcie_width": "$(nvidia-smi --query-gpu=pcie.link.width.current --format=csv,noheader 2>/dev/null || echo unknown)",
  "details": $details
}
RECORD

    # Upload to R2 if credentials are available
    if [ -n "${CLOUDFLARE_R2_ACCESS_KEY_ID:-}" ] && [ -n "${CLOUDFLARE_R2_SECRET_ACCESS_KEY:-}" ] && [ -n "${CLOUDFLARE_R2_ENDPOINT:-}" ]; then
        AWS_ACCESS_KEY_ID="$CLOUDFLARE_R2_ACCESS_KEY_ID" \
        AWS_SECRET_ACCESS_KEY="$CLOUDFLARE_R2_SECRET_ACCESS_KEY" \
        aws --endpoint-url "$CLOUDFLARE_R2_ENDPOINT" --region auto \
            s3 cp "$record_file" \
            "s3://vh-protenix-training/host_registry/${timestamp}_${container_id}_${status}.json" \
            --quiet 2>/dev/null &
        log "  -> host record uploading to R2 (background)"
    else
        log "  -> R2 creds not available, host record saved locally only: $record_file"
    fi
}

reject() {
    log "REJECTED: $*"
    upload_host_record "rejected" "$*" "{}"
    log "Exiting to trigger reallocation..."
    exit 1
}

# Track timing
GATE_START=$(date +%s%N 2>/dev/null || date +%s)

log "Starting host quality gate (container: $(hostname))"

# ============================================================
# CHECK 1: WSL2 Detection (instant)
# ============================================================
KERNEL=$(uname -r)
log "Check 1/5: Kernel = $KERNEL"

if echo "$KERNEL" | grep -qi "microsoft\|WSL"; then
    reject "WSL2 host detected (kernel=$KERNEL). dxgkrnl GPU passthrough adds 50-70% CUDA latency. CPU boost locked by Hyper-V."
fi

if [ -f /proc/version ] && grep -qi "microsoft" /proc/version 2>/dev/null; then
    reject "WSL2 host detected (/proc/version). dxgkrnl GPU passthrough adds 50-70% CUDA latency."
fi

log "  -> Native Linux kernel, OK"

# ============================================================
# CHECK 2: CPU Clock Speed (instant)
# ============================================================
CPU_MHZ=$(grep "cpu MHz" /proc/cpuinfo 2>/dev/null | awk -F: '{print $2}' | awk '{sum+=$1; n++} END {if(n>0) printf "%.0f", sum/n; else print "0"}')
CPU_MODEL=$(grep "model name" /proc/cpuinfo 2>/dev/null | head -1 | awk -F: '{print $2}' | xargs)
CPU_CORES=$(grep -c "^processor" /proc/cpuinfo 2>/dev/null || echo 0)

log "Check 2/5: CPU = $CPU_MODEL ($CPU_CORES cores, ${CPU_MHZ} MHz avg)"

if [ "$CPU_MHZ" -gt 0 ] && [ "$CPU_MHZ" -lt "$GATE_MIN_MHZ" ]; then
    MHZ_SPREAD=$(grep "cpu MHz" /proc/cpuinfo 2>/dev/null | awk -F: '{print $2}' | awk '{if(min=="") min=max=$1; if($1<min) min=$1; if($1>max) max=$1} END {printf "%.0f", max-min}')
    if [ "$MHZ_SPREAD" -lt 10 ]; then
        reject "CPU clock locked at ${CPU_MHZ} MHz (spread=${MHZ_SPREAD}, threshold=${GATE_MIN_MHZ}). Likely VM without boost. CPU: $CPU_MODEL"
    else
        log "  -> CPU below threshold (${CPU_MHZ} < ${GATE_MIN_MHZ}) but has clock spread (${MHZ_SPREAD} MHz) — may be in power-save, allowing"
    fi
else
    log "  -> CPU at ${CPU_MHZ} MHz >= ${GATE_MIN_MHZ} MHz, OK"
fi

# ============================================================
# CHECK 3: CPU Single-Thread Benchmark (< 2 seconds)
# ============================================================
log "Check 3/5: Running CPU benchmark..."

BENCH_RESULT=$(python3 -c "
import time, numpy as np
np.random.seed(42)
a = np.random.randn(2000, 2000).astype(np.float64)
b = np.random.randn(2000, 2000).astype(np.float64)
_ = a[:100] @ b[:100].T
t0 = time.perf_counter()
c = a @ b
t1 = time.perf_counter()
gflops = 2 * 2000**3 / (t1 - t0) / 1e9
print(f'{t1-t0:.4f} {gflops:.1f}')
" 2>/dev/null || echo "0 0")

BENCH_TIME=$(echo "$BENCH_RESULT" | awk '{print $1}')
BENCH_GFLOPS=$(echo "$BENCH_RESULT" | awk '{print $2}')

GFLOPS_OK=$(echo "$BENCH_GFLOPS $GATE_MIN_GFLOPS" | awk '{print ($1 >= $2) ? 1 : 0}')

log "  -> ${BENCH_GFLOPS} GFLOPS (${BENCH_TIME}s, threshold: ${GATE_MIN_GFLOPS})"

if [ "$GFLOPS_OK" -eq 0 ] && [ "$BENCH_GFLOPS" != "0" ]; then
    reject "CPU too slow: ${BENCH_GFLOPS} GFLOPS < ${GATE_MIN_GFLOPS} threshold. CPU: $CPU_MODEL"
fi

# ============================================================
# CHECK 4: Memory Bandwidth (< 3 seconds)
# ============================================================
log "Check 4/5: Running memory bandwidth test..."

MEMBW_RESULT=$(python3 -c "
import time, numpy as np
a = np.zeros(100_000_000, dtype=np.float32)
b = np.zeros_like(a)
np.copyto(b[:1000], a[:1000])
t0 = time.perf_counter()
np.copyto(b, a)
t1 = time.perf_counter()
gb_s = (a.nbytes * 2) / (t1 - t0) / 1e9
print(f'{gb_s:.1f}')
" 2>/dev/null || echo "0")

log "  -> ${MEMBW_RESULT} GB/s (threshold: ${GATE_MIN_MEMBW})"

MEMBW_OK=$(echo "$MEMBW_RESULT $GATE_MIN_MEMBW" | awk '{print ($1 >= $2) ? 1 : 0}')
if [ "$MEMBW_OK" -eq 0 ] && [ "$MEMBW_RESULT" != "0" ]; then
    reject "Memory bandwidth too low: ${MEMBW_RESULT} GB/s < ${GATE_MIN_MEMBW} threshold"
fi

# ============================================================
# CHECK 5: GPU PCIe Link (instant)
# ============================================================
PCIE_WIDTH=$(nvidia-smi --query-gpu=pcie.link.width.current --format=csv,noheader,nounits 2>/dev/null || echo "16")
PCIE_GEN=$(nvidia-smi --query-gpu=pcie.link.gen.current --format=csv,noheader,nounits 2>/dev/null || echo "4")
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo "unknown")

log "Check 5/5: GPU = $GPU_NAME, PCIe Gen${PCIE_GEN} x${PCIE_WIDTH}"

if [ "$PCIE_WIDTH" -lt "$GATE_MIN_PCIE_WIDTH" ]; then
    reject "GPU PCIe too narrow: x${PCIE_WIDTH} < x${GATE_MIN_PCIE_WIDTH}. GPU: $GPU_NAME"
fi

# ============================================================
# ALL CHECKS PASSED
# ============================================================
GATE_END=$(date +%s%N 2>/dev/null || date +%s)
if [ ${#GATE_START} -gt 10 ]; then
    GATE_ELAPSED_MS=$(( (GATE_END - GATE_START) / 1000000 ))
else
    GATE_ELAPSED_MS=$(( (GATE_END - GATE_START) * 1000 ))
fi

log "PASSED all checks in ${GATE_ELAPSED_MS}ms"
log "  Host: $CPU_MODEL, ${CPU_MHZ} MHz, ${BENCH_GFLOPS} GFLOPS, ${MEMBW_RESULT} GB/s"
log "  GPU: $GPU_NAME, PCIe Gen${PCIE_GEN} x${PCIE_WIDTH}"

DETAILS="{\"cpu_mhz\": $CPU_MHZ, \"gflops\": $BENCH_GFLOPS, \"membw_gbs\": $MEMBW_RESULT, \"pcie_gen\": $PCIE_GEN, \"pcie_width\": $PCIE_WIDTH, \"elapsed_ms\": $GATE_ELAPSED_MS}"
upload_host_record "passed" "all checks passed" "$DETAILS"

exit 0
