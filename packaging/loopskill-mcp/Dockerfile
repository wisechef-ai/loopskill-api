# loopskill-mcp — stdio MCP bridge image (Glama / any MCP directory that
# introspects a server by building and running its container).
#
# Built from THIS source tree, never `pip install loopskill-mcp` from PyPI:
# the image must carry exactly the reviewed code at the checked-out commit,
# independent of whatever version PyPI happens to serve.
#
#   docker build -t loopskill-mcp packaging/loopskill-mcp
#   docker run -i --rm loopskill-mcp                          # keyless: tools/list from the public server card
#   docker run -i --rm -e LOOPSKILL_API_KEY=... loopskill-mcp # keyed: full proxy to app.loopskill.io
#
# The container speaks MCP over stdio (`-i` is required). It opens no ports.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && rm -rf /src

RUN useradd --create-home --uid 10001 mcp
USER mcp
WORKDIR /home/mcp

ENTRYPOINT ["loopskill-mcp"]
