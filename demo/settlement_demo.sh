#!/usr/bin/env bash
# Reproducible HTTP example for prize settlement.
#
# Story:
#   * set fixed champion/runner-up amounts once and add funds in batches;
#   * issue a champion payment before an appeal is decided;
#   * the appeal overturns the final after the payment order exists;
#   * the bank confirms the OLD order successfully after readjudication;
#   * finance recovers the overpaid difference;
#   * the recovered cash pays the NEW champion;
#   * restart the API and show that settlement state survives.
#
# Run:
#   ./demo/settlement_demo.sh
#
# Requires curl and jq.  The script starts its own uvicorn and throwaway DB.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi
PORT="${PORT:-8766}"
WORK="$(mktemp -d)"
DB="$WORK/settlement-demo.db"
BASE="http://127.0.0.1:$PORT"
SERVER_PID=""
RESP_CODE=""

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
  for _ in $(seq 1 150); do
    if curl -fsS "$BASE/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  echo "server failed to start" >&2
  exit 1
}

stop_server() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
  fi
}

# http_call METHOD PATH [JSON_BODY] [IDEMPOTENCY_KEY]
# Writes the response body to $LAST and sets RESP_CODE.
http_call() {
  local method="$1" path="$2" body="${3:-}" key="${4:-}"
  local args=(
    -sS -X "$method"
    -H 'Content-Type: application/json'
    -o "$WORK/response.json"
    -w '%{http_code}'
  )
  if [[ -n "$key" ]]; then
    args+=(-H "Idempotency-Key: $key")
  fi
  if [[ -n "$body" ]]; then
    args+=(-d "$body")
  fi
  RESP_CODE="$(curl "${args[@]}" "$BASE$path")"
  LAST="$WORK/response.json"
}

show() {
  local title="$1" filter="${2:-.}"
  echo
  echo "=== $title (HTTP $RESP_CODE) ==="
  jq "$filter" "$LAST"
}

get_json() {
  curl -sS "$BASE$1"
}

player_id() {
  local seed="$1"
  get_json "/tournaments/$TID/bracket" |
    jq -r --argjson seed "$seed" '.players[] | select(.seed == $seed) | .id'
}

play_slot() {
  local slot="$1" winner_seed="$2" key_suffix="$3"
  local pid
  pid="$(player_id "$winner_seed")"
  http_call POST "/tournaments/$TID/slots/$slot/start" \
    "{\"started_by\":\"demo-staff\"}" "start-$key_suffix"
  if [[ "$RESP_CODE" != "201" ]]; then
    echo "start $slot failed: $RESP_CODE" >&2
    cat "$LAST" >&2
    exit 1
  fi
  http_call POST "/tournaments/$TID/slots/$slot/result" \
    "{\"winner_player_id\":$pid,\"reported_by\":\"demo-staff\"}" \
    "result-$key_suffix"
  if [[ "$RESP_CODE" != "200" ]]; then
    echo "result $slot failed: $RESP_CODE" >&2
    cat "$LAST" >&2
    exit 1
  fi
}

cd "$ROOT"
echo "Starting settlement demo with $PYTHON"
echo "Database: $DB"
start_server

echo
echo "=== 1. Create tournament and play bracket ==="
http_call POST /tournaments '{
  "name": "Settlement Cup",
  "players": ["Alice", "Bob", "Cara", "Dan", "Eve", "Finn", "Gus", "Hana"]
}' create-tournament
TID="$(jq -r '.tournament.id' "$LAST")"
echo "tournament id: $TID"

play_slot QF1 1 qf1
play_slot QF2 5 qf2
play_slot QF3 3 qf3
play_slot QF4 2 qf4
play_slot SF1 1 sf1
play_slot SF2 3 sf2
play_slot F 1 final
P1="$(player_id 1)"
P3="$(player_id 3)"
echo "seed 1 player id=$P1 (initial champion); seed 3 player id=$P3 (new champion after overturn)"

echo
echo "=== 2. Set prizes once and add tournament funds in two batches ==="
http_call POST "/tournaments/$TID/prize-settings" '{
  "champion_amount_cents": 10000,
  "runner_up_amount_cents": 5000,
  "set_by": "treasurer"
}' prize-settings-1
show "Prize settings" '.prize_settings | {champion_amount_cents, runner_up_amount_cents}'

http_call POST "/tournaments/$TID/funds" \
  '{"amount_cents":12000,"source":"sponsorship","funded_by":"finance"}' fund-1
show "First funding batch" '{amount_cents, source}'
http_call POST "/tournaments/$TID/funds" \
  '{"amount_cents":3000,"source":"ticket sales","funded_by":"finance"}' fund-2
show "Second funding batch" '{amount_cents, source}'

get_json "/tournaments/$TID/settlement" > "$WORK/settlement.json"
V1="$(jq -r '.effective_final_result_version_id' "$WORK/settlement.json")"
echo "Effective final result version before appeal: $V1"
jq '{status:.settlement_status, available_funds_cents, awards:[.awards[]|{role,player:.player.name,amount_cents}]}' \
  "$WORK/settlement.json"

echo
echo "=== 3. Issue 10000-cent payment to the current champion ==="
http_call POST "/tournaments/$TID/payments" "{
  \"player_id\":$P1,
  \"amount_cents\":10000,
  \"result_version_id\":$V1,
  \"issued_by\":\"finance\"
}" payment-old
PAY1="$(jq -r '.id' "$LAST")"
show "Pending payment order" '{id,payment_no,player_id,amount_cents,result_version_id,status}'

echo
echo "=== 4. Appeal the final; new payments/recoveries pause ==="
http_call POST "/tournaments/$TID/slots/F/appeals" "{
  \"result_version_id\":$V1,
  \"description\":\"Final score was reversed\",
  \"submitted_by\":\"coach\"
}" appeal-final
AID="$(jq -r '.appeal.id' "$LAST")"
show "Appeal filed" '{appeal_id:.appeal.id, status:.appeal.status}'

get_json "/tournaments/$TID/settlement" > "$WORK/paused.json"
jq '{
  settlement_status,
  paused,
  reason,
  old_player:{
    prize_amount_cents:(.players[]|select(.player_id=='"$P1"')|.prize_amount_cents),
    recoverable_amount_cents:(.players[]|select(.player_id=='"$P1"')|.recoverable_amount_cents),
    unconfirmed_payment_cents:(.players[]|select(.player_id=='"$P1"')|.unconfirmed_payment_cents)
  },
  payment_facts:[.payments[]|{id,status,amount_cents}]
}' "$WORK/paused.json"

echo
echo "--- A new payment is rejected while paused ---"
http_call POST "/tournaments/$TID/payments" "{
  \"player_id\":$P1,\"amount_cents\":1,\"result_version_id\":$V1
}" blocked-payment
echo "HTTP $RESP_CODE"
jq '.error | {code,message}' "$LAST"

echo
echo "=== 5. Overturn the final: seed 3 becomes champion, seed 1 runner-up ==="
http_call POST "/tournaments/$TID/appeals/$AID/adjudicate" "{
  \"outcome\":\"overturned\",
  \"expected_result_version_id\":$V1,
  \"adjudicated_by\":\"judge\",
  \"note\":\"Score sheet correction\"
}" adjudicate-final
show "Adjudication" '.adjudication | {outcome, based_on_result_version_id, new_result_version_id}'

get_json "/tournaments/$TID/settlement" > "$WORK/readjudicated.json"
V2="$(jq -r '.effective_final_result_version_id' "$WORK/readjudicated.json")"
echo "New effective final result version: $V2"
jq '{status:.settlement_status, awards:[.awards[]|{role,player:.player.name,amount_cents}]}' \
  "$WORK/readjudicated.json"

echo
echo "=== 6. Bank confirms the OLD payment after readjudication ==="
http_call POST "/payments/$PAY1/confirm" '{
  "status":"success",
  "bank_receipt_number":"BANK-RCPT-OLD-1",
  "confirmed_by":"bank-clerk"
}' confirm-old
show "Bank confirmation" '{id,status,bank_receipt_number,confirmed_at}'

echo
echo "--- Repeating the same successful request/key returns the original receipt ---"
http_call POST "/payments/$PAY1/confirm" '{
  "status":"success",
  "bank_receipt_number":"BANK-RCPT-OLD-1",
  "confirmed_by":"bank-clerk"
}' confirm-old
echo "HTTP $RESP_CODE"
jq '{id,status,bank_receipt_number}' "$LAST"

echo
echo "--- Same key with a different request is a conflict ---"
http_call POST "/payments/$PAY1/confirm" '{
  "status":"failed",
  "failure_reason":"different outcome"
}' confirm-old
echo "HTTP $RESP_CODE"
jq '.error | {code,message}' "$LAST"

get_json "/tournaments/$TID/settlement" > "$WORK/after-bank.json"
jq '{
  available_funds_cents,
  old_player:(.players[]|select(.player_id=='"$P1"')|{
    current_prize_amount_cents,net_paid_cents,pending_recovery_cents
  })
}' "$WORK/after-bank.json"

echo
echo "=== 7. Register actual recovery of the 5000-cent overpayment ==="
http_call POST "/tournaments/$TID/recoveries" "{
  \"player_id\":$P1,
  \"amount_cents\":5000,
  \"result_version_id\":$V2,
  \"received_by\":\"cashier\"
}" recovery-1
show "Recovery" '{id,player_id,amount_cents,result_version_id,status}'

echo
echo "=== 8. Pay the NEW champion with recovered/fund pool money ==="
http_call POST "/tournaments/$TID/payments" "{
  \"player_id\":$P3,
  \"amount_cents\":10000,
  \"result_version_id\":$V2,
  \"issued_by\":\"finance\"
}" payment-new
PAY2="$(jq -r '.id' "$LAST")"
show "New champion payment" '{id,player_id,amount_cents,result_version_id,status}'

http_call POST "/payments/$PAY2/confirm" '{
  "status":"success",
  "bank_receipt_number":"BANK-RCPT-NEW-1"
}' confirm-new
show "New payment confirmed" '{id,status,bank_receipt_number}'

echo
echo "=== 9. Final settlement query ==="
get_json "/tournaments/$TID/settlement" > "$WORK/final-settlement.json"
jq '{
  tournament_id,
  settlement_status,
  paused,
  available_funds_cents,
  cash_balance_cents,
  players:[.players[] | select(.prize_amount_cents != 0 or .net_paid_cents != 0 or .unconfirmed_payment_cents != 0) | {
    player_id,name,role,
    prize_amount_cents,
    net_paid_cents,
    unconfirmed_payment_cents,
    pending_recovery_cents
  }],
  payments:[.payments[] | {
    id,recipient_player_id,amount_cents,snapshot_result_version_id:.result_version_id,
    status,bank_receipt_number
  }],
  recoveries:[.recoveries[] | {id,player_id,amount_cents,result_version_id}],
  fund_pool:{
    funding_total_cents:.prize_pool.funding_total_cents,
    paid_total_cents:.prize_pool.paid_total_cents,
    recovered_total_cents:.prize_pool.recovered_total_cents,
    available_funds_cents:.prize_pool.available_funds_cents
  },
  latest_award_difference:(.award_differences[-1] | {
    from_result_version_id,to_result_version_id,changes
  })
}' "$WORK/final-settlement.json"

echo
echo "=== 10. Restart service and verify durability ==="
stop_server
start_server
get_json "/tournaments/$TID/settlement" > "$WORK/restart-settlement.json"
jq '{settlement_status,available_funds_cents,payment_count:(.payments|length),recovery_count:(.recoveries|length)}' \
  "$WORK/restart-settlement.json"
get_json "/payments/$PAY1" | jq '{id,status,bank_receipt_number,amount_cents,result_version_id}'

echo
echo "Settlement demo completed successfully."
