# pgBouncer + Redis Infrastructure

## Overview

WiseRecipes API uses **pgBouncer** for connection pooling (transaction mode) and **Redis 7** for rate limiting and future session/cache use.

## Architecture

```
Client → FastAPI (uvicorn) → RateLimitMiddleware → Redis (6379)
                               ↓
                            APIKeyMiddleware
                               ↓
                            pgBouncer (6432) → PostgreSQL (5432)
```

## pgBouncer

- **Port**: 6432 (localhost only)
- **Pool mode**: transaction
- **Max client connections**: 500
- **Default pool size**: 25
- **Config**: `/etc/pgbouncer/pgbouncer.ini`
- **Userlist**: `/etc/pgbouncer/userlist.txt`
- **Service**: `systemctl status pgbouncer`

### Admin commands
```bash
# Check pool status
psql "host=127.0.0.1 port=6432 dbname=pgbouncer user=wisechef" -c "SHOW POOLS;"

# Check database stats
psql "host=127.0.0.1 port=6432 dbname=pgbouncer user=wisechef" -c "SHOW DATABASES;"

# Reload config without restart
sudo systemctl reload pgbouncer
```

## Redis

- **Port**: 6379 (localhost only)
- **Version**: 7.0.15
- **Max memory**: 256MB (allkeys-lru eviction)
- **Key prefix for rate limits**: `rate:{client_ip}`
- **Service**: `systemctl status redis-server`

### Useful commands
```bash
# Check rate limit keys
redis-cli KEYS "rate:*"

# Check count for specific IP
redis-cli ZCARD "rate:127.0.0.1"

# Clear all rate limits
redis-cli KEYS "rate:*" | xargs -r redis-cli DEL

# Memory usage
redis-cli INFO memory | grep used_memory_human
```

## Rate Limiting

The `RateLimitMiddleware` uses Redis Sorted Sets for sliding-window rate limiting:
- **Default**: 60 requests/minute per IP
- **Configurable**: `WR_RATE_LIMIT_PER_MINUTE` env var
- **Graceful fallback**: In-memory if Redis is unavailable
- **Persists across restarts**: Keys stored in Redis, not process memory

## Connection Flow

1. App connects to pgBouncer at `127.0.0.1:6432` (not Postgres directly at 5432)
2. pgBouncer pools connections in transaction mode — releases server connection after each transaction
3. 250 concurrent client connections are served by ~25 Postgres server connections
4. This raises effective connection capacity from ~30 to 500+

## Dependencies in systemd

The `wiserecipes-api.service` `After=` directive includes:
- `network.target`
- `postgresql.service`
- `redis-server.service`
- `pgbouncer.service`

## Migration Notes (2026-04-25)

- DATABASE_URL changed from `localhost:5432` to `127.0.0.1:6432`
- Added `WR_REDIS_URL=redis://localhost:6379/0` to .env and systemd unit
- In-memory rate limiter replaced with Redis-backed implementation
- `redis` pip package added to venv
