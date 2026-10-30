# Stage 1: Build Python dependencies
# Set the base image using Python 3.14 and Debian Trixie (current stable)
FROM python:3.14-slim-trixie  as builder

# Version of the published scietex.vfd_service wheel to install.
# build_image.sh passes the version read from
# src/scietex/vfd_service/version.py, so the image and the PyPI artifact always
# match.
ARG VERSION

WORKDIR /app

# Install system dependencies (required for some Python packages)
RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create and activate virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install the released package from PyPI. The image is a distribution channel of
# the same tagged source that was published, so it does not build from the local
# checkout.
# Then slim the venv: pip/setuptools/wheel are build-time only, bytecode caches
# are regenerated on first import, and valkey-glide ships one _fast_response
# extension per supported interpreter (cp39..cp314, pypy) while only the running
# interpreter's is ever loaded.
RUN pip install --no-cache-dir -U pip && \
    pip install --no-cache-dir "scietex.vfd_service==${VERSION}" && \
    pip uninstall -y pip setuptools wheel && \
    find /opt/venv -name '*.pyc' -delete && \
    find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + && \
    find /opt/venv/lib/python3.14/site-packages/glide_shared -name '*.so' \
        ! -name '*cpython-314*' ! -name 'libglide_ffi.so' -delete

# Stage 2: Runtime image
FROM python:3.14-slim-trixie

WORKDIR /app

# Copy virtual env from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Run as non-root user. The service reaches drives over Modbus/TCP through a
# gateway, so it needs no serial devices and no privileged ports.
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app && \
    chmod -R 755 /app

# Configuration lives in a mounted volume so it survives container replacement.
# SCIETEX_CONFIG_DIR is honored by the framework's prepare_conf_dir; the service
# namespaces its files under <config-dir>/vfd/.
ENV SCIETEX_CONFIG_DIR=/config \
    SCIETEX_SERVICE_NAME=VFD \
    SCIETEX_LOGGING_LEVEL=INFO
RUN mkdir -p /config && chown appuser:appuser /config
VOLUME ["/config"]

USER appuser

# Run the app
CMD ["start-vfd-service"]
