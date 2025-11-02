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

# Create a non-root user
RUN useradd -m -u 1000 user
RUN chown -R user:user /app
USER user

# Run the app with gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "run:app", "--workers", "1", "--log-level", "debug", "--timeout", "0"]