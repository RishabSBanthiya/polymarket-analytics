# OmniTrade multi-platform trading system
# Usage:
#   docker build -t omnitrade .
#   docker run --env-file .env omnitrade directional --exchange polymarket --paper

FROM python:3.11-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output for logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Create non-root user for runtime security
RUN groupadd --gid 1000 omnitrade && \
    useradd --uid 1000 --gid omnitrade --create-home omnitrade

WORKDIR /app

# Install system dependencies required by cryptography and web3
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        && rm -rf /var/lib/apt/lists/*

# ---------- dependency layer (cached unless pyproject.toml changes) ----------
FROM base AS deps

COPY pyproject.toml ./
# Install production dependencies only; pip reads pyproject.toml [project].dependencies
RUN pip install --no-cache-dir . && \
    pip install --no-cache-dir . 2>/dev/null; \
    pip install --no-cache-dir -e ".[dev]" 2>/dev/null || true

# ---------- application layer ------------------------------------------------
FROM base AS runtime

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application code
COPY . /app

# Create data directory for SQLite databases and set ownership
RUN mkdir -p /app/data && chown -R omnitrade:omnitrade /app

USER omnitrade

# Health check: verify Python can import the package
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import omnitrade" || exit 1

# Default entrypoint runs the bot CLI; pass bot_type and flags via CMD / docker run args
ENTRYPOINT ["python", "scripts/run_bot.py"]

# Default: show help
CMD ["--help"]
