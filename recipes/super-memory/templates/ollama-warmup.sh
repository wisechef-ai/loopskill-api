#!/usr/bin/env bash
# Pre-warm Ollama models before cognee operations to avoid cold-start failures.
# Origin: feedback issue #61 — nightly-ingest cron failed because Ollama unloads
# models after idle timeout; the first cognee call after wake-up timed out.
#
# Usage (call from launch-from-cron.sh BEFORE cognee operations):
#   bash $HOME/super-memory/templates/ollama-warmup.sh
#
# Idempotent: each call sends a 1-token request to load model into VRAM.

set -euo pipefail

OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
LLM_MODEL="${LLM_WARMUP_MODEL:-qwen3:8b}"
EMBED_MODEL="${EMBED_WARMUP_MODEL:-text-embedding-3-small}"

for model in "$LLM_MODEL" "$EMBED_MODEL"; do
  echo "[warmup] $model"
  curl -fsS -X POST "$OLLAMA_URL/api/generate" \
    -H 'content-type: application/json' \
    -d "{\"model\":\"$model\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":\"30m\"}" \
    > /dev/null || echo "[warmup] $model failed (continuing)"
done

echo '[warmup] done'
