# Multi-stage build: produce a single image that serves both the React dashboard
# and the Python agent server. Designed for GCP Cloud Run (PORT env, /tmp scratch,
# stateless). See cloudrun/README.md for deploy + auth model.

# ---- stage 1: build the React dashboard --------------------------------------
FROM node:22-alpine AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
# Produces /web/dist/ (static SPA). The dist/server.cjs Node bundle is also
# emitted but unused in the Cloud Run path — Python serves the static files.
RUN npm run build

# ---- stage 2: runtime --------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Cloud Run is single-tenant, ephemeral fs. Read-only is the safe default;
    # auto-accept/yolo can be set per deployment via env.
    SWE_AGENT_TRUST_NETWORK=1 \
    SWE_AGENT_STATIC_DIR=/app/web/dist \
    SWE_AGENT_BACKEND=anthropic \
    ANTHROPIC_MODEL=claude-sonnet-4-6

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY swe_agent/ ./swe_agent/
COPY swe_agent.py ./
COPY --from=web-build /web/dist ./web/dist

# Cloud Run requires the workspace to be writeable; only /tmp is.
RUN mkdir -p /tmp/workspace

# `PORT` is injected by Cloud Run (defaults to 8080 locally). The CLI honors it
# in main()'s argparse defaults, so no shell substitution is needed here.
EXPOSE 8080

CMD ["python", "-m", "swe_agent.server", \
     "--host", "0.0.0.0", \
     "--cwd", "/tmp/workspace", \
     "--model", "claude-sonnet-4-6", \
     "--approval", "read-only", \
     "--no-preflight", \
     "--no-persist"]
