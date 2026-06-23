#!/usr/bin/env bash
# examples/mcp/03-cookbook-share.sh
#
# Share-token roundtrip via the REST API.
#
# Demonstrates:
#   1. Create a share token for a cookbook   — POST /api/cookbooks/{id}/share-tokens
#   2. List existing share tokens             — GET  /api/cookbooks/{id}/share-tokens
#   3. Rotate the token                       — POST /api/cookbooks/{id}/share-tokens/{token_id}/rotate
#   4. Revoke the token                       — DELETE /api/cookbooks/{id}/share-tokens/{token_id}
#
# Prerequisites:
#   export RECIPES_API_KEY=rec_xx...x
#   export COOKBOOK_ID=<your-cookbook-uuid>
#
# The share token (cbt_<8hex>_<32hex>) is shown EXACTLY ONCE on creation.
# Distribute it to collaborators — they use it as the x-api-key value to
# install skills from your cookbook without owning your account.
#
# Token scopes:
#   read    — browse cookbook contents (metadata only)
#   install — download/install skills (default)
#   edit    — add/remove skills

set -euo pipefail

BASE_URL="${RECIPES_BASE_URL:-https://recipes.wisechef.ai}"
API_KEY="${RECIPES_API_KEY:?Please set RECIPES_API_KEY}"
COOKBOOK_ID="${COOKBOOK_ID:?Please set COOKBOOK_ID to your cookbook UUID}"
SCOPE="${TOKEN_SCOPE:-install}"
TOKEN_NAME="${TOKEN_NAME:-ci-share-$(date +%Y%m%d)}"

echo "=== 1. Create share token ==="
echo "POST ${BASE_URL}/api/cookbooks/${COOKBOOK_ID}/share-tokens"

CREATE_RESPONSE=$(
  curl -sS -X POST \
    "${BASE_URL}/api/cookbooks/${COOKBOOK_ID}/share-tokens" \
    -H "x-api-key: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"${TOKEN_NAME}\", \"scope\": \"${SCOPE}\"}"
)

echo "${CREATE_RESPONSE}" | python3 -m json.tool

TOKEN=$(echo "${CREATE_RESPONSE}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token',''))")
TOKEN_ID=$(echo "${CREATE_RESPONSE}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))")

if [[ -z "${TOKEN}" ]]; then
  echo "ERROR: could not extract token from response." >&2
  exit 1
fi

echo ""
echo ">>> Share token (shown ONCE — save it now):"
echo "    ${TOKEN}"
echo ""
echo "Distribute this token as x-api-key to grant ${SCOPE} access to cookbook ${COOKBOOK_ID}."
echo ""

# ---

echo "=== 2. List share tokens ==="
echo "GET ${BASE_URL}/api/cookbooks/${COOKBOOK_ID}/share-tokens"

curl -sS \
  "${BASE_URL}/api/cookbooks/${COOKBOOK_ID}/share-tokens" \
  -H "x-api-key: ${API_KEY}" | python3 -m json.tool

# ---

if [[ -z "${TOKEN_ID}" ]]; then
  echo ""
  echo "Skipping rotate + revoke (no token_id returned)."
  exit 0
fi

echo ""
echo "=== 3. Rotate token (id=${TOKEN_ID}) ==="
echo "POST ${BASE_URL}/api/cookbooks/${COOKBOOK_ID}/share-tokens/${TOKEN_ID}/rotate"

ROTATE_RESPONSE=$(
  curl -sS -X POST \
    "${BASE_URL}/api/cookbooks/${COOKBOOK_ID}/share-tokens/${TOKEN_ID}/rotate" \
    -H "x-api-key: ${API_KEY}"
)

echo "${ROTATE_RESPONSE}" | python3 -m json.tool

NEW_TOKEN=$(echo "${ROTATE_RESPONSE}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('new_token',''))")
NEW_TOKEN_ID=$(echo "${ROTATE_RESPONSE}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('new_token_id',''))")

echo ""
echo ">>> New token (shown ONCE):"
echo "    ${NEW_TOKEN}"
echo ""

# ---

REVOKE_ID="${NEW_TOKEN_ID:-$TOKEN_ID}"
echo "=== 4. Revoke token (id=${REVOKE_ID}) ==="
echo "DELETE ${BASE_URL}/api/cookbooks/${COOKBOOK_ID}/share-tokens/${REVOKE_ID}"

curl -sS -X DELETE \
  "${BASE_URL}/api/cookbooks/${COOKBOOK_ID}/share-tokens/${REVOKE_ID}" \
  -H "x-api-key: ${API_KEY}" | python3 -m json.tool

echo ""
echo "Done. Token ${REVOKE_ID} is now revoked."
