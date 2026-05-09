# macOS Apple Silicon Adaptation — super-memory stack

Tested: 2026-05-08, macOS ARM64, Docker Desktop (docker-compose standalone v5.1.0),
Ollama 0.23.2, cognee/cognee:latest (1.0.9-local), pgvector/pgvector:pg16, couchdb:3.

## Architecture Differences from Linux

| Component     | Linux (skill default)      | macOS adaptation              |
|---------------|---------------------------|-------------------------------|
| Postgres      | Docker container           | Docker container (same)       |
| CouchDB       | Docker container           | Docker container (same)       |
| Cognee API    | systemd + venv             | Docker container              |
| LiteLLM       | systemd + venv             | Native venv (no ARM64 image)  |
| Ollama        | not included               | brew services (Metal GPU)     |
| Watchdog      | cron + systemctl restart   | cron + docker restart         |
| Nightly       | cron + setsid+nohup        | cron + nohup (no setsid)      |

## Key Environment Variables for Cognee Container

```yaml
environment:
  GRAPH_DATABASE_PROVIDER: "postgres"           # NOT pgvector, NOT falkordb
  GRAPH_DATABASE_URL: "postgresql+asyncpg://cognee:PASS@postgres:5432/cognee_db"  # +asyncpg required
  VECTOR_DB_PROVIDER: "pgvector"
  DB_PROVIDER: "postgres"
  ENABLE_BACKEND_ACCESS_CONTROL: "false"        # Required without neo4j
  COGNEE_SKIP_CONNECTION_TEST: "true"           # Health check uses Cypher, fails with postgres graph
  LLM_PROVIDER: "openai"
  LLM_MODEL: "ollama/qwen3:8b"                 # ollama/ prefix required for litellm routing
  LLM_ENDPOINT: "http://host.docker.internal:11434"  # No /v1 for ollama/ prefix
  LLM_API_KEY: "ollama"
  EMBEDDING_PROVIDER: "openai"
  EMBEDDING_MODEL: "openai/text-embedding-3-small"   # See tiktoken alias trick below
  EMBEDDING_API_KEY: "ollama"
  EMBEDDING_ENDPOINT: "http://host.docker.internal:11434/v1"  # /v1 needed for openai/ prefix
  EMBEDDING_DIMENSIONS: "768"
```

## Tiktoken Alias Trick for Local Embeddings

cognee uses tiktoken internally, which fails on unknown model names like
`nomic-embed-text`. Workaround: create an Ollama alias matching an OpenAI name.

```bash
# Create alias: Ollama will serve nomic-embed-text when asked for text-embedding-3-small
echo "FROM nomic-embed-text" > /tmp/Modelfile.embed
ollama create text-embedding-3-small -f /tmp/Modelfile.embed
# Verify: should return dim=768
curl -s http://localhost:11434/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model":"text-embedding-3-small","input":"test"}' | python3 -c \
  "import sys,json;d=json.load(sys.stdin);print(f'dim={len(d[\"data\"][0][\"embedding\"])}')"
```

Then use `EMBEDDING_MODEL=openai/text-embedding-3-small` in cognee env. tiktoken
recognizes the OpenAI name, and Ollama serves the local model.

## Ollama Network Binding for Docker

Ollama defaults to 127.0.0.1 — unreachable from Docker containers. Fix:

```bash
# Edit the brew plist source (NOT ~/Library/LaunchAgents — that's regenerated)
PLIST=/opt/homebrew/Cellar/ollama/$(ollama --version | awk '{print $NF}')/homebrew.mxcl.ollama.plist
# Add OLLAMA_HOST to EnvironmentVariables dict:
#   <key>OLLAMA_HOST</key>
#   <string>0.0.0.0:11434</string>
brew services restart ollama
# Verify: should show TCP *:11434
lsof -i :11434 -sTCP:LISTEN
```

## Docker Port Mapping

Cognee image listens on port 8000 internally. Map to 8100 externally:
```yaml
ports:
  - "127.0.0.1:8100:8000"
```

Health check inside container must target port 8000:
```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8000/"]
```

## API Authentication

Even with ENABLE_BACKEND_ACCESS_CONTROL=false, cognee API requires auth:
```bash
# Register
curl -X POST http://127.0.0.1:8100/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@local.dev","password":"admin123456"}'
# Login (returns JWT)
curl -X POST http://127.0.0.1:8100/api/v1/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d 'username=admin@local.dev&password=admin123456'
# Use token
curl http://127.0.0.1:8100/api/v1/datasets -H "Authorization: Bearer $TOKEN"
```

## V1 API Usage (add → cognify → search)

```bash
# Add data (multipart file upload, NOT JSON)
curl -X POST http://127.0.0.1:8100/api/v1/add \
  -H "Authorization: Bearer $TOKEN" \
  -F "datasetName=mydata" -F "data=@file.txt"

# Cognify (process into knowledge graph)
curl -X POST http://127.0.0.1:8100/api/v1/cognify \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"datasets": ["mydata"]}'

# Search
curl -X POST http://127.0.0.1:8100/api/v1/search \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "your question"}'
```

## Shell Command Substitutions

| Linux              | macOS                        |
|--------------------|------------------------------|
| `ss -tlnp`         | `lsof -i -sTCP:LISTEN`      |
| `nproc`            | `sysctl -n hw.ncpu`         |
| `md5sum`           | `md5`                        |
| `setsid`           | N/A (use `nohup` directly)   |
| `sed -i 's/…'`     | `sed -i '' 's/…'`           |
| `docker compose`   | `docker-compose` (standalone)|
| `systemctl restart`| `docker restart sm-cognee`   |