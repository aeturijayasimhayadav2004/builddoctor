# STAGE 1 OF 2 - the dashboard bundle (Phase 12).
#
# The React app used to be a separate container serving a separate origin.
# It is built here instead, and the Python image below serves the result,
# because the dashboard's session is a COOKIE: a cookie set by the API's host
# is not sent on a request from a different site, and the workaround for that
# (SameSite=None, a third-party cookie) is the thing browsers are removing.
# One origin makes the problem disappear rather than managing it.
#
# A separate stage, so node and node_modules - several hundred megabytes -
# never appear in the final image. Only dist/ is copied across.
FROM node:24-alpine AS dashboard

WORKDIR /dashboard

# Dependencies first, source second, so editing a component does not reinstall
# the toolchain.
COPY frontend/package.json frontend/package-lock.json ./

# `npm install`, not `npm ci`: the lock file is generated on Windows and some
# dependencies ship a different prebuilt binary per platform, which ci refuses
# to resolve.
RUN npm install

COPY frontend/ ./

# `npm run build` is `tsc --noEmit && vite build`, so a type error fails the
# IMAGE BUILD. That is the point: the frontend's types are hand-written claims
# about the API's shape, and the moment a route stops matching them the deploy
# should stop too, rather than shipping a page that renders undefined.
RUN npm run build


# STAGE 2 OF 2 - the application.
#
# Slim keeps the image small; the full image adds ~700MB of build tools we
# do not need, because psycopg[binary] ships prebuilt wheels.
FROM python:3.12-slim

# Unbuffered: this app reports everything it does through print(). With
# buffering on (Python's default when stdout is not a terminal) that output
# would sit in a buffer and `docker compose logs` would look frozen.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Where the embedding model's weights live inside the image. Set BEFORE the
# download below so the download lands here, and still set at runtime so
# sentence-transformers looks in the same place instead of trying to fetch
# the model again.
#
# HF_HUB_OFFLINE=1 makes the build-time bake enforceable: at runtime the
# library is not allowed to touch the network at all. A missing weight then
# fails loudly and immediately, rather than quietly during an incident.
ENV SENTENCE_TRANSFORMERS_HOME=/opt/models \
    HF_HOME=/opt/models \
    HF_HUB_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

# PyTorch, CPU build, on its own line before everything else. Two reasons:
#
#  1. `pip install torch` from PyPI on Linux pulls the CUDA build and its
#     nvidia-* dependencies - several gigabytes of GPU runtime, on a
#     machine with no GPU. This index serves the CPU-only build instead.
#  2. It is the largest and least-changing dependency, so its own layer
#     means editing requirements.txt never reinstalls it.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch

# Dependencies are copied and installed BEFORE the source, on purpose.
# Docker caches layers: this way, editing main.py rebuilds in a second
# instead of reinstalling SQLAlchemy every time.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model's weights into the image AT BUILD TIME.
#
# Downloading on first use would make a smaller image and a worse product.
# BuildDoctor sits idle until a webhook arrives, so that is exactly the
# moment it would have to reach Hugging Face. A slow first request is bad;
# a first request that FAILS because a CDN was unreachable means the one
# build we exist to explain goes unexplained, and the evidence for why is
# a stack trace nobody is watching. ~90MB of image size, paid once at
# build, removes that failure mode completely.
RUN HF_HUB_OFFLINE=0 python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')" \
    && chmod -R a+rX /opt/models

COPY . .

# The built dashboard, from the node stage. main.py mounts this directory at
# /dashboard if it exists, and explains what to run if it does not - so a
# checkout without a build is a helpful message rather than a 404.
COPY --from=dashboard /dashboard/dist ./frontend/dist

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
