# One image for the whole application: the Hugging Face-compatible server, the GUI and the download worker.
# Built for the owner's Synology DS713+ (x86-64 Intel Atom, 4 GB RAM, an old kernel):
#   docker build --platform linux/amd64 -t modeldepot:latest .
#
# - plain `uvicorn` (pure Python), not `uvicorn[standard]`: no uvloop / httptools, nothing built for a newer CPU than an Atom has;
# - hf-xet (a Rust wheel the Hugging Face client pulls in) is removed: the client is told not to use it (HF_HUB_DISABLE_XET) and it is
#   the one component that could be built for instructions this CPU lacks;
# - tzdata, because the download window ("01:00-07:00") is local time and TZ means nothing without it.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_XET=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    MIRROR_ROOT=/data

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY server ./server
COPY ingester ./ingester
RUN pip install . && (pip uninstall -y hf-xet || true)

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
