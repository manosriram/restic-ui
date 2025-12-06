# Use a slim Python base image
FROM python:3.12-slim

# Install restic and any needed OS tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        restic \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy project metadata first (for better Docker layer caching)
COPY pyproject.toml uv.lock ./

# Install uv (fast Python package/dependency manager)
RUN pip install --no-cache-dir uv Flask

# Install dependencies using the lockfile
RUN uv sync --frozen --no-dev

# Copy the rest of the application source
COPY . .

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV FLASK_ENV=production
ENV FLASK_APP=app.py
# Default restic repo location inside container; can be overridden
# ENV RESTIC_REPOSITORY=s3:s3.eu-central-003.backblazeb2.com/mano-homelab-backup
# ENV RESTIC_PASSWORD_FILE=/etc/restic-password

# Create directory for restic repository (can be backed by a volume)
# RUN mkdir -p /data/repo

# Expose the port the app listens on
EXPOSE 8000

# Default command: run the Flask app
CMD ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=8000"]
