FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (data/credentials/token are mounted at runtime).
COPY app ./app
COPY web ./web

EXPOSE 3000

CMD ["uvicorn", "web.server:app", "--host", "0.0.0.0", "--port", "3000"]
