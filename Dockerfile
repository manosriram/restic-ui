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

# Expose the port the app listens on
EXPOSE 8000

# Default command: run the Flask app
CMD ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=8000"]
