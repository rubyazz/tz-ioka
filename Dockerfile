FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# 1) Install third-party dependencies from pyproject (cached layer)
COPY pyproject.toml ./
RUN python - <<'EOF'
import subprocess, sys, tomllib
deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
subprocess.run([sys.executable, "-m", "pip", "install", *deps], check=True)
EOF

# 2) Install the application package itself (no deps — already installed)
COPY app ./app
COPY scripts ./scripts
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-deps .

# Non-root runtime user; /data holds generated tickets (shared volume)
RUN useradd --system --create-home appuser \
    && mkdir -p /data/tickets \
    && chown -R appuser:appuser /data \
    && chown -R appuser:appuser /srv/alembic
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/health/live || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
