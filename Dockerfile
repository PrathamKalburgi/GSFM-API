FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# The geospatial wheels bundle GDAL and PROJ, so no system libraries are installed.
COPY pyproject.toml requirements.lock README.md alembic.ini ./
COPY app ./app
COPY alembic ./alembic
RUN pip install -c requirements.lock .

# Run as a non-root user; the default SQLite database lives in /app/data.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser /app/data
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; port = os.environ.get('PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health')"

# Apply migrations, then serve. Set DATABASE_URL to use PostgreSQL / Supabase instead.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-access-log"]
