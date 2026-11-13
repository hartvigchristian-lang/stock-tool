#!/bin/bash
# Starts the portfolio server (if it isn't already running) and opens the dashboard.
cd "$(dirname "$0")" || exit 1
PORT=8765
URL="http://127.0.0.1:$PORT"

if ! /usr/sbin/lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  nohup /usr/bin/python3 portfolio.py > server.log 2>&1 &
  for _ in $(seq 1 20); do
    /usr/sbin/lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && break
    sleep 0.25
  done
fi

open "$URL"
