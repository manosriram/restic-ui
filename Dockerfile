# Use a slim Python base image
FROM python:3.12-slim

# Install system dependencies needed by restic and curl (for healthcheck)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fuse \
        wget \
        bzip2 && \
    rm -rf /var/lib/apt/lists/*

# Install restic binary
# You can adjust RESTIC_VERSION as needed.
ENV RESTIC_VERSION=0.16.4
RUN ARCH="$(uname -m)"; \
    case "$ARCH" in \
      x86_64) RESTIC_ARCH=amd64 ;; \
      aarch64) RESTIC_ARCH=arm64 ;; \
      armv7l) RESTIC_ARCH=arm ;; \
      *) echo "Unsupported architecture: $ARCH" && exit 1 ;; \
    esac && \
    wget -O /tmp/restic.bz2 \
      "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${RESTIC_ARCH}.bz2" && \
    bunzip2 /tmp/restic.bz2 && \
    mv /tmp/restic /usr/local/bin/restic && \
    chmod +x /usr/local/bin/restic

# Set workdir
WORKDIR /app

# Copy dependency metadata first for better build caching
COPY pyproject.toml uv.lock ./

# Install uv and project dependencies into the system environment
RUN pip install --no-cache-dir uv && \
    uv pip install --system .

# Copy the rest of the application code
COPY . .

# Environment defaults (can be overridden in docker-compose.yml)
ENV FLASK_ENV=production \
    RESTIC_UI_SNAPSHOTS_PER_PAGE=20 \
    RESTIC_UI_RESTIC_TIMEOUT=30 \
    RESTIC_UI_RESTORE_TIMEOUT=3600 \
    PYTHONUNBUFFERED=1

# Expose the port gunicorn will listen on
EXPOSE 8000

# Use gunicorn to serve the Flask app
# Assumes the Flask app instance is named "app" in app.py
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8000", "app:app"]
