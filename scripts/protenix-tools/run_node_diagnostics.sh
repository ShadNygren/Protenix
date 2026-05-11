#!/bin/bash
# Run a comprehensive node diagnostic suite on whatever cloud GPU machine
# this container has been scheduled onto. Output is JSON to stdout and saved
# to /data/diagnostics-<machine_id>-<timestamp>.json.
#
# Designed to run at container startup OR on demand. Captures the
# information that varies per physical node: geo, network bandwidth,
# disk mount layout, GPU/CPU/RAM topology, and reachability to the
# storage backends we care about (Cloudflare R2, RunPod S3).
#
# Use it to score nodes before committing to a long training run there.
#
# Usage:
#   /opt/protenix-tools/run_node_diagnostics.sh
#   /opt/protenix-tools/run_node_diagnostics.sh --upload     # also push to R2
#   /opt/protenix-tools/run_node_diagnostics.sh --quick      # skip bandwidth tests

set +e  # don't abort on missing tools — collect what we can

UPLOAD=0
QUICK=0
for arg in "$@"; do
    case "$arg" in
        --upload) UPLOAD=1 ;;
        --quick)  QUICK=1 ;;
    esac
done

TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
MACHINE_ID="${SALAD_MACHINE_ID:-${RUNPOD_POD_ID:-$(hostname)}}"
OUT_DIR="/data"
[ -w "$OUT_DIR" ] || OUT_DIR="/tmp"
OUT_FILE="$OUT_DIR/diagnostics-${MACHINE_ID}-${TIMESTAMP}.json"

# === helpers ===
json_string() {
    python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))' 2>/dev/null \
        || printf '%s' "\"$(echo "$1" | sed 's/"/\\"/g; s/\\/\\\\/g')\""
}

run_capture() {
    local cmd="$1"
    local out
    out=$(eval "$cmd" 2>&1)
    printf '%s' "$out"
}

# === collect ===
echo "[diagnostics] machine_id=$MACHINE_ID timestamp=$TIMESTAMP"
echo "[diagnostics] writing to $OUT_FILE"

# Public IP + geolocation (one HTTP call; ipinfo.io includes city/region/country/org/asn)
GEO_JSON=$(curl -s --max-time 8 https://ipinfo.io/json 2>/dev/null || echo '{}')

# Hostname / kernel / uptime
HOSTNAME=$(hostname 2>/dev/null)
KERNEL=$(uname -a 2>/dev/null)
UPTIME=$(uptime -p 2>/dev/null)
LOADAVG=$(cat /proc/loadavg 2>/dev/null)

# CPU
CPU_INFO=$(lscpu 2>/dev/null | grep -E '^(Model name|Architecture|CPU\(s\)|Socket|Core|Thread|MHz|Cache|Vendor|Virtualization|Hypervisor)' | head -20)

# Memory
MEM_INFO=$(free -h 2>/dev/null)
MEM_DETAIL=$(cat /proc/meminfo 2>/dev/null | head -10)

# Disk mounts (where does our 250GB live?)
DISK_DF=$(df -h 2>/dev/null)
DISK_BLOCKS=$(lsblk 2>/dev/null)
DISK_INODES=$(df -ih 2>/dev/null | head -10)

# GPU
NVIDIA_INFO=$(nvidia-smi 2>/dev/null)
NVIDIA_QUERY=$(nvidia-smi --query-gpu=name,driver_version,memory.total,memory.free,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max,clocks.current.graphics,clocks.current.memory,temperature.gpu,power.limit --format=csv,noheader 2>/dev/null)
NVIDIA_TOPO=$(nvidia-smi topo -m 2>/dev/null | head -20)

# PCI bus topology (CPU-to-GPU, GPU-to-NIC distance)
LSPCI=$(lspci -nn 2>/dev/null | grep -iE 'nvidia|ethernet|network|wifi|wireless|nvme|sata' | head -20)

# Network interfaces
IP_ADDR=$(ip -j addr show 2>/dev/null | head -200)
IP_ROUTE=$(ip route 2>/dev/null)
DNS=$(cat /etc/resolv.conf 2>/dev/null | grep -v '^#')

# Container env clues (Salad / RunPod / k8s identification)
CONTAINER_ENV=$(env 2>/dev/null | grep -E '^(SALAD|RUNPOD|KUBE|K8S|HOSTNAME|HOME|PATH|PWD)=' | grep -vE '^(.*KEY|.*SECRET|.*TOKEN|.*PASS)' | sort)

# Storage endpoints we actually care about — measure latency + traceroute
R2_ENDPOINT="${CLOUDFLARE_R2_ENDPOINT:-https://r2.cloudflarestorage.com}"
RUNPOD_ENDPOINT="${RUNPOD_S3_ENDPOINT:-https://s3api-us-ks-2.runpod.io}"
PUBLIC_TARGETS=("8.8.8.8" "1.1.1.1")

R2_HOST=$(echo "$R2_ENDPOINT" | sed -E 's|https?://||; s|/.*||')
RUNPOD_HOST=$(echo "$RUNPOD_ENDPOINT" | sed -E 's|https?://||; s|/.*||')

PING_R2=$(ping -c 4 -W 3 "$R2_HOST" 2>/dev/null | tail -3)
PING_RUNPOD=$(ping -c 4 -W 3 "$RUNPOD_HOST" 2>/dev/null | tail -3)
PING_GOOG=$(ping -c 4 -W 3 8.8.8.8 2>/dev/null | tail -3)

# mtr is much more informative than traceroute (reports per-hop loss + jitter)
if [ "$QUICK" -eq 0 ]; then
    MTR_R2=$(mtr -r -c 5 -n "$R2_HOST" 2>/dev/null)
    MTR_RUNPOD=$(mtr -r -c 5 -n "$RUNPOD_HOST" 2>/dev/null)
fi

# HTTP latency + TLS handshake to R2 (curl timing)
CURL_R2=$(curl -o /dev/null -s --max-time 10 -w 'dns=%{time_namelookup}s connect=%{time_connect}s tls=%{time_appconnect}s ttfb=%{time_starttransfer}s total=%{time_total}s code=%{http_code}\n' "$R2_ENDPOINT/" 2>/dev/null)

# === bandwidth (skipped under --quick because it can take 30+ sec) ===
SPEEDTEST_OUT=""
IPERF_R2=""
if [ "$QUICK" -eq 0 ]; then
    if command -v speedtest-cli &>/dev/null; then
        SPEEDTEST_OUT=$(speedtest-cli --simple --timeout 30 2>&1 | head -10)
    elif command -v speedtest &>/dev/null; then
        SPEEDTEST_OUT=$(speedtest --accept-license --accept-gdpr -f tsv 2>&1 | head -5)
    fi

    # Real R2 throughput: download a known object, time it
    # Skip if no R2 creds — we only want to report failure modes that are about THIS machine
    if [ -n "$CLOUDFLARE_R2_ACCESS_KEY_ID" ] && [ -n "$CLOUDFLARE_R2_SECRET_ACCESS_KEY" ]; then
        R2_TEST_URL="${R2_ENDPOINT}/vh-protenix-training/diagnostics/r2-bandwidth-test-100mb.bin"
        R2_DL=$(curl -o /dev/null -s --max-time 60 -w 'size=%{size_download}B time=%{time_total}s speed=%{speed_download}B/s' "$R2_TEST_URL" 2>/dev/null)
    fi
fi

# === assemble JSON ===
python3 - <<PYEOF > "$OUT_FILE" 2>/dev/null
import json, os, sys
from datetime import datetime, timezone

def s(x):
    return x if x is not None else ""

doc = {
    "schema_version": 1,
    "captured_at_utc": "$TIMESTAMP",
    "machine_id": "$MACHINE_ID",
    "platform_hint": "salad" if os.environ.get("SALAD_MACHINE_ID") else ("runpod" if os.environ.get("RUNPOD_POD_ID") else "unknown"),
    "host": {
        "hostname": s("""$HOSTNAME"""),
        "kernel":   s("""$KERNEL"""),
        "uptime":   s("""$UPTIME"""),
        "loadavg":  s("""$LOADAVG"""),
    },
    "geo_ipinfo_raw": $GEO_JSON,
    "cpu_lscpu_excerpt": s("""$CPU_INFO"""),
    "memory_free_h": s("""$MEM_INFO"""),
    "memory_proc_meminfo_head": s("""$MEM_DETAIL"""),
    "disk": {
        "df_h":   s("""$DISK_DF"""),
        "lsblk":  s("""$DISK_BLOCKS"""),
        "df_inodes": s("""$DISK_INODES""")
    },
    "gpu": {
        "nvidia_smi": s("""$NVIDIA_INFO"""),
        "nvidia_smi_query_csv": s("""$NVIDIA_QUERY"""),
        "topology": s("""$NVIDIA_TOPO"""),
        "lspci_nvidia_and_net": s("""$LSPCI""")
    },
    "network": {
        "ip_addr_json_head": s("""$IP_ADDR"""),
        "ip_route": s("""$IP_ROUTE"""),
        "resolv_conf": s("""$DNS""")
    },
    "container_env_nonsecret": s("""$CONTAINER_ENV"""),
    "reachability": {
        "ping_r2_host": s("""$PING_R2"""),
        "ping_runpod_host": s("""$PING_RUNPOD"""),
        "ping_google_dns": s("""$PING_GOOG"""),
        "mtr_r2": s("""$MTR_R2"""),
        "mtr_runpod": s("""$MTR_RUNPOD"""),
        "curl_r2_timing": s("""$CURL_R2""")
    },
    "bandwidth": {
        "speedtest_simple": s("""$SPEEDTEST_OUT"""),
        "r2_object_download": s("""$R2_DL""")
    }
}
json.dump(doc, sys.stdout, indent=2)
PYEOF

echo "[diagnostics] wrote $OUT_FILE"

# Print human-readable summary to stdout (the Salad log viewer will surface this)
echo "===================================="
echo "NODE DIAGNOSTICS SUMMARY"
echo "===================================="
echo "Machine ID: $MACHINE_ID"
echo "Public IP / Geo (ipinfo.io):"
echo "$GEO_JSON" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(f"  IP: {d.get(\"ip\")}  City: {d.get(\"city\")}, {d.get(\"region\")}, {d.get(\"country\")}  ISP/ASN: {d.get(\"org\")}  Hostname: {d.get(\"hostname\")}")' 2>/dev/null || echo "  (ipinfo.io unreachable or unparseable)"
echo "Disk:"
echo "$DISK_DF"
echo "GPU:"
echo "$NVIDIA_QUERY"
echo "R2 latency:"
echo "  $CURL_R2"
echo "Speed test:"
echo "  $SPEEDTEST_OUT"
echo "===================================="

if [ "$UPLOAD" -eq 1 ] && [ -n "$CLOUDFLARE_R2_ACCESS_KEY_ID" ]; then
    DEST="s3://vh-protenix-training/node-diagnostics/${MACHINE_ID}/$(basename "$OUT_FILE")"
    echo "[diagnostics] uploading to $DEST"
    AWS_ACCESS_KEY_ID="$CLOUDFLARE_R2_ACCESS_KEY_ID" \
    AWS_SECRET_ACCESS_KEY="$CLOUDFLARE_R2_SECRET_ACCESS_KEY" \
        aws --endpoint-url "$CLOUDFLARE_R2_ENDPOINT" --region auto \
        s3 cp "$OUT_FILE" "$DEST" 2>&1 | tail -3
fi
