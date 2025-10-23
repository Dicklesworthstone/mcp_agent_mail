# Python FastMCP Best Practices for Web Apps (mid-2025 Edition by Jeffrey Emanuel)

*   **uv and a venv targeting only python 3.13 and higher (NOT pip/poetry/conda!)**; key commands to use for this are:
    *   `uv venv --python 3.13`
    *   `uv lock --upgrade`
    *   `uv sync --all-extras`

*   **pyproject.toml with hatchling build system**; ruff for linter and mypy for type checking;

*   **.envrc file containing `source .venv/bin/activate`** (for direnv)

*   **setup.sh script for automating all that stuff targeting ubuntu 25**

*   **All settings handled via .env file using the python-decouple library**; key pattern to always use:

    ```python
    from decouple import Config as DecoupleConfig, RepositoryEnv
    decouple_config = DecoupleConfig(RepositoryEnv(".env"))
    POSTGRES_URL = decouple_config("DATABASE_URL")
    ```

*   **fastmcp for the backend**; building an interoperable, secure, and discoverable Model Context Protocol (MCP) server. The server exposes its capabilities (tools, resources) in a standardized way, allowing multiple different LLM clients to interact with it without custom integrations.

*   **sqlmodel/sqlalchemy for database connecting to postgres**; alembic for db migrations. Database operations should be as efficient as possible; batch operations should use batch insertions where possible (same with reads); we should create all relevant database indexes to optimize the access patterns we care about, and create views where it simplifies the code and improves performance.

*   **where it would help a lot and make sense in the overall flow of logic and be complementary, we should liberally use redis to speed things up.**

*   **typer library used for any CLI (including detailed help)**

*   **rich library used for all console output**; really leveraging all the many powerful features to make everything looks extremely slick, polished, colorful, detailed; syntax highlighting for json, progress bars, rounded boxes, live panels (be careful about having more than one live panel at once!), etc.

*   **For HTTP transports, uvicorn with uvloop for serving (to be reverse proxied from NGINX)**. For local development and specific clients, the default STDIO transport is used.

*   For key functionality in the app and key dependencies (e.g., postgres database, redis, elastic search, openai API, etc) we want to **"fail fast"** so we can address core bugs and problems, not hide issues and try to recover gracefully from everything.

*   **Async for absolutely everything**: all network activity (use httpx); all file access (use aiofiles); all database operations (sqlmodel/sqlalchemy/psycopg2); etc. All FastMCP tools should be `async def`.

*   **No unit tests or mocks; no fake/generated data; always REAL data, REAL API calls, and REAL, REALISTIC, ACTUAL END TO END INTEGRATION TESTS**. All integration tests should feature super detailed and informative logging using the rich library.

*   Aside from the allowed ruff exceptions specified in the `pyproject.toml` file, we must always strive for **ZERO ruff linter warnings/errors** in the entire project, as well as **ZERO mypy warnings/errors**!

*   Network requests (especial API calls to third party services like OpenAI, Gemini, Anthropic, etc. should be properly **rate limited with semaphores and use robust retry with exponential backoff and random jitter**. Where possible, we should always try to do network requests in parallel using `asyncio.gather()` and similar design patterns (using the semaphores to prevent rate limiting issues automatically).

*   Usage of AI APIs should either get **precise token length estimates** using official APIs or should use the `tiktoken` library and the relevant tokenizer, never estimate using simplistic rules of thumb. We should always carefully track and monitor and report (using rich console output) the total costs of using APIs since the last startup of the app, for the most recent operations, etc. and track approximate run-rate of spend per day using extrapolation.

*   Code should be **sensibly organized by functional areas** using customary and typical code structures to make it easy and familiar to navigate. But we don't want extreme fragmentation and proliferation of tiny code files! It's about striking the right balance so we don't end up with excessively long and monolithic code files but so we also don't have dozens and dozens of code files with under 50 lines each!

Here is a sample complete pyproject.toml file showing the basic structure of an example application:

```toml
# pyproject.toml

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "smartedgar"
version = "0.1.0"
description = "SEC EDGAR filing downloader and processor with MCP Server"
readme = "README.md"
requires-python = ">=3.13"
license = { text = "MIT" }
authors = [
    { name = "SmartEdgar Team", email = "info@smartedgar.ai" },
]
classifiers = [
    "Development Status :: 4 - Beta",
    "Intended Audience :: Financial and Insurance Industry",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.13",
    "Topic :: Office/Business :: Financial :: Investment",
    "Topic :: Scientific/Engineering :: Artificial Intelligence",
    "Topic :: Text Processing :: Indexing",
    "Typing :: Typed",
]

# Core dependencies
dependencies = [
    # MCP framework and server
    "fastmcp >= 2.10.0",
    "uvicorn[standard] >= 0.35.0",
    # Async operations and HTTP
    "aiofiles >= 23.2.0",
    "aiohttp[speedups] >= 3.9.0",
    "aiohttp-retry >= 2.8.0",
    "aioh2 >= 0.2.0",
    "aiolimiter >= 1.1.0",
    "aiosqlite >= 0.19.0",
    "httpx[http2] >= 0.25.0",
    # Data processing and validation
    "beautifulsoup4 >= 4.12.0",
    "lxml >= 4.9.0",
    "html2text >= 2020.1.0",
    "html5lib >= 1.1",
    "pydantic >= 2.7.0",
    "python-decouple>=3.8",
    "pandas >= 2.0.0",
    # Database and ORM
    "sqlalchemy >= 2.0.41",
    "sqlmodel >= 0.0.15",
    # Text processing and NLP
    "tiktoken >= 0.5.0",
    "nltk >= 3.8.0",
    "fuzzywuzzy >= 0.18.0",
    "python-Levenshtein >= 0.20.0",
    "tenacity >= 8.2.0",
    # PDF and document processing
    "PyMuPDF >= 1.23.0",
    "PyPDF2 >= 3.0.0",
    "pdf2image >= 1.16.0",
    "Pillow >= 10.0.0",
    # Word document processing
    "python-docx >= 1.1.0",
    "mammoth >= 1.8.0",
    # PowerPoint processing
    "python-pptx >= 1.0.0",
    # RTF processing
    "striprtf >= 0.0.26",
    # Text encoding detection
    "chardet >= 5.2.0",
    # Excel and data formats
    "openpyxl >= 3.1.0",
    "xlsx2html >= 0.4.0",
    "markitdown >= 0.1.0",
    # XBRL processing
    "arelle-release >= 2.37.0",
    "tidyxbrl >= 1.2.0",
    # Caching and performance
    "redis[hiredis] >= 5.3.0",
    "cachetools >= 5.3.0",
    # Console output and CLI
    "rich>=13.7.0",
    "typer >= 0.15.0",
    "prompt_toolkit >= 3.0.0",
    "colorama >= 0.4.0",
    "termcolor >= 2.3.0",
    # Progress and utilities
    "tqdm >= 4.66.0",
    "psutil >= 5.9.0",
    "tabulate >= 0.9.0",
    "structlog >= 23.0.0",
    # Networking and scraping
    "scrapling >= 0.2.0",
    "sec-cik-mapper >= 2.1.0",
    # Machine learning and AI
    "torch >= 2.1.0",
    "transformers >= 4.35.0",
    "aisuite[all] >= 0.1.0",
    # Development and code quality
    "ruff>=0.9.0",
    "mypy >= 1.7.0",
    # Monitoring and profiling
    "yappi >= 1.4.0",
    "nvidia-ml-py3 >= 7.352.0",
    # Integration and protocols
    "mcp[cli] >= 1.5.0",
    "google-genai",
    "tiktoken",
    "scipy>=1.15.3",
    "numpy>=2.2.6",
    "cryptography>=45.0.3",
    "pyyaml>=6.0.2",
    "watchdog>=6.0.0",
    "pytrends",
    "pandas-ta>=0.3.14b0",
    "scikit-learn",
    "statsmodels",
    "backtesting",
    "defusedxml",
    "ciso8601",
    "holidays",
    "matplotlib>=3.5.0",
    "seaborn>=0.11.0",
    "plotly>=5.0.0",
    "networkx",
    "authlib>=1.5.2",
    "jinja2>=3.1.6",
    "itsdangerous>=2.2.0",
    "openai",
    "elasticsearch>=9.0.0,<10.0.0",
    "pyjwt",
    "httpx-oauth",
    "arelle>=2.2",
    "alembic",
    "brotli>=1.1.0",
    "psycopg2-binary>=2.9.10",
    "sqlalchemy-utils>=0.41.2",
    "pgcli>=4.3.0",
    "asyncpg>=0.30.0",
    "user-agents>=2.2.0",
    "types-aiofiles>=24.1.0.20250606",
    "types-pyyaml>=6.0.12.20250516",
    "types-cachetools>=6.0.0.20250525",
    "orjson>=3.11.2,<4",
    "opentelemetry-instrumentation-starlette>=0.45",
    "testcontainers>=4.0",
]

[project.optional-dependencies]
# Heavy ML dependencies (optional for basic functionality)
ml = [
    "ray >= 2.40.0",
    "flashinfer-python < 0.2.3",
]

# Interactive tools
interactive = [
    "streamlit >= 1.22.0",
    "ipython >= 8.0.0",
]

# Development dependencies
dev = [
    "pytest >= 7.4.0",
    "pytest-asyncio >= 0.21.0",
    "pytest-cov >= 4.1.0",
    "black >= 23.0.0",
    "pre-commit >= 3.0.0",
]

# All optional dependencies
all = [
    "smartedgar[ml,interactive,dev]",
]

[project.scripts]
smartedgar = "smartedgar.cli.main:main"

[project.urls]
Homepage = "https://github.com/Dicklesworthstone/smartedgar"
Repository = "https://github.com/Dicklesworthstone/smartedgar"
Issues = "https://github.com/Dicklesworthstone/smartedgar/issues"
Documentation = "https://github.com/Dicklesworthstone/smartedgar#readme"

# Configure Hatchling
[tool.hatch.build.targets.wheel]
packages = ["smartedgar"]

[tool.hatch.build.targets.sdist]
exclude = [
    "/.venv",
    "/.vscode", 
    "/.git",
    "/.github",
    "/__pycache__",
    "/*.pyc",
    "/*.pyo", 
    "/*.pyd",
    "*.db",
    "*.db-journal",
    "*.db-wal",
    "*.db-shm",
    ".env",
    "tests/*",
    "docs/*", 
    "*.log",
    "sec_filings/*",
    "logs/*",
    "old_code/*",
    "*.gz",
    ".DS_Store",
    "cache.db",
    "fonts/*",
    "static/*.html",
]

# --- Tool Configurations ---

[tool.ruff]
line-length = 150
target-version = "py313"

[tool.ruff.lint]
select = [ "E", "W", "F", "I", "C4", "B", "A", "RUF", "ASYNC", "FA", "SIM", "TID", "PTH", "RUF100" ]
extend-select = [ "A005", "A006", "FURB188", "PLR1716", "RUF032", "RUF033", "RUF034" ]
ignore = [ "E501", "E402", "B008", "B007", "A003", "SIM108", "W293", "RUF003" ]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"
docstring-code-format = true
line-ending = "lf"
skip-magic-trailing-comma = false
docstring-code-line-length = "dynamic"

[tool.ruff.lint.isort]
known-first-party = ["smartedgar"]
combine-as-imports = true

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["S101", "I001"]
"old_code/*" = ["E", "W", "F", "I", "C4", "B", "A", "RUF"]

[tool.mypy]
python_version = "3.13"
warn_return_any = true
warn_unused_configs = true
ignore_missing_imports = true
strict_optional = true
disallow_untyped_defs = false
disallow_incomplete_defs = true
check_untyped_defs = true
no_implicit_optional = true
sqlite_cache = true
cache_fine_grained = true
incremental = true

[[tool.mypy.overrides]]
module = "streamlit.*"
ignore_missing_imports = true

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = [ "--strict-markers", "--cov=smartedgar", "--cov-report=term-missing", "--cov-report=html" ]

[tool.coverage.run]
source = ["smartedgar"]
omit = ["tests/*", "*/conftest.py", "old_code/*"]

[tool.coverage.report]
exclude_lines = [ "pragma: no cover", "def __repr__", "raise AssertionError", "raise NotImplementedError", "if __name__ == .__main__.:", "if TYPE_CHECKING:" ]

[tool.uv]
[dependency-groups]
dev = [ "types-aiofiles>=24.1.0.20250606", "types-cachetools>=6.0.0.20250525", "types-python-dateutil>=2.9.0.20250516", "types-pyyaml>=6.0.12.20250516" ]

# ---------------------------------------------------------------
# Installation Instructions with uv:
# ---------------------------------------------------------------
# 1. Create virtual environment: uv venv --python 3.13
# 2. Activate: source .venv/bin/activate
# 3. Install: uv sync --all-extras
# 4. Set up environment: cp .env.example .env && # Edit .env
# 5. Initialize system: smartedgar setup
# 6. Run MCP server: smartedgar run-server
#    # Or for HTTP transport: smartedgar run-server --transport http
# 7. Other commands: smartedgar --help
# ---------------------------------------------------------------
```

## Advanced uv Features

**Leverage uv's tool functionality** to install and run linters/formatters in isolation without polluting your app's virtual environment:

```bash
uv tool install ruff
uv tool run ruff check .
```

**For monorepo projects**, uv provides workspace management capabilities. It supports constraint dependencies and override dependencies for complex dependency scenarios.
• **Workspace initialization**: Use `uv workspace init` to create a Cargo-style workspace for multi-package monorepos, allowing shared dependencies and coordinated builds across packages.
• **Free-threaded Python**: Install GIL-less CPython with `uv python install 3.13.0-ft` to experiment with true parallelism in CPU-bound workloads.

## Project Structure for Scale

When your application grows, use **domain-based organization** and FastMCP's composition features:

```
smartedgar/
├── main.py              # Main FastMCP server, mounts other servers
├── cli.py               # Typer CLI application
├── filings/
│   ├── server.py        # Filings FastMCP sub-server
│   ├── models.py        # SQLModel definitions
│   └── service.py       # Business logic
├── analytics/
│   ├── server.py        # Analytics FastMCP sub-server
│   ├── models.py
│   └── service.py
└── common/
    └── dependencies.py  # Shared dependencies
```
Each domain package contains its own `server.py` defining a modular `FastMCP` instance. The main server in `main.py` then uses `mcp.mount()` to compose them into a single, unified server.

## FastMCP Performance & Best Practices

**Use lifespan context manager** for robust startup/shutdown logic (v2.0.0+):

```python
from contextlib import asynccontextmanager
from fastmcp import FastMCP

@asynccontextmanager
async def lifespan(app: FastMCP):
    # Startup
    await connect_to_database()
    await initialize_redis_pool()
    yield
    # Shutdown
    await disconnect_from_database()
    await close_redis_pool()

mcp = FastMCP(name="MyServer", lifespan=lifespan)
```

**Use FastMCP Middleware** for cross-cutting concerns instead of raw ASGI middleware. It's aware of the MCP request lifecycle.

```python
from fastmcp.server.middleware import Middleware, MiddlewareContext

class TimingMiddleware(Middleware):
    async def on_request(self, context: MiddlewareContext, call_next):
        start_time = time.time()
        result = await call_next(context)
        process_time = time.time() - start_time
        # Use ctx.info for logging back to the client if needed
        # Or log to console/structured logging system
        return result

mcp.add_middleware(TimingMiddleware())
```

**Control Tool Output with `ToolResult`** for optimal client communication. Return both human-readable `content` and machine-readable `structured_content`.

```python
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

@mcp.tool
def get_analysis() -> ToolResult:
    """Provides a detailed analysis."""
    structured_data = {"metric": 42, "status": "complete"}
    human_summary = "Analysis complete. The key metric is 42."
    
    return ToolResult(
        content=[TextContent(text=human_summary)],
        structured_content=structured_data
    )
```

**Streaming AI responses** and long-running tasks should use `ctx.report_progress`:

```python
@mcp.tool
async def generate_report(ctx: Context, topic: str):
    await ctx.report_progress(progress=0, message="Starting report generation...")
    # ... stream from AI API, updating progress ...
    async for chunk in ai_client.stream(topic):
        # Process chunk...
        await ctx.report_progress(progress=0.5, message=f"Generated section: {chunk.title}")
    await ctx.report_progress(progress=1.0, message="Report finished.")
    return "Report generation complete."
```

## Compression & JSON

**Use ORJSON for JSON responses and Brotli for compression**. Set FastAPI's default response class to `ORJSONResponse` (we already depend on `orjson`) and add a Brotli compression middleware for large JSON payloads. Brotli is a drop-in ASGI layer for Starlette/FastAPI and typically outperforms gzip at similar CPU.

```python
# smartedgar/main.py (HTTP parent app example)
from fastapi import FastAPI
from fastapi.responses import ORJSONResponse
from fastmcp import FastMCP
from brotli_asgi import BrotliMiddleware  # pip name: brotli-asgi

# ... your mcp instance ...
mcp = FastMCP(name="SmartEdgarServer", lifespan=...)

# Parent FastAPI app wraps the MCP HTTP app
app = FastAPI(default_response_class=ORJSONResponse, lifespan=mcp.http_app().lifespan)

# Add Brotli compression for large responses (fallbacks gracefully if client doesn't support it)
app.add_middleware(BrotliMiddleware, quality=5, minimum_size=8_192)

# Mount the MCP server at a sub-path
app.mount("/mcp", mcp.http_app())
```

Dependency note: add `brotli-asgi` to your dependencies for the middleware (we already include `orjson`).

## Advanced Database Patterns

**Use SQLModel's async-first patterns** (v0.0.15+):

```python
from sqlmodel import SQLModel, create_engine
from sqlmodel.ext.asyncio.session import AsyncSession, AsyncEngine
from sqlalchemy.ext.asyncio import create_async_engine

engine = AsyncEngine(create_async_engine(
    "postgresql+asyncpg://user:pass@localhost/db",
    echo=False,
    future=True
))

async def get_session() -> AsyncSession:
    async with AsyncSession(engine) as session:
        yield session
```

**Configure connection pool** for optimal async performance:

```python
from sqlalchemy.pool import AsyncAdaptedQueuePool

engine = create_async_engine(
    "postgresql+asyncpg://...",
    poolclass=AsyncAdaptedQueuePool,
    pool_size=20, max_overflow=10, pool_pre_ping=True, pool_recycle=3600,
    query_cache_size=1200,
    connect_args={
        "server_settings": { "application_name": "smartedgar", "jit": "off" },
        "command_timeout": 60, "statement_cache_size": 256,
    }
)
```

**Use PostgreSQL COPY for bulk operations** achieving 100,000-500,000 rows/second:

```python
async def bulk_insert_with_copy(df: pd.DataFrame, table_name: str):
    from io import StringIO
    import csv
    
    buffer = StringIO()
    df.to_csv(buffer, index=False, header=False, quoting=csv.QUOTE_MINIMAL)
    buffer.seek(0)
    
    async with engine.raw_connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.copy_expert(f"COPY {table_name} FROM STDIN WITH CSV", buffer)
```

**For bulk updates, use the UNNEST pattern**:

```python
async def bulk_update_filings(updates: list[dict]):
    stmt = text("""
        UPDATE filings SET status = updates.status
        FROM (SELECT * FROM UNNEST(:ids::integer[], :statuses::text[])) AS t(id, status)
        WHERE filings.id = updates.id
    """)
    await session.execute(stmt, {"ids": [u["id"] for u in updates], "statuses": [u["status"] for u in updates]})
```

**Leverage PostgreSQL's JSON capabilities** for complex aggregations:

```python
query = text("""
    SELECT json_build_object('company', c.name, 'filings', json_agg(...)) as data
    FROM companies c LEFT JOIN filings f ON c.id = f.company_id GROUP BY c.id
""")
```

## Redis Advanced Patterns

**Note: redis-py now includes async support** (aioredis is deprecated):

```python
from redis import asyncio as redis

class RedisClient:
    def __init__(self):
        self.pool = redis.ConnectionPool(host="localhost", max_connections=50)
    
    async def get(self, key: str):
        try:
            async with redis.Redis(connection_pool=self.pool) as r:
                return await r.get(key)
        except redis.RedisError:
            raise
```

**Use Lua scripts for atomic operations** like advanced rate limiting:

```python
rate_limit_script = """
local key, limit, window, now = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window * 1000000)
if redis.call('ZCARD', key) < limit then
    redis.call('ZADD', key, now, now)
    redis.call('EXPIRE', key, window)
    return 1
else
    return 0
end
"""
```

**Leverage Redis Streams** for event-driven architectures:

```python
async def publish_event(stream: str, event: dict):
    async with redis.Redis(connection_pool=redis_pool) as r:
        await r.xadd(stream, {"data": json.dumps(event)})
```

## Rich Console Advanced Features

**Optimize Live displays** with appropriate refresh rates:

```python
from rich.live import Live
console = Console(width=120)

with Live(table, refresh_per_second=4, transient=True, console=console) as live:
    for update in data_stream:
        table.add_row(update)
        live.update(table)
```

**Memory-efficient rendering** for large datasets using pagination:

```python
from rich.console import Console

console = Console()
with console.pager():
    for line in massive_dataset:
        console.print(line)
```

## Integration Testing Patterns

**Use TestContainers v4.0+** for real service testing:

```python
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer
import pytest
```

**Use FastMCP's in-memory transport for testing**, which is significantly faster and simpler than HTTP testing.

```python
import pytest
from fastmcp import Client
from my_mcp_server import mcp as server_instance # Import your server

@pytest.fixture
async def mcp_client():
    # Pass the server instance directly to the client for in-memory testing
    async with Client(server_instance) as client:
        yield client

@pytest.mark.asyncio
async def test_my_tool(mcp_client: Client):
    result = await mcp_client.call_tool("my_tool_name", {"arg": "value"})
    assert result.data == "expected_output"
```

**Parallel test execution** with pytest-xdist:

```bash
pytest -n 4 --dist loadscope
```

## AI API Token Management

**Provider-specific token counting**:

```python
encoding_gpt4o = tiktoken.encoding_for_model("gpt-4o")
# ... other tokenizers ...
```

**Request coalescing** for similar queries:

```python
class AIRequestCoalescer:
    # ... implementation remains the same ...
```

## Production Deployment

**Gunicorn configuration** with uvicorn workers for HTTP transport:

```python
# gunicorn.conf.py
import multiprocessing

bind = "0.0.0.0:8007"
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
max_requests = 1000
max_requests_jitter = 50
graceful_timeout = 30
timeout = 60
accesslog = "-"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'
```

**Expose the ASGI app** for Gunicorn in your server file:

```python
# smartedgar/main.py
from fastmcp import FastMCP
# ... mcp server definition ...
mcp = FastMCP(name="SmartEdgarServer", lifespan=...)
# ... tool definitions ...

# This 'app' object is what Gunicorn will run
app = mcp.http_app()
```

**Health check endpoints**: Mount your FastMCP server into a minimal FastAPI app that provides health checks.

```python
# smartedgar/main.py
from fastapi import FastAPI
from fastmcp import FastMCP
# ... your mcp instance ...

# Create the parent FastAPI app, passing the lifespan from the MCP app
app = FastAPI(lifespan=mcp.http_app().lifespan)

# Mount the MCP server at a sub-path
app.mount("/mcp", mcp.http_app())

@app.get("/health/liveness")
async def liveness():
    return {"status": "alive"}

@app.get("/health/readiness")
async def readiness(...):
    # ... check dependencies (DB, Redis, etc.) ...
    return {"status": "ready", "checks": ...}
```

## Dockerization

**Multi-stage build** for lean production images:

```dockerfile
# Dockerfile
FROM python:3.13-slim-bookworm as builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=off UV_SYSTEM_PYTHON=true
WORKDIR /app
RUN pip install uv
COPY pyproject.toml uv.lock* ./
RUN uv venv .venv && source .venv/bin/activate && uv sync --all-extras

FROM python:3.13-slim-bookworm as final
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN addgroup --system app && adduser --system --group app
USER app
COPY --from=builder /app/.venv ./.venv
COPY ./smartedgar ./smartedgar
COPY gunicorn.conf.py .
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8007
# The command points to the ASGI app object in your main server file
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "gunicorn.conf.py", "smartedgar.main:app"]
```

## CI/CD with GitHub Actions

**Automated testing and linting** for every commit (remains the same):

```yaml
# .github/workflows/ci.yml
name: CI Pipeline
on: [push, pull_request]
jobs:
  test-and-lint:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: ${{ matrix.python-version }} }
      - uses: actions/cache@v3
        with: { path: | ~/.cache/uv \n .venv, key: ... }
      - run: pip install uv
      - run: uv sync --all-extras
      - run: uvx ruff check .
      - run: uvx ruff format . --check
      - run: uv run mypy .
      - run: uv run pytest
      - run: uv run pip-audit
```

## Monitoring and Observability

**Structured logging with correlation IDs**: Continue using `structlog` + `asgi-correlation-id` (works with any ASGI app, including FastMCP's HTTP transport).

```python
from asgi_correlation_id import CorrelationIdMiddleware
import structlog
# ... server setup ...
app = mcp.http_app()
app.add_middleware(CorrelationIdMiddleware) # Add to the ASGI app
```

**Prometheus metrics integration**: Add middleware to the Starlette app returned by `mcp.http_app()`.

```python
# ... prometheus metrics definitions ...
app = mcp.http_app()

@app.middleware("http")
async def track_metrics(request: Request, call_next):
    # ... same metric tracking logic ...
    return response

@app.get("/metrics") # Add this route to the parent ASGI app
async def metrics():
    return Response(...)
```

### Error tracking (optional): Sentry

Sentry's Starlette/FastAPI hooks are trivial to enable in production. Use `python-decouple` to load the DSN and initialize once.

```python
from decouple import Config as DecoupleConfig, RepositoryEnv
import sentry_sdk
from sentry_sdk.integrations.starlette import StarletteIntegration

decouple_config = DecoupleConfig(RepositoryEnv(".env"))
SENTRY_DSN = decouple_config("SENTRY_DSN", default=None)

if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[StarletteIntegration()],
        traces_sample_rate=0.1,  # adjust to taste
    )
```

See documentation at `https://docs.sentry.io`.

## FastMCP Server Composition & Modularity

**Reduce complexity with server composition**, not router-level dependencies. Mount specialized servers onto a main server.

```python
# Main server
main_mcp = FastMCP(name="MainServer")

# Create a sub-server for admin tools
admin_mcp = FastMCP(name="AdminTools")
# Add bearer token auth provider only to this server
# mcp.auth = BearerAuthProvider(...)

@admin_mcp.tool
def sensitive_operation(): ...

# Mount the admin server with a prefix and its own auth
main_mcp.mount(admin_mcp, prefix="admin")
```

## Service Layer Architecture Pattern

**Implement clean separation of concerns** beyond simple tool definitions:

```python
# services/filing_service.py - Business logic layer
class FilingService:
    async def process_filing(self, filing_id: int) -> Filing:
        # ... complex business logic ...

# servers/filings_server.py - MCP Presentation layer
from services.filing_service import FilingService

mcp = FastMCP(name="FilingServer")

@mcp.tool
async def process_filing_tool(filing_id: int):
    # Tool only handles MCP concerns (I/O, validation)
    service = FilingService(...) # Dependency injection
    result = await service.process_filing(filing_id)
    # Return a ToolResult or Pydantic model
    return result
```

## Advanced Async Patterns

**Structured concurrency with asyncio.TaskGroup** is ideal for tools that orchestrate multiple async operations.

```python
@mcp.tool
async def process_many_items(items: list) -> list:
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(process_item(item)) for item in items]
    return [task.result() for task in tasks]
```

## Rate-limiting, retries, parallelism: one composable pattern

```python
# smartedgar/net.py
import asyncio, random
from aiolimiter import AsyncLimiter
import httpx
from tenacity import retry, wait_exponential_jitter, stop_after_attempt

limiter = AsyncLimiter(max_rate=50, time_period=1.0)

@retry(wait=wait_exponential_jitter(initial=0.25, max=8), stop=stop_after_attempt(6))
async def fetch_json(client: httpx.AsyncClient, url: str, **kw):
    async with limiter:
        r = await client.get(url, timeout=kw.pop("timeout", 30))
        r.raise_for_status()
        return r.json()

async def fetch_many(urls: list[str]):
    async with httpx.AsyncClient(http2=True) as client:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(fetch_json(client, u)) for u in urls]
        return [t.result() for t in tasks]
```

- Semaphore/rate-limit is outside the retry boundary (prevents dog-piles).
- `TaskGroup` provides structured concurrency and clean cancellation.

## OpenTelemetry Distributed Tracing

Prefer installing `opentelemetry-distro` and one exporter once, then instrument Starlette, SQLAlchemy, and Redis. Auto-instrumentation for Starlette (the foundation of FastMCP's HTTP transport) provides distributed traces.

```python
from opentelemetry.instrumentation.starlette import StarletteInstrumentor
# ... other instrumentors ...

def setup_telemetry(app: FastAPI):
    # ... setup provider and exporter ...
    StarletteInstrumentor.instrument_app(app, tracer_provider=provider)
    # ... instrument SQLAlchemy, Redis, etc. ...

# Instrument the ASGI app, not the mcp object
app = mcp.http_app()
setup_telemetry(app)
```

## Production Deployment Architecture

The three-layer architecture (Nginx -> Gunicorn -> FastMCP/Uvicorn) remains the gold standard.

```nginx
# nginx.conf
upstream app_servers {
    server unix:/tmp/gunicorn.sock fail_timeout=0;
}
server {
    listen 443 ssl http2;
    # ... same config, points to Gunicorn socket ...
}
```

**systemd service** for production:
```ini
# /etc/systemd/system/smartedgar.service
[Unit]
Description=SmartEdgar MCP Server
After=network.target postgresql.service redis.service

[Service]
# ... user, group, working directory ...
ExecStart=/opt/smartedgar/.venv/bin/gunicorn \
    -c /opt/smartedgar/gunicorn.conf.py \
    smartedgar.main:app
# ... restart policy, security ...

[Install]
WantedBy=multi-user.target
```

## Advanced LLM Development Patterns

**Context management for LLM-assisted coding** remains a critical best practice, especially when building tools *for* LLMs.

```python
class LLMContext:
    # ... implementation remains the same ...
```

**Multi-model orchestration** can be implemented in a service layer called by your FastMCP tools.

```python
class MultiModelOrchestrator:
    # ... implementation remains the same ...
```

## Documentation Workflow (mid-2025)

**Use MkDocs ≥ 1.6 with material theme and `mkdocstrings`** to document not just your API, but your entire system architecture.

**Architectural Decision Records (ADRs)** are crucial for codifying technical choices, especially in a rapidly evolving field like AI engineering.

This comprehensive approach ensures your FastMCP application is robust, scalable, maintainable, and secure, following the most modern Python development practices for mid-2025.