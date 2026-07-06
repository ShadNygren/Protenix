#!/usr/bin/env bash
# Re-confirm the separation boundary is intact: no Salad work in production
# prefixes, all Salad work isolated under salad_testing/<node-id>/.
source /dev/shm/secure/creds
EP="$CLOUDFLARE_R2_ENDPOINT"
NODE_ID=$(hostname)

echo "================================================================"
echo "R2 PREFIX SEPARATION AUDIT (Salad testing vs RunPod production)"
echo "Date: $(date -u)"
echo "Node:  $NODE_ID"
echo "================================================================"

echo ""
echo "=== A. Production prefixes (RunPod A100 ONLY) ==="
echo "--- checkpoints/idp_only/ (count of objects) ---"
A1=$(aws s3 ls "s3://vh-protenix-training/checkpoints/idp_only/" \
    --profile cloudflare-r2 --endpoint-url "$EP" --recursive 2>&1 | wc -l)
echo "  total: $A1"
echo "--- checkpoints/idp_only/ — Salad pollution? (smoke_v22|qualify_v22|salad_) ---"
P1=$(aws s3 ls "s3://vh-protenix-training/checkpoints/idp_only/" \
    --profile cloudflare-r2 --endpoint-url "$EP" --recursive 2>&1 \
    | grep -E "smoke_v22|qualify_v22|salad_" | wc -l)
echo "  count: $P1"
[ "$P1" -eq 0 ] && echo "  ✅ CLEAN — no Salad work in idp_only/"

echo ""
echo "--- checkpoints/interleaved/ (count of objects) ---"
A2=$(aws s3 ls "s3://vh-protenix-training/checkpoints/interleaved/" \
    --profile cloudflare-r2 --endpoint-url "$EP" --recursive 2>&1 | wc -l)
echo "  total: $A2"
echo "--- checkpoints/interleaved/ — Salad pollution? ---"
P2=$(aws s3 ls "s3://vh-protenix-training/checkpoints/interleaved/" \
    --profile cloudflare-r2 --endpoint-url "$EP" --recursive 2>&1 \
    | grep -E "smoke_v22|qualify_v22|salad_" | wc -l)
echo "  count: $P2"
[ "$P2" -eq 0 ] && echo "  ✅ CLEAN — no Salad work in interleaved/"

echo ""
echo "=== B. Salad testing prefix (this node) ==="
echo "--- checkpoints/salad_testing/$NODE_ID/ ---"
aws s3 ls "s3://vh-protenix-training/checkpoints/salad_testing/$NODE_ID/" \
    --profile cloudflare-r2 --endpoint-url "$EP" --recursive 2>&1 | head -20
S1=$(aws s3 ls "s3://vh-protenix-training/checkpoints/salad_testing/$NODE_ID/" \
    --profile cloudflare-r2 --endpoint-url "$EP" --recursive 2>&1 | wc -l)
echo "  total in this node's prefix: $S1"

echo ""
echo "=== C. Misnamed prefix that we removed ==="
echo "--- checkpoints/salad_qualification/ ---"
M=$(aws s3 ls "s3://vh-protenix-training/checkpoints/salad_qualification/" \
    --profile cloudflare-r2 --endpoint-url "$EP" --recursive 2>&1 | wc -l)
echo "  count: $M"
[ "$M" -eq 0 ] && echo "  ✅ EMPTY — misnamed prefix is gone"

echo ""
echo "=== D. Watcher status ==="
ps -ef | grep -v grep | grep "checkpoint_watcher.py" | head -1 | awk '{print $2, "  cmd:", substr($0, index($0,$8))}' | cut -c1-200

echo ""
echo "================================================================"
echo "SUMMARY"
echo "================================================================"
if [ "$P1" -eq 0 ] && [ "$P2" -eq 0 ] && [ "$M" -eq 0 ]; then
    echo "✅ SEPARATION INTACT"
    echo "   - Production prefixes (idp_only, interleaved) have ZERO Salad pollution"
    echo "   - Salad work isolated under salad_testing/$NODE_ID/ ($S1 objects)"
    echo "   - Misnamed salad_qualification/ is empty"
    echo ""
    echo "Quarantine cmd (if data corruption later found):"
    echo "  aws s3 rm --recursive s3://vh-protenix-training/checkpoints/salad_testing/$NODE_ID/ \\"
    echo "      --profile cloudflare-r2 --endpoint-url $EP"
else
    echo "❌ SEPARATION VIOLATED — bytes from Salad found in production prefixes"
    echo "   idp_only Salad count: $P1"
    echo "   interleaved Salad count: $P2"
    echo "   salad_qualification count: $M"
fi
