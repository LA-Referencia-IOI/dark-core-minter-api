FROM python:3.14-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Build context is expected to be components so dark-core-lib is available.
# Example:
# docker build -f services/dark-core-minter-api/Dockerfile -t dark-core-minter-api .
COPY dark-core-minter-api/requirements.txt /tmp/requirements.txt
COPY dark-core-lib /tmp/dark-core-lib
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir /tmp/dark-core-lib

# Copy application and alembic
COPY dark-core-minter-api/app/ ./app/
COPY dark-core-minter-api/alembic/ ./alembic/
COPY dark-core-minter-api/alembic.ini .
COPY dark-core-minter-api/docker-entrypoint.sh /usr/local/bin/minter-entrypoint

# Create non-root user and set ownership
RUN useradd -m appuser \
    && chown -R appuser:appuser /app \
    && chmod +x /usr/local/bin/minter-entrypoint

# Environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV DATABASE_URL=postgresql://dark:dark_password@postgres:5432/minter

# Expose port
EXPOSE 8001

ENTRYPOINT ["minter-entrypoint"]

# Default command runs API only (worker runs in separate process/service).
CMD ["api"]
