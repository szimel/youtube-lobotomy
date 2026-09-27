#!/usr/bin/env bash
#
# Starts the Productivity Feed on http://127.0.0.1:5000
#
# The Linux counterpart of run.ps1. It checks the things that otherwise fail
# confusingly later -- the virtualenv, the dependencies, .env and the YouTube
# cookies -- and waits for the server to answer before opening a browser at it.
#
# It never prints the contents of .env or cookies.txt; only whether they are
# present and usable.
#
#   ./run.sh                 # http://127.0.0.1:5000
#   PORT=5055 ./run.sh       # somewhere else
#   HOST=0.0.0.0 ./run.sh    # reachable from the LAN (and from Tailscale)
#   ./run.sh --no-browser    # do not try to open anything

set -euo pipefail

PORT="${PORT:-5000}"
HOST="${HOST:-127.0.0.1}"
OPEN_BROWSER=1
for argument in "$@"; do
    case "$argument" in
        --no-browser) OPEN_BROWSER=0 ;;
        -h|--help)
            sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown option: $argument" >&2
            exit 2
            ;;
    esac
done

cd "$(dirname "$0")"

fail() {
    printf '\n  %s\n' "$1" >&2
    [ $# -gt 1 ] && printf '  %s\n' "$2" >&2
    printf '\n' >&2
    exit 1
}

PYTHON=".venv/bin/python"
[ -x "$PYTHON" ] || fail "No virtualenv found at .venv" "Create one with: python3 -m venv .venv"

"$PYTHON" -c "import flask, dotenv, yt_dlp" 2>/dev/null \
    || fail "The virtualenv is missing dependencies." ".venv/bin/python -m pip install -r requirements.txt"

[ -f .env ] || fail "No .env file, so there is no Jev API key." "Copy .env.example to .env, then put your key in it."

# Presence only: the value is never read out or printed.
"$PYTHON" -c "import os,sys; from dotenv import load_dotenv; load_dotenv(); sys.exit(0 if os.environ.get('JEV_API_KEY','').strip() else 1)" \
    || fail "JEV_API_KEY is not set in .env" "Add a line reading JEV_API_KEY=your-key (see .env.example)."

if [ ! -f data/cookies.txt ]; then
    printf '  No data/cookies.txt: the home feed and watch history will not work.\n' >&2
    printf "  See 'Keeping YouTube signed in' in README.md.\n\n" >&2
fi

# yt-dlp solves YouTube's JavaScript challenges with an external runtime; without
# one, caption downloads start failing in ways that look like YouTube's fault.
if ! command -v node >/dev/null 2>&1 && ! command -v deno >/dev/null 2>&1; then
    printf '  Neither node nor deno is installed: yt-dlp needs one of them for YouTube.\n' >&2
    printf '  Install Node 22 or newer, or expect missing transcripts.\n\n' >&2
fi

if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
    fail "Something is already listening on port $PORT." "Close it, or run: PORT=5055 ./run.sh"
fi

URL="http://127.0.0.1:$PORT/"
printf '\n  Productivity Feed\n  %s\n  Ctrl+C to stop.\n\n' "$URL"

"$PYTHON" -m flask --app app run --host "$HOST" --port "$PORT" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

READY=0
for _ in $(seq 1 40); do
    sleep 0.25
    kill -0 "$SERVER_PID" 2>/dev/null || break
    if "$PYTHON" -c "
import sys, urllib.request
try:
    urllib.request.urlopen('$URL', timeout=2)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        READY=1
        break
    fi
done

if [ "$READY" -ne 1 ]; then
    fail "The server did not start." "Run it directly to see the error: .venv/bin/python -m flask --app app run"
fi

if [ "$OPEN_BROWSER" -eq 1 ]; then
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 || true
    else
        printf '  Open %s in a browser (no xdg-open on this machine).\n' "$URL"
    fi
fi

wait "$SERVER_PID"
