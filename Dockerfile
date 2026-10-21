# Voice agent server: webhook receiver + WebRTC bridge + operator console on port 8080.
# WebRTC media does not use port 8080. Read docs/deploy.md before running this anywhere real.
FROM python:3.12-slim-bookworm

COPY scripts/check-system-ca.sh /usr/local/bin/check-system-ca
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssl \
    && sh /usr/local/bin/check-system-ca \
    && rm -rf /var/lib/apt/lists/*


COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first (cached layer), exactly as locked.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY agent ./agent
COPY .env.example docker-entrypoint.sh ./
RUN uv sync --frozen --no-dev \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data && chown app /app/data && chmod 0700 /app/data \
    && chmod 0755 docker-entrypoint.sh

USER app
ENV PATH=/app/.venv/bin:$PATH DATA_DIR=/app/data PYTHONUNBUFFERED=1 LOGURU_LEVEL=INFO
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT') or 8080}/healthz\", timeout=3)"]
ENTRYPOINT ["/app/docker-entrypoint.sh"]
# Listens on $PORT when the host sets it (Railway does), else 8080.
CMD ["voice-agent", "serve", "--host", "0.0.0.0"]
