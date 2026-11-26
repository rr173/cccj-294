#!/usr/bin/env bash
# Complete HTTP demo, driven with curl against a running uvicorn.
#
#   ./demo/run_demo.sh
#
# It starts its own server on 127.0.0.1:$PORT with a throwaway database.
# The story:
#   1. create an eight-seed bracket and play the quarterfinals
#   2. appeal QF2 while SF1 is still unplayed -> SF1/F suspend
#   3. uphold -> SF1 becomes playable again; finish the semifinals and final
#   4. file a nested appeal on QF2's *new* effective version and overturn it
#      -> the finished SF1 and final are kept as replaced history, the final
#         is no longer a champion, and the untouched half keeps its result
#   5. replay the affected SF1 and the final with the corrected pair
#   6. restart the server and verify the bracket, appeals and every bout
#      that was ever played are still there
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
PORT="${PORT:-8765}"
WORK="$(mktemp -d)"
DB="$WORK/tournament.db"
BASE="http://127.0.0.1:$PORT"
SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

start_server() {
  TOURNAMENT_DB="$DB" "$PYTHON" -m uvicorn app.asgi:app \
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

# post PATH JSON KEY  -- prints the request, body and HTTP status
post() {
  local path="$1" body="$2" key="$3"
  echo "POST $path"
  echo "  request: $body"
  echo "  key:     $key"
  curl -sS -X POST "$BASE$path" \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: $key" \
    -d "$body" -w '\n  HTTP %{http_code}\n'
  echo
}

get() {
  echo "GET $1" >&2
  curl -sS "$BASE$1"
}

start_match() { post "/tournaments/1/slots/$1/start" "{\"started_by\":\"$2\"}" "start-$1-$3"; }
report() { post "/tournaments/1/slots/$1/result" "{\"winner_player_id\":$2,\"reported_by\":\"$3\"}" "result-$1-$4"; }

result_version() {
  curl -sS "$BASE/tournaments/1/bracket" |
    jq --arg slot "$1" '.rounds[].slots[] | select(.slot_id==$slot) | .current_match.result.id'
}

slot_summary() {
  curl -sS "$BASE/tournaments/1/bracket" | jq -r '
    "champion: \(.champion.player.name // "none")  champion_status: \(.champion_status.state)",
    (.rounds[] | .slots[] |
      "  \(.slot_id)  \(.state)\t" +
      "current=" + (.current_match | if . == null then "-"
        else "m\(.id) \(.status) v\(.result.version // 0) winner=\(.result.winner.name // "-")" end) +
      "\tparticipants=" + ((.participants // .candidate_participants // {}) |
        "\(.a.name // "?") vs \(.b.name // "?")") +
      "\thistory=" + ([.history[].id] | tostring) +
      "\tpending_appeals=" + (.suspension.appeal_ids | tostring))'
}

cd "$ROOT"
echo "=============================================================="
echo " Eight-player knockout service - complete HTTP demo"
echo " database: $DB"
echo "=============================================================="
start_server

echo
echo "--- 1. Create the tournament (fixed seeds 1v8 4v5 | 3v6 2v7) ---"
post /tournaments '{"name":"Club championship","players":["Alice","Bob","Cara","Dan","Eve","Frank","Gus","Hana"]}' create-1

echo "--- 2. Play all four quarterfinals ---"
start_match QF1 alice 1; report QF1 1 alice 1
start_match QF2 alice 2; report QF2 5 alice 2
start_match QF3 alice 3; report QF3 3 alice 3
start_match QF4 alice 4; report QF4 2 alice 4
slot_summary

echo "--- 3. A start cannot be repeated, and a result needs a started bout ---"
start_match QF1 bob 2 || true
report SF1 1 bob 1 || true

echo "--- 4. Appeal the QF2 result (seed 5 beat seed 4) while SF1 is unplayed ---"
V1=$(result_version QF2)
echo "QF2 current effective result_version_id = $V1"
post /tournaments/1/slots/QF2/appeals \
  "{\"result_version_id\":$V1,\"description\":\"Second game score was recorded backwards\",\"submitted_by\":\"coach\"}" \
  appeal-qf2-1
echo "Bracket while the appeal is pending:"
slot_summary
echo "SF1 is suspended, so its old winner may not be used to start it:"
start_match SF1 bob 2 || true
echo "The unaffected half's SF2 is still playable:"
slot_summary

echo "--- 5. Uphold the appeal: SF1 becomes playable again ---"
post /tournaments/1/appeals/1/adjudicate \
  "{\"outcome\":\"upheld\",\"expected_result_version_id\":$V1,\"adjudicated_by\":\"judge\",\"note\":\"Score sheet confirmed\"}" \
  adjudicate-1
slot_summary
start_match SF1 alice 3; report SF1 1 alice 3
start_match SF2 alice 4; report SF2 3 alice 4
start_match F alice 5;  report F 1 alice 5
slot_summary

echo "--- 6. Nested appeal on the new effective QF2 version, then overturn ---"
V2=$(result_version QF2)
echo "QF2 current effective result_version_id = $V2"
post /tournaments/1/slots/QF2/appeals \
  "{\"result_version_id\":$V2,\"description\":\"Video review shows seed 4 won the decider\",\"submitted_by\":\"coach\"}" \
  appeal-qf2-2
start_match SF1 bob 6 || true
post /tournaments/1/appeals/2/adjudicate \
  "{\"outcome\":\"overturned\",\"expected_result_version_id\":$V2,\"adjudicated_by\":\"judge\",\"note\":\"Video review\"}" \
  adjudicate-2
echo "Bracket after the overturn: SF1 and F are history, SF2 is untouched:"
slot_summary
get /tournaments/1/bracket | jq '{
  champion,
  champion_status,
  superseded_matches: [.superseded_matches[] | {id, slot_id, status, result_winner: .result.winner.name}],
  slots: [.rounds[] | .slots[] | {
    slot_id, state, participants_source,
    participants: ((.participants // .candidate_participants) |
      if . == null then null else "\(.a.name) vs \(.b.name)" end),
    current_match_id: (.current_match.id // null),
    history: [.history[] | {id, status, winner: .result.winner.name}],
    suspension_reasons: .suspension.appeal_ids
  }]
}'

echo "--- 7. Replay the affected SF1 and final with the corrected pair ---"
start_match SF1 alice 7; report SF1 1 alice 7
echo "The final waits until both feeders are on the effective path:"
slot_summary
start_match F alice 8; report F 3 alice 8
slot_summary

echo "--- 8. Restart the server: bracket, appeals and all played bouts survive ---"
stop_server
start_server
slot_summary
get /tournaments/1/history | jq '{
  matches: [.matches[] | {id, slot_id, attempt, status,
                           superseded: (.superseded != null),
                           versions: [.result_versions[] | {version, winner: .winner.name, source}]}],
  appeals: [.appeals[] | {id, match_id, result_version_id, status, outcome, stale_reason}]
}'

echo "=============================================================="
echo " Demo finished. The temporary database is removed on exit."
echo "=============================================================="
