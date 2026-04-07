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

ENV PORT=8080
EXPOSE 8080

# Gunicorn: 2 workers, 60s timeout (trade + tx signing can be slow)
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 60 polywatch-api:app"]
