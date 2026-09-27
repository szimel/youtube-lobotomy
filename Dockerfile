# syntax=docker/dockerfile:1

# yt-dlp solves YouTube's JavaScript challenges with an external runtime (the
# "js_runtimes" option in fetcher.py), and it requires Node 22 or newer -- a
# version Debian does not ship yet. Copying the binary out of the official image
# avoids adding a third-party apt repository to the build.
FROM node:24-bookworm-slim AS node

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

# gosu drops root after the data volume has been made writable; tini forwards
# signals and reaps the JavaScript runtime yt-dlp spawns.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        gosu \
        libstdc++6 \
        tini; \
    rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node

WORKDIR /app

# Dependencies first, so editing the application does not reinstall them.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app.py fetcher.py ./
COPY templates/ ./templates/
COPY static/ ./static/
COPY tests/ ./tests/
COPY docker/ ./docker/
RUN chmod 0755 /app/docker/entrypoint.sh

# Everything the app writes lives under /app/data: the feed, the rulebook, the
# transcript cache, the run history and the exported cookies. Creating it here
# means a named volume inherits an ownership the server can actually use.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin feed \
 && mkdir -p /app/data \
 && chown -R feed:feed /app

VOLUME ["/app/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/healthz', timeout=4)"

# The entrypoint starts as root only to fix up /app/data and then hands the
# server to the unprivileged account, so a bind mount works without the host
# directory having to be chowned by hand first.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["gunicorn", "--config", "/app/docker/gunicorn.conf.py", "app:app"]
