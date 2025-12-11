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

# Create virtualenv and install dependencies including postgres extras (asyncpg)
# The virtualenv is created at /opt/mcp-agent-mail/.venv to match runtime path
RUN uv sync --frozen --no-editable --extra postgres

# Runtime stage: Use slim image with runtime dependencies
FROM python:3.14-slim-bookworm AS runtime

# Install runtime dependencies: git (for GitPython), libpq (for asyncpg), curl (for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/mcp-agent-mail/.venv/bin:$PATH"
ENV HTTP_HOST=0.0.0.0
ENV STORAGE_ROOT=/data/mailbox

# Set working directory
WORKDIR /opt/mcp-agent-mail

# Copy the entire project including virtualenv from build stage
COPY --from=build /opt/mcp-agent-mail /opt/mcp-agent-mail

# Create non-root user, data directory, and set ownership
RUN useradd -m -u 1000 appuser && \
    mkdir -p /data/mailbox && \
    chown -R appuser:appuser /opt/mcp-agent-mail /data

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8765

# Volume for persistent data
VOLUME ["/data"]

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8765/health/liveness || exit 1

# Run the HTTP server using CLI (properly initializes settings)
CMD ["python", "-m", "mcp_agent_mail.cli", "serve-http"]
