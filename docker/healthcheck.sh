#!/bin/bash
# Container health check: asks the app for /healthz and insists on a 200.
#
# This runs forever, once a minute, for months, so what it costs matters more
# than what it looks like. The obvious version -- python -c with urllib -- costs
# about 165 ms of CPU per run, which made the health check roughly twenty times
# more expensive than the application it was checking (0.25 millicores for the
# server, 5.8 for watching it). Bash redirects to a TCP socket itself, which
# does the same job in about 4 ms.
#
# /dev/tcp is a bash feature, not a POSIX sh one, so this has to be run by bash
# (the Dockerfile uses the exec form of HEALTHCHECK to do exactly that).

exec 3<>/dev/tcp/127.0.0.1/"${PORT:-8080}" 2>/dev/null || exit 1
printf 'GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n' >&3

# A server that accepts the connection and then says nothing is not healthy, so
# the read is bounded rather than left to Docker's own timeout.
read -r -t 5 status <&3

case "$status" in
    *" 200 "*) exit 0 ;;
    *) exit 1 ;;
esac
