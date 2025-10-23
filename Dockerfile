# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.4.12 AS build
WORKDIR /app
COPY pyproject.toml uv.lock README.md .
COPY src src
COPY third_party_docs third_party_docs
COPY project_idea_and_guide.md project_idea_and_guide.md
COPY AGENTS.md .
RUN uv sync --frozen --no-editable

FROM python:3.14-slim AS runtime
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/mcp-agent-mail/.venv/bin:$PATH"
WORKDIR /opt/mcp-agent-mail
COPY --from=build /app /opt/mcp-agent-mail
RUN useradd -m appuser
USER appuser
EXPOSE 8765
CMD ["uvicorn", "mcp_agent_mail.http:build_http_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]
