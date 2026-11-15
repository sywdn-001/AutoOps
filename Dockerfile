# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# AutoOps Bastion - Linux container image
#
# Two stages: the web UI is built with Node, the runtime image only carries
# Python + the Flask backend + the built SPA. Base images are Debian-based
# ("slim"), so the resulting image is linux/amd64 (and arm64, same Dockerfile).
#
#   docker build -t autoops-bastion:latest .
#   docker run -d --name autoops -p 5000:5000 -p 2222:2222 \
#       -v autoops-data:/app/data autoops-bastion:latest
#
# Ports   5000 = web UI + REST, 2222 = SSH gateway, 4489 = optional RDP module
# Data    /app/data   (mount it, or every new container starts empty)
#
# Notes
#   * The Windows-only features (WinRM targets, the ComputerUse host agent and
#     the Rust RDP gateway module) are not available inside this image; SSH
#     assets, policies, auditing, replay and the web UI all work normally.
# ---------------------------------------------------------------------------

ARG NODE_TAG=22-bookworm-slim
ARG PYTHON_TAG=3.13-slim-bookworm

# ===========================================================================
# stage 1 - build the web UI (ant-design-pro)
# ===========================================================================
FROM node:${NODE_TAG} AS frontend
WORKDIR /fe

COPY ant-design-pro/package.json ant-design-pro/package-lock.json ./
# `npm ci` is the strict replay; the committed lock file lacks a few
# platform-specific optional entries (lightningcss / esbuild binaries), which
# npm 10 rejects on Linux, so fall back to a normal install in that case.
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY ant-design-pro/ ./
RUN npm run build && test -f /fe/dist/index.html

# ===========================================================================
# stage 2 - runtime: python + Flask backend + built web UI
# ===========================================================================
FROM python:${PYTHON_TAG} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai

WORKDIR /app

# Python dependencies first, so source edits do not re-run pip.
COPY bastion-backend/requirements.txt bastion-backend/requirements.txt
RUN pip install --no-cache-dir -r bastion-backend/requirements.txt

# Application source + the web UI produced by stage 1 + container helpers.
COPY bastion-backend/ bastion-backend/
COPY --from=frontend /fe/dist ant-design-pro/dist
COPY docker/ docker/

RUN mkdir -p /app/data/instance && chmod +x /app/docker/entrypoint.sh

# Container defaults. Every value can be overridden with `docker run -e ...`.
ENV BASTION_INSTANCE_DIR=/app/data/instance \
    BASTION_HOST=0.0.0.0 \
    BASTION_PORT=5000 \
    BASTION_GATEWAY_ENABLED=1 \
    BASTION_GATEWAY_HOST=0.0.0.0 \
    BASTION_GATEWAY_PORT=2222 \
    BASTION_SERVE_FRONTEND=1 \
    BASTION_FRONTEND_DIST=/app/ant-design-pro/dist

EXPOSE 5000 2222 4489
VOLUME /app/data

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/api/health', timeout=5).status == 200 else 1)"

ENTRYPOINT ["/app/docker/entrypoint.sh"]
