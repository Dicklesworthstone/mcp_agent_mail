# syntax=docker/dockerfile:1.7
FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    PATH="/root/.local/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /app

# Copy project metadata and sync deps first for better caching
# README.md is required by hatchling since pyproject.toml references it
COPY pyproject.toml README.md ./
# Install runtime deps
RUN uv sync --no-dev

# Copy source
COPY src ./src

# Defaults suitable for container
ENV HTTP_HOST=0.0.0.0 \
    STORAGE_ROOT=/data/mailbox

EXPOSE 8765
VOLUME ["/data"]

# Create non-root user and set ownership on data dir
RUN adduser --disabled-password --gecos "" --uid 10001 appuser && \
    mkdir -p /data/mailbox && chown -R appuser:appuser /data /app
USER appuser

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8765/health/liveness || exit 1

# Run the HTTP server
CMD ["uv", "run", "python", "-m", "mcp_agent_mail.cli", "serve-http"]
