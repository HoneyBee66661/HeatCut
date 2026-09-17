#!/usr/bin/env bash
# HeatCut — Termux/device launcher (one command: start / stop / status)
#
#   ./start-termux.sh            start everything & print health
#   ./start-termux.sh stop       stop everything
#   ./start-termux.sh status     show what is running
#
# FULL MODE (repo checkout with .venv + node_modules):
#   backend API :8000 + device worker :8765 + frontend dev :5173
# WORKER MODE (single script shared with friends, no repo deps):
#   only heatcut_worker.py on :8765 — run this script next to the file,
#   or: cp start-termux.sh next to heatcut_worker.py
#
# Requirements: python3 (+ ffmpeg for exports). Termux tips:
#   termux-wake-lock keeps CPU alive when the screen is off;
#   run inside `tmux` so the session survives connection drops.
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
LOG_DIR="$DIR/logs"
mkdir -p "$LOG_DIR"

PORT_API="${PORT_API:-8000}"
PORT_WORKER="${HEATMAP_WORKER_PORT:-8765}"
PORT_UI="${PORT_UI:-5173}"

# optional local config (gitignored: .env.*): e.g. HEATMAP_WORKER_DRIVE_FOLDER,
# DRIVE_OAUTH_JSON, PROXY_URL, YT_COOKIES_FILE — sourced so nohup'd services inherit.
[ -f "$DIR/.worker.env" ] && set -a && . "$DIR/.worker.env" && set +a

# --- detect mode ----------------------------------------------------------
WORKER_ONLY=0
if [ ! -x "$DIR/.venv/bin/python" ] && [ ! -d "$DIR/node_modules" ]; then
    WORKER_ONLY=1
fi
PY="$DIR/.venv/bin/python"
[ "$WORKER_ONLY" = "1" ] && PY="${PYTHON:-python3}"

stop_all() {
    for svc in worker backend frontend; do
        pidfile="$LOG_DIR/$svc.pid"
        if [ -f "$pidfile" ]; then
            pid="$(cat "$pidfile" 2>/dev/null || true)"
            if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null && echo "stopped $svc (pid $pid)"
            fi
            rm -f "$pidfile"
        fi
    done
    echo "all stopped."
}

start_one() { # name cmd...
    local name="$1"; shift
    local pidfile="$LOG_DIR/$name.pid"
    if [ -f "$pidfile" ]; then
        local oldpid; oldpid="$(cat "$pidfile" 2>/dev/null || true)"
        if [ -n "${oldpid:-}" ] && kill -0 "$oldpid" 2>/dev/null; then
            echo "$name already running (pid $oldpid)"
            return 0
        fi
        rm -f "$pidfile"
    fi
    nohup "$@" >"$LOG_DIR/$name.log" 2>&1 &
    echo $! >"$pidfile"
    echo "$name started (pid $(cat "$pidfile"))"
}

status() {
    for svc in worker backend frontend; do
        pidfile="$LOG_DIR/$svc.pid"
        if [ -f "$pidfile" ]; then
            pid="$(cat "$pidfile" 2>/dev/null || true)"
            if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
                echo "$svc: RUNNING pid $pid"
            else
                echo "$svc: stale pidfile ($pidfile)"
            fi
        else
            echo "$svc: not managed by this script"
        fi
    done
}

case "${1:-start}" in
    stop)
        stop_all
        exit 0
        ;;
    status)
        status
        exit 0
        ;;
    start) ;;
    *) echo "usage: $0 [start|stop|status]"; exit 1 ;;
esac

# best-effort wake lock (native Termux only; harmless elsewhere)
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock 2>/dev/null || true

if [ "$WORKER_ONLY" = "1" ]; then
    [ -f "$DIR/heatcut_worker.py" ] || { echo "heatcut_worker.py not found next to this script"; exit 1; }
    start_one worker "$PY" "$DIR/heatcut_worker.py"
else
    start_one backend "$PY" -m uvicorn backend.main:app --host 127.0.0.1 --port "$PORT_API"
    start_one worker "$PY" "$DIR/heatcut_worker.py"
    if [ -d "$DIR/node_modules" ]; then
        start_one frontend npm run dev-frontend
    fi
fi

# --- health check (retry: backend takes ~5s to boot) ---------------------
for _ in 1 2 3 4 5 6; do
    backend_ok=0
    [ "$WORKER_ONLY" = "1" ] && break
    curl -s -o /dev/null -m 3 "http://127.0.0.1:$PORT_API" && backend_ok=1 && break
    sleep 3
done
echo ""
check() { # port name
    if curl -s -o /dev/null -m 5 "http://127.0.0.1:$1"; then
        echo "  $2  :$1   UP"
    else
        echo "  $2  :$1   DOWN (see $LOG_DIR logs)"
    fi
}
echo "HeatCut health:"
[ "$WORKER_ONLY" = "1" ] && check "$PORT_WORKER" "device-worker" || {
    check "$PORT_API" "backend-api"
    check "$PORT_WORKER" "device-worker"
    [ -d "$DIR/node_modules" ] && check "$PORT_UI" "frontend"
}
echo ""
echo "Logs: $LOG_DIR/  |  stop: $0 stop"
