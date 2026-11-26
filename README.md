# Eight-player single-elimination tournament service

Python 3.11 · FastAPI · SQLite (WAL, `BEGIN IMMEDIATE` writers) · pytest.

The bracket is fixed: seeds 1v8, 4v5 | 3v6, 2v7 in the quarterfinals, the
winners meeting in SF1/SF2 and then the final. There are no draws and no UI.
Venue reservation and closure-aware rescheduling are provided over HTTP.

The central design rule is that **a bout that actually happened is never
renamed or deleted**. The bracket slot is a separate thing that points at a
*current* bout; an appeal that changes an upstream result can retire a
downstream bout into history and leave the slot waiting for a fresh pair.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install fastapi "uvicorn[standard]" pytest httpx

.venv/bin/uvicorn app.asgi:app --port 8000        # TOURNAMENT_DB=/path/to.db
.venv/bin/python -m pytest -q                     # 69 tests
./demo/run_demo.sh                                # appeal/curl walkthrough
./demo/scheduling_demo.sh                         # booking/closure/start curl story
./demo/settlement_demo.sh                         # payment/readjudication/recovery story
./demo/admission_demo.sh                          # offline gate permit + reschedule conflict
```

`demo/run_demo.sh` starts its own uvicorn on a throwaway database, drives the
whole story with `curl`, restarts the process at the end, and prints the
history. It needs `jq` and `curl` on the PATH. `demo/settlement_demo.sh` is a
repeatable end-to-end finance example: it issues a payment, overturns the final,
confirms the old payment after the decision, recovers the overpayment, pays the
new champion, and restarts the service.

## Model

| concept | meaning |
| --- | --- |
| slot | one bracket position: `QF1..QF4`, `SF1`, `SF2`, `F` |
| match (bout) | something staff actually started; participants are locked at start; never mutated, never deleted |
| result version | immutable result rows of a bout; version 1 is staff-reported, later versions come only from adjudications |
| appeal | targets one specific result version; `pending` → `upheld` / `overturned` / `stale` |
| adjudication | one immutable decision with the version it was based on and the version it created |

A bout is `started` then `completed`, or stays `started` if the path is
replaced. `is_current = 0` means the bout is replaced history: it is still
queryable, still shows its result and versions, but **can never advance a
player again** (the award of a slot to a participant only reads the *current*
bout of each feeder).

`result_versions` has a partial unique index allowing at most one
`source='reported'` row per bout, so two staff members can never create two
effective results for one match. Only an adjudication adds later versions.

### Slot states

`waiting` (feeders have no effective winners yet) · `ready` (both participants
known and playable) · `suspended` (participants known, but an ancestor result
is under appeal) · `in_progress` · `completed`. A slot with a live or
completed current bout keeps displaying it even while an ancestor appeal is
pending — only *not yet started* affected slots are paused. `participants`
always reflects the pair of the current bout when there is one
(`participants_source: "current_match"`), otherwise the pair the current
effective path would give now (`participants_source: "effective_path"`).

## HTTP

`Idempotency-Key` is required for `start`, `result` and `adjudicate`
(`appeal` accepts one optionally). Replaying the same key with the same body
returns the original response; reusing it for a different operation/body is a
`409 idempotency_conflict`. Missing key is `400`.

| method & path | purpose |
| --- | --- |
| `POST /tournaments` | `{name, players: [8 unique names]}` → seeds 1..8 |
| `GET /tournaments/{id}/bracket` | rounds, slot states, current/history bouts, suspension, champion status |
| `GET /tournaments/{id}/history` | every bout ever started (with all result versions) and every appeal |
| `GET /tournaments/{id}/appeals?status=` | appeal ledger |
| `POST /tournaments/{id}/slots/{slot}/start` | `{started_by}`; locks the two participants |
| `POST /tournaments/{id}/slots/{slot}/result` | `{winner_player_id, reported_by}`; only for a started, not-yet-completed current bout |
| `POST /tournaments/{id}/slots/{slot}/appeals` | `{result_version_id, description, submitted_by}` on the **current** effective version |
| `POST /tournaments/{id}/appeals/{aid}/adjudicate` | `{outcome: "upheld"\|"overturned", expected_result_version_id, adjudicated_by, note?}` |
| `GET /tournaments/{id}/matches/{mid}` | one bout, current or historical |
| `POST /tournaments/{id}/prize-settings` | set once: champion/runner-up fixed amounts in integer cents |
| `POST /tournaments/{id}/funds` | add one positive prize-fund contribution for this tournament |
| `GET /tournaments/{id}/settlement` | funds, player balances, payment orders, recoveries and ledger |
| `POST /tournaments/{id}/payments` | issue a payment against the seen effective final result version |
| `POST /payments/{pid}/confirm` | register bank `success`/`failed`; success needs a unique receipt |
| `POST /tournaments/{id}/recoveries` | register an actual partial/full clawback against the current version |
| `POST /tournaments/{id}/reservations` | submit ≤6 candidates for all seven slots and confirm one booking |
| `GET /tournaments/{id}/schedule` | candidates plus current selected arrangement |
| `POST /closures` or `POST /venues/{venue}/closures` | register a future closure and atomically re-plan |
| `GET /schedule?date=YYYY-MM-DD` | closures and all tournaments' current arrangements for a UTC day |
| `GET /venues/{venue}/schedule?date=...` | one venue's closures and assignments |
| `GET /receipts/{Idempotency-Key}` | stored status/body of a successful idempotent write |
| `GET /health` | liveness |

Errors are `{"error": {"code": ..., "message": ..., ...}}`.

Key codes: `match_already_in_progress`, `match_already_completed`,
`match_not_started`, `participants_not_ready`, `slot_suspended`,
`result_version_changed`, `appeal_stale`, `appeal_already_decided`,
`invalid_winner`, `idempotency_conflict`, `idempotency_key_required`.

## Venue scheduling

Candidates use UTC minute-of-day.  An interval is half-open:
`[start_minute, end_minute)`, so one slot ending at minute 630 and a feeder
or next slot starting at minute 630 are compatible.  A compact request is:

```json
{
  "candidates": {
    "QF1": [
      {"venue_name": "A", "date": "2026-03-15", "start_minute": 540, "end_minute": 570},
      {"venue_name": "B", "date": "2026-03-15", "start_minute": 540, "end_minute": 570}
    ],
    "QF2": ["..."], "QF3": ["..."], "QF4": ["..."],
    "SF1": ["..."], "SF2": ["..."], "F": ["..."]
  }
}
```

The API also accepts a list of `{slot_id, options:[...]}` groups and common
aliases such as `venue`, `court`, `start_time:"09:00"`, or full UTC
`starts_at`/`ends_at` timestamps.  Send an `Idempotency-Key` header.

Confirmation is one atomic transaction:

* all seven slots must receive one feasible choice;
* at most six candidates are stored per slot;
* newly chosen starts must not be before server UTC time;
* choices must not overlap a closure or another tournament's selected interval
  at the same venue;
* a slot may start only after every direct feeder's selected interval ends;
* a first reservation treats every other tournament's arrangement as fixed;
* expected search envelope: at most four tournaments on one UTC day.

`POST /closures` accepts the same date/minute interval fields.  The solver
searches changed-slot counts from zero upward.  At the minimum count it takes
the first lexical DFS solution in `(tournament id, QF1..QF4, SF1, SF2, F)`
order, trying each slot's `(start, end, venue_name)` tuple lexicographically;
that is exactly the required global tie-break.  A selected slot is immutable
after its start instant has arrived or after any bout has used it.  Movable
slots can use only their own original candidates, including slots moved solely
to resolve a new cross-tournament overlap or bracket cascade.  If no complete
solution exists, the closure insert and tentative plan roll back.

All reservation, closure and start transactions use `BEGIN IMMEDIATE`; day and
history reads use one SQLite snapshot.  This prevents duplicate venue
occupancy and torn schedule reads across concurrent HTTP workers.

For a reserved tournament, the **first** start of each slot must satisfy the
existing bracket/appeal rules and occur inside its confirmed interval.  The
successful start writes an immutable `match_venue_usages` row; it appears in
`GET /tournaments/{id}/history` as `venue_usage` / `actual_schedule` and is
never updated by an appeal or adjudication.  An appeal replay creates attempt
2 and is outside this first-booking window rule so staff can arrange that
rematch separately.  Tournaments without a reservation keep the old start
behavior.

`TOURNAMENT_NOW=YYYY-MM-DDTHH:MM:SS+00:00` can pin the clock for tests and the
reproducible demo; leave it unset in normal operation.

## Spectator admission and offline gates

Admission is modeled per reserved bracket slot (“轮位”) and applies to that
slot's first/current booked occurrence.  Tickets follow the slot, not an old
schedule row: after a reschedule, an unused code is accepted only at the
current venue/interval unless an unsettled offline permit pins the old one.

The capacity invariant is:

```text
confirmed admissions
+ release_limit of every unsettled offline permit
<= fixed slot capacity
```

Merely issuing tickets does **not** reserve capacity.  A permit may list more
codes than its `release_limit`; all listed codes are locked, while only
`release_limit` places are reserved.

Canonical HTTP endpoints (plus common `/permits`, `/entry-codes`, `/check-in`,
camelCase and action-path aliases):

| method & path | purpose |
| --- | --- |
| `PUT /tournaments/{id}/slots/{slot}/admission-capacity` | set/update the fixed cumulative cap; cannot lower it below committed entries+reservations |
| `POST /tournaments/{id}/slots/{slot}/tickets` | issue generated codes or explicit `codes`; issuance never checks remaining capacity |
| `GET /tournaments/{id}/slots/{slot}/admission` | confirmed entries, outstanding permit reservations, remaining allocatable capacity, tickets/permits/admissions |
| `POST /tickets/{code}/revoke` | revoke an unused code; blocked while it is listed on an unsettled permit |
| `POST /gates/{gate_id}/verify` | online one-code admission; checks current venue and half-open UTC window `[starts_at, ends_at)` |
| `POST /tournaments/{id}/slots/{slot}/offline-permits` | claim a gate permit from `ticket_codes` and `release_limit` |
| `GET /offline-permits/{permit_id}` | permit state; `expired` is informational and never means settled |
| `POST /offline-permits/{permit_id}/settle` | submit the complete offline record, including `records: []` |
| `POST /offline-permits/{permit_id}/terminate` | optional marker that local gate use has stopped; this does **not** settle or release capacity |
| `GET /offline-settlements/{receipt_id}` or `GET /receipts/{Idempotency-Key}` | retrieve the original receipt |

All admission writers use SQLite `BEGIN IMMEDIATE`.  Settlement validates the
gate, every code's membership in the permit, duplicate codes in the report,
`len(records) <= release_limit`, capacity, and each record's timestamp against
the original permit window.  Any validation failure rolls the whole report
back.  A correct report can be settled after the window has ended.  Settlement
turns only reported codes into confirmed admissions, marks unreported listed
codes unused/free, and removes the permit's full reservation.  Service restart,
window expiry or a lost response never automatically settles a permit; retry
with the same `Idempotency-Key`/`report_id` returns the original receipt, and
different records get `409 settlement_conflict` / `idempotency_conflict`.

`app/gate.py` provides `GateTerminal`, a durable SQLite gate state, and
`HttpTransport`.  It admits one code per call, rejects local duplicates and
over-limit use, survives `GateTerminal(path)` restart, durably terminates a
permit before network submission, and retries one stable report key after a
timeout.  `python -m app.gate --state gate.db ...` is a small CLI.

Every `active`/unsettled permit contributes a pinned scheduling variable in
`scheduling.build_repair_variables`.  Thus closure repair keeps its exact
venue/date/start/end; the rest of the arrangement still uses the original
minimum-change lexical solver.  If the new closure makes that pin infeasible,
the closure insert and plan update roll back together.

`python demo/admission_demo.py` (or `./demo/admission_demo.sh`) is a
self-checking repeatable story: a permit blocks closure/rescheduling, an empty
report allows the normal repair, a second permit snapshots the new schedule,
the durable gate admits/offline-restarts/rejects a duplicate, settlement
releases the unused reserved place, and the same unused code is rejected at the
old venue and accepted at the new one.

## Appeal semantics

* Filing an appeal requires the bout's current effective `result_version_id`;
  a stale version number is rejected (`result_version_changed`).
* While pending, the appealed bout keeps showing its result, and every
  **not yet started** slot downstream of it is `suspended`; `start` returns
  `409 slot_suspended`. Already started/completed downstream bouts are facts
  and are untouched. The other half of the bracket is unaffected.
* No champion is declared while any appeal is pending
  (`champion_status = {state: "provisional", blocking_appeal_ids: [...]}` and
  `champion = null`).
* **Upheld** creates a new version with the same winner (a deliberate "result
  confirmed" record). Nothing is renamed; paused slots become playable again.
* **Overturned** creates a new version with the opponent as winner and retires
  each downstream current bout whose locked pair no longer matches the pair
  produced by the new path (bouts whose pair is unchanged are left alone).
  Those retired bouts, including any result or live state, stay in
  `history`/`superseded_matches` and can never advance anyone. Their slots go
  back to `ready`/`waiting` and a fresh bout starts later as attempt 2, 3, …
* Each adjudication makes every other pending appeal **on the old version**
  stale (`another_appeal_adjudicated`), and every pending appeal whose bout
  was just superseded stale
  (`result_superseded_by_upstream_adjudication`). Stale appeals no longer
  freeze anything and cannot be adjudicated.
* Adjudication checks that `expected_result_version_id` is still the effective
  version inside a serialized write transaction. Two opposing decisions
  therefore cannot both apply: the loser gets `409`. Repeating the *same*
  decision for the *same* appeal and base version returns the original
  response unchanged (no new version row).

## Prize settlement

All money fields are non-negative **integer minor units** (`*_cents`; use fen
for CNY). Prize amounts are configured once per tournament; fund additions are
tournament rows and every balance query is filtered by `tournament_id`, so one
event can never spend another event's money. New settlement tables are created
by `init_db`, so old SQLite tournament files keep working and gain the finance
tables on restart.

The settlement state is derived from the same bracket/appeal rules:

* `decided` — the current final is complete and no pending appeal blocks the
  champion. Its result-version winner and loser receive the fixed champion and
  runner-up amounts.
* `suspended` — a pending appeal blocks champion declaration, or an upstream
  overturn superseded/invalidated the final. New payments and recoveries are
  rejected with `409 settlement_suspended`; existing orders can still be
  confirmed by staff.
* `awaiting_final` — the final has not produced an effective result yet.

A payment order snapshots `player_id`, `amount_cents`, `final_match_id`,
`result_version_id`, award role and award amount. Readjudication never updates
those fields. New issuance is limited by both:

```text
player capacity = current_prize_amount
                  - net_paid_cents
                  - unconfirmed_payment_cents
tournament available_funds_cents = funding_total + actual_recoveries
                                   - successful_payments
                                   - pending_payments
```

`net_paid_cents = successful_payments - actual_recoveries`. A failed bank
result is the only thing that releases that order's pending encumbrance; a
pending order keeps reserving funds because it may still succeed. Success posts
real money out and stores the bank receipt. A successful receipt has a partial
unique index, so it cannot be attached to a second payment. Repeating the same
idempotent confirmation returns the stored response; reusing the key with a
different request returns `409 idempotency_conflict`.

When settlement is `decided`, `pending_recovery_cents` is
`max(0, net_paid_cents - current_prize_amount_cents)`. A player no longer on
the final has current prize zero. Recoveries are actual receipts (partial
recovery is allowed) and immediately become available tournament funds. During
`suspended`, prize and recovery amounts are explicitly `null`; factual paid,
pending and payment-history fields remain visible, so suspension is not treated
as a universal refund order.

`GET /tournaments/{id}/settlement` returns:

* `prize_pool` with funding, cash balance, pending commitments and
  `available_funds_cents`;
* `players[]` with current prize, net paid, unconfirmed payments and pending
  recovery;
* `payments[]`, `recoveries[]` and `funding_additions[]`;
* `fund_movements[]`, including commitment events and real cash events;
* `award_versions[]` and non-cash `award_differences[]` for readjudication
  effects.

Canonical write bodies are:

```json
{"champion_amount_cents": 10000, "runner_up_amount_cents": 5000}
{"amount_cents": 3000, "source": "sponsor"}
{"player_id": 12, "amount_cents": 2000, "result_version_id": 8}
{"status": "success", "bank_receipt_number": "BANK-123"}
{"player_id": 12, "amount_cents": 5000, "result_version_id": 8}
```

Common path/field aliases (`/prizes`, `/funds`, `/clawbacks`,
`/payments/{id}/bank-result`, `receipt_number`, etc.) are accepted. All finance
writers use `BEGIN IMMEDIATE`; settlement reads use one SQLite snapshot, so
concurrent issuance, recovery, bank confirmation and appeals cannot over-commit
or return mixed versions.

## Verification

```
tests/test_bracket.py       seeds, start/result rules, idempotency, restart
tests/test_scheduling.py    candidate validation, lexical solver, cross-event
                            minimum closure repair, locks, idempotency/restart,
                            scheduled first starts, immutable usage, DB races
tests/test_appeals.py       suspend/uphold/overturn, history, untouched half,
                            version targeting, stale appeals, nested appeals
tests/test_robustness.py    replaced live bouts, races through HTTP, nested
                            overturn chains, durability
tests/test_concurrency_db.py  independent connections: one result wins,
                            one of two opposite adjudications wins
tests/test_settlement.py    prize/fund limits, commitments, late bank
                            confirmation, receipt uniqueness, recovery,
                            suspension/final invalidation, restart and
                            concurrent payment/recovery/receipt/appeal races
tests/test_admissions.py    capacity, one-time codes, online venue/time checks,
                            permit locks/reservations, durable offline gate,
                            atomic/late/idempotent settlement, schedule pins,
                            restart/timeout and concurrency races
```

The demo script prints, at each stage, the champion status and per-slot
`current`/`participants`/`history`/`pending_appeals` so the effect on SF1, the
final, and the untouched SF2 is directly visible.
