---
allowed-tools: Bash(docker compose:*), Bash(docker:*)
description: Start infrastructure services (PostgreSQL)
---

# Run Infrastructure

Start the PostgreSQL database via Docker Compose for local development.

## Run Command

Start postgres in detached mode and wait for health check:

```
docker compose up -d postgres
```

Verify postgres is healthy:

```
docker compose ps postgres
```

PostgreSQL will be available at `localhost:6501`.

## Connection Details

- **Host:** localhost
- **Port:** 6501
- **Database:** crypto_arbitrage
- **User:** postgres
- **Password:** postgres
- **URL:** `postgresql://postgres:postgres@localhost:6501/crypto_arbitrage`
