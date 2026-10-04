#!/usr/bin/env bash
# Live demo control: the console + the mailbox poller, both in the background.
#
#   ./scripts/demo.sh start     console on :8000, worker polling Zoho
#   ./scripts/demo.sh status     what is running, and the last few runs
#   ./scripts/demo.sh logs       follow both logs
#   ./scripts/demo.sh stop       stop both
#
# caffeinate keeps the Mac awake for the length of the demo: a sleeping laptop
# stops the poller, and the mail arrives to nothing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PORT="${PORT:-8020}"        # 8000/8010 are commonly already taken on this machine
PROFILE="${PROFILE:-zoho}"
LOGS="$ROOT/data/logs"; mkdir -p "$LOGS"
WEB_LOG="$LOGS/web.log"; WORKER_LOG="$LOGS/worker.log"
WEB_PID="$LOGS/web.pid"; WORKER_PID="$LOGS/worker.pid"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# A shell variable beats .env, which beats active_profile in settings.yaml. Export it so
# the web process (whose webhook and /api/health call load_settings() with no argument)
# agrees with the worker instead of quietly serving the dev profile.
export AML_PROFILE="$PROFILE"

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

start() {
  # a busy port makes uvicorn exit while some OTHER app answers on that URL - which
  # looks like "the app is broken" rather than "the app never started"
  if ! alive "$WEB_PID" && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "!! port $PORT is already in use by:"
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN | tail -n +2 | sed 's/^/     /'
    echo "   pick another:  PORT=8021 $0 start"
    exit 1
  fi
  alive "$WEB_PID"    && { echo "console already running (pid $(cat "$WEB_PID"))"; } || {
    nohup caffeinate -i "$PY" -m uvicorn src.api:app --host 127.0.0.1 --port "$PORT" \
      >"$WEB_LOG" 2>&1 & echo $! >"$WEB_PID"
    echo "console  -> http://localhost:$PORT   (pid $(cat "$WEB_PID"), log $WEB_LOG)"
  }
  alive "$WORKER_PID" && { echo "worker already running (pid $(cat "$WORKER_PID"))"; } || {
    nohup caffeinate -i "$PY" scripts/worker.py --profile "$PROFILE" >"$WORKER_LOG" 2>&1 &
    echo $! >"$WORKER_PID"
  }
  sleep 3
  # both processes die in the first seconds when misconfigured - surface it now,
  # not in front of an audience
  if alive "$WORKER_PID"; then
    echo "worker   -> $(head -1 "$WORKER_LOG")"
    grep -q '^!!' "$WORKER_LOG" && { echo; sed -n 's/^!!/     !!/p' "$WORKER_LOG" | head -2; }
  else
    echo; echo "!! worker exited immediately:"; tail -5 "$WORKER_LOG" | sed 's/^/     /'
  fi
  if ! alive "$WEB_PID"; then
    echo; echo "!! console exited immediately:"; tail -5 "$WEB_LOG" | sed 's/^/     /'
  fi
  echo
  echo "open http://localhost:$PORT and send the watchlist mail - the console picks it up on its own"
}

stop() {
  for f in "$WEB_PID" "$WORKER_PID"; do
    if alive "$f"; then kill "$(cat "$f")" && echo "stopped pid $(cat "$f")"; fi
    rm -f "$f"
  done
}

status() {
  alive "$WEB_PID"    && echo "console  RUNNING  pid $(cat "$WEB_PID")   http://localhost:$PORT" \
                      || echo "console  stopped"
  alive "$WORKER_PID" && echo "worker   RUNNING  pid $(cat "$WORKER_PID")" \
                      || echo "worker   stopped"
  echo; echo "last worker activity:"; tail -6 "$WORKER_LOG" 2>/dev/null || echo "  (no log yet)"
  echo; echo "recent runs:"
  "$PY" - <<'EOF'
import glob, json, os
for p in sorted(glob.glob("data/runs/*.json"), key=os.path.getmtime)[-5:]:
    d = json.load(open(p))
    c = d.get("counts") or {}
    print(f"  {d['run_id']}  {d['status']:8s} {d.get('trigger_type',''):10s} "
          f"HIGH={c.get('HIGH','-')} total={c.get('total','-')}  {d.get('error') or ''}")
EOF
}

case "${1:-status}" in
  start)  start ;;
  stop)   stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  logs)   tail -f "$WORKER_LOG" "$WEB_LOG" ;;
  *) echo "usage: $0 {start|stop|restart|status|logs}"; exit 2 ;;
esac
