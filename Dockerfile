# Multi-stage: dependencies are built once in a throwaway layer so the final
# image carries no compilers and no build cache.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

# Run unprivileged: this process holds live session cookies.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY pyproject.toml README.md ./

# The queryId cache is written at runtime and must be writable by appuser.
RUN mkdir -p /app/data && chown -R appuser:appuser /app
ENV QUERY_ID_FILE=/app/data/query_ids.json

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
    sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/healthz', timeout=4).status==200 else 1)"

# Shell form so $PORT is expanded — Render and Fly inject the port at runtime.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers
