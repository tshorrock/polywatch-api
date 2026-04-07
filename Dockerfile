FROM python:3.11-slim

WORKDIR /app

# System deps for building native wheels (web3, cryptography)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY polywatch-api.py .

# Railway injects PORT at runtime. Do NOT hardcode it with ENV.
# Gunicorn: 1 worker (low memory), 120s request timeout, 120s worker boot timeout.
# Shell form so $PORT is expanded at container start.
CMD gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --timeout 120 --graceful-timeout 30 --access-logfile - --error-logfile - polywatch-api:app
