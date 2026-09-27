#!/bin/sh
# Prepares the data volume and then runs the server as an unprivileged user.
#
# A bind-mounted ./data directory is owned by whoever created it on the host,
# which is frequently not the uid inside the container. PUID/PGID (the
# convention self-hosted images use) fix that on startup so the app never fails
# with a permission error halfway through a refresh.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" != "0" ]; then
    # Started with compose's `user:` or `--user`, so there is nothing to
    # rearrange; the mount already has to be writable by whoever that is.
    exec "$@"
fi

if [ "$PUID" = "0" ] || [ "$PGID" = "0" ]; then
    echo "entrypoint: refusing to run the server as root. Set PUID/PGID to a normal account." >&2
    exit 1
fi

if [ "$PGID" != "$(id -g feed)" ]; then
    groupmod --gid "$PGID" feed
fi

if [ "$PUID" != "$(id -u feed)" ]; then
    usermod --uid "$PUID" feed
fi

mkdir -p /app/data
# Only the data directory is touched: the code stays read-only to the account
# that runs it, so a bug cannot rewrite the application.
chown -R "$PUID:$PGID" /app/data

if ! gosu "$PUID:$PGID" test -w /app/data; then
    echo "entrypoint: /app/data is not writable by $PUID:$PGID." >&2
    echo "entrypoint: check the volume mount, or set PUID/PGID to the owner of the host directory." >&2
    exit 1
fi

exec gosu "$PUID:$PGID" "$@"
