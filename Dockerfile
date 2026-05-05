FROM arm64v8/python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 \
    can-utils \
    usbutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory
RUN mkdir -p /data

# Expose the Flask port
EXPOSE 5000

# Run with Gunicorn — single worker + threads for SSE support
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--timeout", "0", "run:app"]
