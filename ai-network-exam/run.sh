#!/usr/bin/env bash
# ./run.sh start|stop [--all]|restart|status  -- Kafka (Docker) + topic + backend (uvicorn) + worker
set -euo pipefail
exec </dev/null          # never consume the caller's stdin (docker compose exec / nohup would)
cd "$(dirname "$0")"
RUN=data/run
LOGS=data/logs
mkdir -p "$RUN" "$LOGS"
PY=.venv/bin/python
TOPIC=$(grep -E '^KAFKA_TOPIC=' .env 2>/dev/null | cut -d= -f2- || true)
TOPIC=${TOPIC:-network-config-jobs}
PORT=${PORT:-8000}

alive() {
  [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null
}

start_kafka() {
  docker compose up -d kafka >/dev/null
  printf "kafka: waiting for broker"
  for _ in $(seq 1 60); do
    if docker compose exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --list >/dev/null 2>&1; then
      echo " ok"
      break
    fi
    printf "."
    sleep 2
  done
  docker compose exec -T kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 \
    --create --if-not-exists --topic "$TOPIC" --partitions 1 --replication-factor 1 >/dev/null
  echo "kafka: topic $TOPIC ready"
}

start_proc() {
  local name=$1
  shift
  if alive "$name"; then
    echo "$name: already running (pid $(cat "$RUN/$name.pid"))"
    return
  fi
  nohup "$@" </dev/null >>"$LOGS/$name.log" 2>&1 &
  echo $! >"$RUN/$name.pid"
  echo "$name: started (pid $!), log $LOGS/$name.log"
}

stop_proc() {
  local name=$1
  if alive "$name"; then
    kill "$(cat "$RUN/$name.pid")"
    sleep 1
    if alive "$name"; then kill -9 "$(cat "$RUN/$name.pid")" 2>/dev/null || true; fi
    echo "$name: stopped"
  else
    echo "$name: not running"
  fi
  rm -f "$RUN/$name.pid"
}

case "${1:-status}" in
  start)
    [ -f .env ] || { echo "missing .env (copy .env.example)"; exit 1; }
    start_kafka
    start_proc backend "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
    for _ in $(seq 1 30); do
      curl -fs "http://127.0.0.1:$PORT/health" >/dev/null && break
      sleep 0.5
    done
    start_proc worker "$PY" worker.py
    echo "UI: http://$(hostname -I | awk '{print $1}'):$PORT/    monitor: http://$(hostname -I | awk '{print $1}'):$PORT/jobs"
    ;;
  stop)
    stop_proc worker
    stop_proc backend
    if [ "${2:-}" = "--all" ]; then docker compose stop kafka; fi
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    for n in backend worker; do
      if alive "$n"; then echo "$n: running (pid $(cat "$RUN/$n.pid"))"; else echo "$n: stopped"; fi
    done
    docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null || true
    curl -fs "http://127.0.0.1:$PORT/health" && echo || echo "backend health: unreachable"
    ;;
  *)
    echo "usage: $0 start|stop [--all]|restart|status"
    exit 2
    ;;
esac
