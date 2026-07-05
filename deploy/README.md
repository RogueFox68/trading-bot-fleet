# Fleet Container Deployment

The fleet runs as the `trading-fleet` container on the Beelink, defined in
`~/homelab/docker-compose.yml`. This directory is the build context source —
Dockerfile, entrypoint, and the PM2 ecosystem config all live here so that
code and deployment change together in one commit.

## Compose service block

Point the `trading-fleet` service at the repo (replaces the old
`~/homelab/fleet/` build context):

```yaml
  trading-fleet:
    build:
      context: /home/trader/bots/repo
      dockerfile: deploy/Dockerfile
    container_name: trading-fleet
    environment:
      - APP_DIR=/app/code
      - TZ=America/Chicago
    volumes:
      # Live code mount: git pull on the host = new code inside the container
      # (running processes keep old code until pm2 restart / container restart)
      - /home/trader/bots/repo:/app/code
    restart: unless-stopped
    depends_on:
      - influxdb
    # Fleet needs outbound internet for Alpaca API, Discord, yfinance.
    # No ports exposed - all communication is outbound.
```

Notes vs. the old setup:

- **No `env_file` / no `.env`.** Config truth is `config.py` inside the repo
  mount (gitignored). The old env-var layer (`config_docker.py` + `.env`) was
  never actually consumed by the bots and has been removed. One place to
  rotate secrets.
- **No `INFLUX_HOST` override.** `config.py` on the Beelink must set
  `INFLUX_HOST = "influxdb"` (Docker DNS name on the shared compose network).
- **No bundled code fallback.** If the volume mount is missing the container
  exits instead of trading a stale snapshot.

## Build / restart

```bash
cd ~/homelab
docker compose build trading-fleet
docker compose up -d trading-fleet     # recreates the container
docker exec trading-fleet pm2 ls       # verify the process list
```

Recreating the container resets in-container PM2 state, which is how the old
`moon_bag` process name retires (the ecosystem now registers it as `moon_bot`).

## Day-to-day code deploys

```bash
cd ~/bots/repo && git pull
docker exec trading-fleet pm2 restart all   # pick up the new code
```

A full `docker compose build` is only needed when `requirements.txt` or
anything in `deploy/` changes.
