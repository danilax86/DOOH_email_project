# Use Python 3.11 slim image for better compatibility with pandas
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies including curl for healthcheck
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire application
COPY . .

# Create necessary directories
RUN mkdir -p app/data
RUN mkdir -p app/data/sessions

# Set environment variables for production
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1
ENV SECRET_KEY="replace_with_a_long_random_secret"

# Expose port 7860 (required by Hugging Face Spaces)
EXPOSE 7860

# Create a non-root user for security
RUN useradd -m -u 1000 user
RUN chown -R user:user /app
USER user

# Use Gunicorn instead of Flask dev server
CMD ["gunicorn", "run:app", "--bind", "0.0.0.0:7860", "--workers", "1"]