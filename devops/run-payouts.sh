#!/usr/bin/env bash
# WiseRecipes Monthly Payout Runner
# Runs on the 1st of each month at 02:00 UTC
# Computes and executes creator payouts for the previous month

set -euo pipefail

API_KEY=$(grep WR_API_KEY /home/wisechef/wiserecipes-api/.env | cut -d= -f2)
API_URL="http://localhost:8200/api/admin/payouts/run"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting monthly payout run..."

# First do a dry run
echo "=== DRY RUN ==="
DRY_RUN_RESULT=$(curl -sf -X POST "${API_URL}?dry_run=true" \
  -H "x-api-key: ${API_KEY}" \
  -H "Content-Type: application/json")

echo "$DRY_RUN_RESULT" | python3 -m json.tool

TOTAL_CENTS=$(echo "$DRY_RUN_RESULT" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('total_cents', 0))")
TOTAL_COUNT=$(echo "$DRY_RUN_RESULT" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('total_count', 0))")

echo ""
echo "Dry run: ${TOTAL_COUNT} payouts, ${TOTAL_CENTS} cents total"

if [ "$TOTAL_COUNT" -eq 0 ]; then
    echo "No payouts to process. Done."
    exit 0
fi

# If dry run shows payouts, execute for real
echo ""
echo "=== LIVE RUN ==="
LIVE_RESULT=$(curl -sf -X POST "${API_URL}?dry_run=false" \
  -H "x-api-key: ${API_KEY}" \
  -H "Content-Type: application/json")

echo "$LIVE_RESULT" | python3 -m json.tool

echo ""
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Payout run complete."
