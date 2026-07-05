# =============================================================================
# Trading Bot Fleet - Docker Image
# Base: Python 3.11 slim + Node.js 20 (for PM2 process manager)
#
# Build context is the REPO ROOT (so requirements.txt is the single dependency
# manifest for both the image and the code). Build from ~/homelab with:
#
#   docker compose build trading-fleet
#
# after pointing the compose service at this repo (see deploy/README.md).
# =============================================================================
FROM python:3.11-slim

# -- System deps --
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        gcc \
        g++ \
        git \
        procps \
        gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g pm2 \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# -- Working directory --
WORKDIR /app

# -- Python dependencies (cached layer - only rebuilds when requirements change) --
# Comes from the repo root: one manifest for image and code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# -- PM2 ecosystem config + entrypoint --
COPY deploy/ecosystem.config.js .
COPY deploy/entrypoint.sh .
RUN chmod +x entrypoint.sh

# NOTE: deliberately no bundled code snapshot. The repo is volume-mounted at
# /app/code; if the mount is missing the entrypoint exits hard rather than
# trading against a stale copy of the strategies.

# -- Health check: verify pm2 is responsive --
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD pm2 ping 2>/dev/null || exit 1

ENTRYPOINT ["./entrypoint.sh"]
