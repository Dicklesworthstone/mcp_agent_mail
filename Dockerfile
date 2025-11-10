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
COPY pyproject.toml ./
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
# syntax=docker/dockerfile:1

# Build stage: Use full Debian image with build tools
FROM python:3.14-bookworm AS build

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install build dependencies for asyncpg and other C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory to final location to avoid relocation issues
WORKDIR /opt/mcp-agent-mail

# Copy project files
COPY pyproject.toml uv.lock README.md ./
COPY src src
COPY third_party_docs third_party_docs
COPY project_idea_and_guide.md project_idea_and_guide.md
COPY AGENTS.md ./

# Create virtualenv and install dependencies
# The virtualenv is created at /opt/mcp-agent-mail/.venv to match runtime path
RUN uv sync --frozen --no-editable

# Runtime stage: Use slim image with runtime dependencies
FROM python:3.14-slim-bookworm AS runtime

# Install runtime dependencies: git (for GitPython) and libpq (for asyncpg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/mcp-agent-mail/.venv/bin:$PATH"

# Set working directory
WORKDIR /opt/mcp-agent-mail

# Copy the entire project including virtualenv from build stage
COPY --from=build /opt/mcp-agent-mail /opt/mcp-agent-mail

# Create non-root user and set ownership
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /opt/mcp-agent-mail

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8765

# Run the application
CMD ["uvicorn", "mcp_agent_mail.http:build_http_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]
