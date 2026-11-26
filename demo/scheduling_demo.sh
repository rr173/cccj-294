#!/usr/bin/env bash
# Reproducible HTTP example for shared-venue reservations and closure repair.
#
#   PYTHON=.venv/bin/python ./demo/scheduling_demo.sh
#
# It starts its own uvicorn on a throwaway database.  The server clock is
# pinned through TOURNAMENT_NOW: first to a pre-event time for booking/closure,
# then restarted at the QF1 start time.  In real operation unset
# TOURNAMENT_NOW to use the real UTC clock.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
  else
    PYTHON="$(command -v python3)"
  fi
fi
PORT="${PORT:-8766}"
WORK="$(mktemp -d)"
DB="$WORK/scheduling-demo.db"
BASE="http://127.0.0.1:$PORT"
BOOKING_NOW="2026-03-15T08:30:00+00:00"
START_NOW="2026-03-15T09:00:00+00:00"
SERVER_NOW="$BOOKING_NOW"
DATE="2026-03-15"
SERVER_PID=""
LAST_CODE=""
LAST_BODY=""

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

for tool in curl jq; do
  command -v "$tool" >/dev/null 2>&1 || { echo "$tool is required" >&2; exit 1; }
done

start_server() {
  TOURNAMENT_DB="$DB" TOURNAMENT_NOW="$SERVER_NOW" "$PYTHON" -m uvicorn app.asgi:app \
    --host 127.0.0.1 --port "$PORT" --log-level warning &
  SERVER_PID=$!
  for _ in $(seq 1 100); do
    if curl -fsS "$BASE/health" >/dev/null 2>&1; then return; fi
    sleep 0.1
  done
  echo "server failed to start" >&2
  exit 1
}

stop_server() {
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  SERVER_PID=""
}

# send_post PATH JSON KEY -- sets LAST_CODE and LAST_BODY.
send_post() {
  local path="$1" body="$2" key="$3" response
  echo "POST $path  [Idempotency-Key: $key]"
  response="$(curl -sS -X POST "$BASE$path" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $key" \
    -d "$body" -w $'\n%{http_code}')"
  LAST_CODE="${response##*$'\n'}"
  LAST_BODY="${response%$'\n'*}"
  echo "  HTTP $LAST_CODE"
}

create_tournament() {
  local name="$1" response
  response="$(curl -sS -X POST "$BASE/tournaments" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$name\",\"players\":[\"$name P1\",\"P2\",\"P3\",\"P4\",\"P5\",\"P6\",\"P7\",\"P8\"]}")"
  jq -r '.tournament.id' <<<"$response"
}

reservation_body() {
  local tid="$1" qf1="$2"
  cat <<JSON
{"candidates":{
  "QF1":$qf1,
  "QF2":[{"venue_name":"T${tid}-QF2","date":"$DATE","start_minute":540,"end_minute":570}],
  "QF3":[{"venue_name":"T${tid}-QF3","date":"$DATE","start_minute":540,"end_minute":570}],
  "QF4":[{"venue_name":"T${tid}-QF4","date":"$DATE","start_minute":540,"end_minute":570}],
  "SF1":[{"venue_name":"T${tid}-SF1","date":"$DATE","start_minute":580,"end_minute":610}],
  "SF2":[{"venue_name":"T${tid}-SF2","date":"$DATE","start_minute":580,"end_minute":610}],
  "F":[{"venue_name":"T${tid}-F","date":"$DATE","start_minute":620,"end_minute":650}]
}}
JSON
}

qf1_summary() {
  curl -sS "$BASE/schedule?date=$DATE" | jq -r '
    "day \(.date): closures=" + ([.closures[].venue_name] | join(",")) ,
    (.assignments[] | select(.slot_id=="QF1") |
      "  T\(.tournament_id) QF1  \(.start)-\(.end) UTC  @ \(.venue_name)")
  '
}

cd "$ROOT"
echo "=============================================================="
echo " Shared-venue scheduling and closure re-plan HTTP demo"
echo " db: $DB"
echo " booking clock: $BOOKING_NOW ; start clock after restart: $START_NOW"
echo "=============================================================="
start_server

echo
echo "--- 1. Create two eight-player tournaments ---"
T1="$(create_tournament Alpha)"
T2="$(create_tournament Beta)"
echo "T1=$T1 T2=$T2"

echo
echo "--- 2. Confirm seven-slot reservations ---"
Q1='[{"venue_name":"A","date":"'"$DATE"'","start_minute":540,"end_minute":570},{"venue_name":"B","date":"'"$DATE"'","start_minute":540,"end_minute":570}]'
Q2='[{"venue_name":"B","date":"'"$DATE"'","start_minute":540,"end_minute":570},{"venue_name":"C","date":"'"$DATE"'","start_minute":540,"end_minute":570}]'
send_post "/tournaments/$T1/reservations" "$(reservation_body "$T1" "$Q1")" reserve-alpha
[[ "$LAST_CODE" == "201" ]] || { echo "$LAST_BODY" | jq .; exit 1; }
send_post "/tournaments/$T2/reservations" "$(reservation_body "$T2" "$Q2")" reserve-beta
[[ "$LAST_CODE" == "201" ]] || { echo "$LAST_BODY" | jq .; exit 1; }
qf1_summary

echo
echo "--- 3. Close venue A 09:05-09:20; exact global minimum is two moves ---"
CLOSURE_BODY="{\"venue_name\":\"A\",\"date\":\"$DATE\",\"start_minute\":545,\"end_minute\":560,\"reason\":\"floor repair\"}"
send_post /closures "$CLOSURE_BODY" closure-a-1
[[ "$LAST_CODE" == "201" ]] || { echo "$LAST_BODY" | jq .; exit 1; }
echo "$LAST_BODY" | jq -r '
  "closure id=\(.closure.id); changed_slot_count=\(.changed_slot_count)",
  (.changes[] | "  T\(.tournament_id) \(.slot_id): \(.from.venue_name) -> \(.to.venue_name)")
'
qf1_summary

echo
echo "--- 4. Replay closure with same Idempotency-Key ---"
send_post /closures "$CLOSURE_BODY" closure-a-1
echo "$LAST_BODY" | jq -c '{closure_id: .closure.id, changed_slot_count, changed_slots}'

echo
echo "--- 5. Restart at 09:00 UTC; schedule and receipt survive ---"
stop_server
SERVER_NOW="$START_NOW"
start_server
qf1_summary
curl -sS "$BASE/receipts/closure-a-1" | jq -c '{key, scope, status_code, closure_id: .response.closure.id}'

echo
echo "--- 6. First starts use the replanned venues ---"
send_post "/tournaments/$T1/slots/QF1/start" '{"started_by":"staff-alice"}' start-alpha-qf1
[[ "$LAST_CODE" == "201" ]] || { echo "$LAST_BODY" | jq .; exit 1; }
echo "$LAST_BODY" | jq -c '.match.venue_usage | {slot_id, venue, date, start_minute, end_minute, actual_started_at, within_scheduled_window}'

send_post "/tournaments/$T2/slots/QF1/start" '{"started_by":"staff-bob"}' start-beta-qf1
[[ "$LAST_CODE" == "201" ]] || { echo "$LAST_BODY" | jq .; exit 1; }
echo "$LAST_BODY" | jq -c '.match.venue_usage | {slot_id, venue, date, start_minute, end_minute, actual_started_at, within_scheduled_window}'

echo
echo "--- 7. Actual venue records in T1 history ---"
curl -sS "$BASE/tournaments/$T1/history" | jq -c '
  .matches[] | select(.slot_id=="QF1") |
  {slot_id, attempt, venue, started_at, usage_id: .venue_usage.id}
'

echo
echo "--- 8. Restart once more and verify durability ---"
stop_server
start_server
qf1_summary
curl -sS "$BASE/tournaments/$T1/history" | jq -c '[.matches[] | {slot_id, venue, usage_id: .venue_usage.id}]'

echo
echo "=============================================================="
echo " Scheduling demo finished; temporary DB removed."
echo "=============================================================="
