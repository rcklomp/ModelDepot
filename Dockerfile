# Multi-arch shim image (amd64 + arm64). Build with:
#   docker buildx build --platform linux/amd64,linux/arm64 -t llm-repo:dev .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml README.md ./
COPY server ./server
COPY ingester ./ingester
RUN pip install --no-cache-dir .

# Default mirror root inside the container; override via bind mount in compose.
ENV MIRROR_ROOT=/data/mirror-root \
    MIRROR_HOST=0.0.0.0 \
    MIRROR_PORT=8000

VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
