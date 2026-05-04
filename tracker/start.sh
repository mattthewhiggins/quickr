#!/usr/bin/env bash
# Render entrypoint: runs the tracker loop in the background and uvicorn in the foreground.
# Render restarts the whole service if uvicorn dies; this wrapper auto-restarts the worker.
set -e

CONFIG_PATH="${CONFIG_PATH:-config.toml}"

# Worker, with restart-on-crash supervisor.
(
  while true; do
    python -m pokecentre.main -c "$CONFIG_PATH" --loop "${POLL_SECONDS:-60}" || true
    echo "[supervisor] worker exited, restarting in 5s" >&2
    sleep 5
  done
) &

# Web — must bind to $PORT for Render's healthcheck.
exec uvicorn pokecentre.web:app --host 0.0.0.0 --port "${PORT:-8000}"
