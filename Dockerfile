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
    PORT=8080 \
    IDLE_RESTART_SECONDS=600 \
    MALLOC_ARENA_MAX=2 \
    MALLOC_MMAP_THRESHOLD_=131072 \
    MALLOC_TRIM_THRESHOLD_=131072

# gosu drops root after the data volume has been made writable; tini forwards
# signals and reaps the JavaScript runtime yt-dlp spawns.
#
# The MALLOC_* settings above are the difference between idling at 45 MB and at
# 165 MB after a refresh. Python's worker threads each get their own allocator
# arena, and glibc never returns a secondary arena's freed pages to the kernel,
# so a pipeline that parses megabytes of JSON across eight threads holds tens of
# megabytes it is no longer using. Capping the arenas at two and having large
# blocks mapped and trimmed directly keeps that memory out of the process (see
# "Measuring the footprint" in the README for the numbers).
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
RUN chmod 0755 /app/docker/entrypoint.sh /app/docker/healthcheck.sh

# Everything the app writes lives under /app/data: the feed, the rulebook, the
# transcript cache, the run history and the exported cookies. Creating it here
# means a named volume inherits an ownership the server can actually use.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin feed \
 && mkdir -p /app/data \
 && chown -R feed:feed /app

VOLUME ["/app/data"]
EXPOSE 8080

# Checked once a minute: this container is expected to sit idle for days, and
# nothing acts on the result except a human looking at `docker ps`. The script
# is bash rather than Python because the check outlives everything else here.
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD ["bash", "/app/docker/healthcheck.sh"]

# The entrypoint starts as root only to fix up /app/data and then hands the
# server to the unprivileged account, so a bind mount works without the host
# directory having to be chowned by hand first.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["gunicorn", "--config", "/app/docker/gunicorn.conf.py", "app:app"]
