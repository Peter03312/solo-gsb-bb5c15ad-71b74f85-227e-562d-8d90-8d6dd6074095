FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TIDE_DB_PATH=/data/tide.db

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Persistent SQLite location; created and schema-initialised on startup.
RUN mkdir -p /data && chown -R nobody:nogroup /data /app
USER nobody

VOLUME ["/data"]

EXPOSE 8000

# Application startup: uvicorn serves the app whose lifespan initialises
# the SQLite database (file + table) before accepting requests.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
