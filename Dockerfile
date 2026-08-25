# Slim keeps the image small; the full image adds ~700MB of build tools we
# do not need, because psycopg[binary] ships prebuilt wheels.
FROM python:3.12-slim

# Unbuffered: this app reports everything it does through print(). With
# buffering on (Python's default when stdout is not a terminal) that output
# would sit in a buffer and `docker compose logs` would look frozen.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies are copied and installed BEFORE the source, on purpose.
# Docker caches layers: this way, editing main.py rebuilds in a second
# instead of reinstalling SQLAlchemy every time.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a normal user. Nothing here needs root, and if the app is ever
# compromised it should not own the filesystem.
RUN useradd --create-home --uid 10001 builddoctor \
    && mkdir -p /app/logs \
    && chown -R builddoctor:builddoctor /app
USER builddoctor

# Documentation only - it does not publish anything. docker-compose.yml
# does the actual publishing.
EXPOSE 8000

# --host 0.0.0.0 is mandatory inside a container. 127.0.0.1 would mean
# "this container's own loopback", which Docker's port mapping can never
# reach, and the app would appear dead from outside.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
