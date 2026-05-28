# CORS Policy — WiseRecipes API

## Summary

The API uses a **strict origin allow-list** via Starlette's `CORSMiddleware`.
Only the two canonical production web origins are permitted to make
credentialed cross-origin requests from browsers.

## Allowed Origins

| Origin | Rationale |
|--------|-----------|
| `https://recipes.wisechef.ai` | Primary production frontend |
| `https://www.recipes.wisechef.ai` | www-prefixed variant (same deployment) |

## Policy Settings

```python
CORSMiddleware(
    allow_origins=[
        "https://recipes.wisechef.ai",
        "https://www.recipes.wisechef.ai",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["x-api-key", "authorization", "content-type"],
)
```

- **`allow_credentials=True`** — Required so the browser sends the session
  cookie or `x-api-key` header on cross-origin requests from the frontend.
- **`allow_methods`** — Covers the full REST verb set used by the API;
  `OPTIONS` is handled implicitly by `CORSMiddleware` for preflight.
- **`allow_headers`** — The three headers our frontend needs:
  `x-api-key` for API-key auth, `authorization` for JWT, `content-type`
  for JSON POST bodies.

## Middleware Order

`CORSMiddleware` is the **outermost** layer so that preflight `OPTIONS`
requests are answered immediately — before `RateLimitMiddleware` or
`APIKeyMiddleware` touch the request. This prevents browsers from being
rate-limited on preflight and from receiving a 401 on the OPTIONS probe.

```
Incoming request
  → CORSMiddleware      ← adds CORS response headers / handles OPTIONS
  → RateLimitMiddleware
  → APIKeyMiddleware
  → BucketHostMiddleware
  → Route handler
```

## MCP Endpoint Considerations

The `/api/mcp/sse` and `/api/mcp/http` endpoints are used exclusively by
AI agents and CLI tools that communicate **programmatically** — they never
run inside a browser context and therefore never send an HTTP `Origin`
header. Because no `Origin` header is present, `CORSMiddleware` is a no-op
for these requests and the restrictive allow-list has zero effect on MCP
traffic.

**Why not a wildcard for MCP?**

A wildcard (`allow_origins=["*"]`) combined with `allow_credentials=True`
is rejected by the browser spec and by Starlette itself.  Since MCP clients
are not browsers, there is no benefit to widening the origin list to include
a wildcard.  If a future MCP web-client arises that needs its own origin
permitted, add that origin explicitly to the allow-list rather than opening
a wildcard.

## Local Development

During local development (`localhost`, `127.0.0.1`, `http://localhost:*`)
there is currently no `CORS_ALLOW_ORIGINS` override injected by
`app/config.py`.  Two options for developers:

1. Use the API via the same host (no cross-origin calls).
2. Add a `CORS_EXTRA_ORIGINS` env-var toggle in `app/config.py` and merge
   it into `allow_origins` at startup (future work).

## References

- [MDN CORS guide](https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS)
- [Starlette CORSMiddleware docs](https://www.starlette.io/middleware/#corsmiddleware)
- `app/main.py` — middleware registration
- `tests/test_topshelf_n_cors.py` — CORS regression tests
