"""Gunicorn settings for the container image.

Deliberately one worker. The refresh queue, the progress the page polls, the
watch-history cache and the transcript cache all live in this process's memory,
so a second worker would answer "/api/progress" from a different copy of that
state and let two refreshes run at once -- the app already refuses to start a
second refresh, and that guard only holds inside one process. Threads give the
concurrency instead, which is what the local `flask run` server does too.

Every other setting is the command-line equivalent, so overriding any of it is
just `docker run ... gunicorn --workers 2 app:app`.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = 1
threads = int(os.environ.get("THREADS", "8"))

# Requests return quickly -- a refresh hands back a job id and the page polls --
# so this only has to cover a slow /api/progress under load.
timeout = 120
graceful_timeout = 15
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
