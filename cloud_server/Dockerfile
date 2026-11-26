# syntax=docker/dockerfile:1
# Prism Cloud Config Server (棱镜浏览器多租户云下发服务端)
# Build context is the repository root (see docker-compose.yml: context: ..).

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src ./src
# Copy ONLY cloud_server source modules and static/seed assets explicitly so
# operator secrets (.env, *.key), databases (cloud_data/), or backups/ are never
# copied into the build context layer or packaged into /opt/venv.
COPY cloud_server/__init__.py \
     cloud_server/admin_ui.py \
     cloud_server/app.py \
     cloud_server/resolver.py \
     cloud_server/store.py \
     ./cloud_server/
COPY cloud_server/seed ./cloud_server/seed
COPY cloud_server/static ./cloud_server/static

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PRISM_CLOUD_HOST="0.0.0.0" \
    PRISM_CLOUD_PORT="8787" \
    PRISM_CLOUD_DB="/data/cloud_server.db"

RUN groupadd --gid 1000 prism && \
    useradd --uid 1000 --gid prism --create-home --shell /usr/sbin/nologin prism && \
    mkdir -p /data && \
    chown -R prism:prism /data

COPY --from=builder /opt/venv /opt/venv

WORKDIR /data
USER prism

VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3)" || exit 1

ENTRYPOINT ["prism-cloud-server"]
CMD ["--host", "0.0.0.0", "--port", "8787", "--db", "/data/cloud_server.db"]
