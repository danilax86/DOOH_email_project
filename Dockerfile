FROM python:3.11-slim

# Working directory
WORKDIR /app

# Install system dependencies including curl for healthcheck
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create necessary directories
RUN mkdir -p app/data
RUN mkdir -p app/data/sessions

# optional Set environment variables for production
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:5000/health || exit 1

# Create a non-root user
RUN useradd -m -u 1000 user
RUN chown -R user:user /app
USER user

# Run app with a threaded worker and bounded request timeout.
# This prevents a single hung request from blocking all traffic forever.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "run:app", "--worker-class", "gthread", "--workers", "1", "--threads", "8", "--timeout", "120", "--graceful-timeout", "30", "--keep-alive", "5", "--max-requests", "1000", "--max-requests-jitter", "100", "--log-level", "info"]
