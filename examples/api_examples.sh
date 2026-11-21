#!/usr/bin/env bash
# Directly callable API walkthrough using only curl + jq.
#
# Scenario: checkout-service, 6 replicas, batch cap 2, 30s minimum stable
# window, 5% error-rate threshold. The script drives two successful batches,
# changes the rollout config mid-flight while a promote is pending, then
# shows a threshold breach forcing rollback on a second rollout.
#
# Usage:
#   docker compose up -d            # start the API first
#   bash examples/api_examples.sh   # then run this script
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
need() { command -v "$1" >/dev/null || { echo "missing dependency: $1" >&2; exit 1; }; }
need curl
need jq

post() { curl -fsS -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
put()  { curl -fsS -X PUT  "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
get()  { curl -fsS "$BASE$1"; }

now_iso() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

echo "== 1. create rollout: 6 replicas, cap 2/batch, 30s window, 5% threshold =="
ROLLOUT=$(post /rollouts '{
  "service": "checkout-service",
  "target_version": "v2.3.1",
  "total_replicas": 6,
  "max_replicas_per_batch": 2,
  "min_stable_window_seconds": 30,
  "error_rate_threshold": 0.05
}')
RID=$(echo "$ROLLOUT" | jq -r .id)
echo "rollout id: $RID (config_version $(echo "$ROLLOUT" | jq .config_version))"

echo
echo "== 2. first observation with no data -> evaluate holds (pause) =="
get "/rollouts/$RID" | jq '{status, config_version, pending_config}'
EVAL=$(post "/rollouts/$RID/evaluate" '{}')
echo "$EVAL" | jq '{action: .decision.action, reason: .message, status: .rollout.status}'

echo
echo "== 3. report gen-0 replicas (2 ready) and a 30s healthy window =="
for i in 1 2; do
  post "/rollouts/$RID/replicas" "{\"generation\":0,\"replica_id\":\"canary-$i\",\"state\":\"ready\",\"observed_version\":\"v2.3.1\"}" >/dev/null
done
post "/rollouts/$RID/metrics" "{
  \"generation\": 0,
  \"window_started_at\": \"$(now_iso)\",
  \"window_seconds\": 30,
  \"request_count\": 1000,
  \"error_count\": 4
}" >/dev/null

echo "== 4. evaluate -> promote 0 -> 2 (decision pending execution) =="
EVAL=$(post "/rollouts/$RID/evaluate" '{}')
DID=$(echo "$EVAL" | jq -r .decision.id)
echo "$EVAL" | jq '{action: .decision.action, from: .decision.from_replicas, to: .decision.to_replicas, reason: .message}'

echo
echo "== 5. operator loosens the error-rate threshold mid-rollout =="
echo "-- a promote is pending, so the change is queued as the pending config"
put "/rollouts/$RID/config" '{"base_version":0,"error_rate_threshold":0.2}' \
  | jq '{applied, message, effective_threshold: .rollout.error_rate_threshold, pending: .rollout.pending_config}'

echo
echo "-- a submission based on the superseded version 0 is rejected (409)"
curl -s -o /dev/null -w 'stale base_version=0 -> HTTP %{http_code}\n' \
  -X PUT "$BASE/rollouts/$RID/config" -H 'Content-Type: application/json' \
  -d '{"base_version":0,"error_rate_threshold":0.9}'

echo
echo "-- based on the queued v1, a newer change replaces the pending config"
put "/rollouts/$RID/config" '{"base_version":1,"error_rate_threshold":0.3}' \
  | jq '{applied, pending: .rollout.pending_config}'

echo
echo "== 6. platform reports successful execution; pending config activates =="
post "/rollouts/$RID/decisions/$DID/result" '{"status":"succeeded","detail":"deployment scaled to 2"}' \
  | jq '{message, status: .rollout.status, replicas: .rollout.current_replicas,
         generation: .rollout.current_generation,
         config_version: .rollout.config_version,
         effective_threshold: .rollout.error_rate_threshold,
         pending_config: .rollout.pending_config}'

echo
echo "== 7. stale (gen-0) observation after the advance -> recorded but ignored =="
post "/rollouts/$RID/replicas" '{"generation":0,"replica_id":"late-canary","state":"ready"}' | jq .

echo
echo "== 8. gen-1 reports: 2 old + 2 new canaries ready, healthy window =="
for i in 1 2 3 4; do
  post "/rollouts/$RID/replicas" "{\"generation\":1,\"replica_id\":\"canary-$i\",\"state\":\"ready\",\"observed_version\":\"v2.3.1\"}" >/dev/null
done
post "/rollouts/$RID/metrics" "{
  \"generation\": 1,
  \"window_started_at\": \"$(now_iso)\",
  \"window_seconds\": 30,
  \"request_count\": 2000,
  \"error_count\": 10
}" >/dev/null
EVAL=$(post "/rollouts/$RID/evaluate" '{}')
DID=$(echo "$EVAL" | jq -r .decision.id)
echo "$EVAL" | jq '{action: .decision.action, from: .decision.from_replicas, to: .decision.to_replicas}'
post "/rollouts/$RID/decisions/$DID/result" '{"status":"succeeded","detail":"scaled to 4"}' >/dev/null

echo
echo "== 9. decisions recorded so far =="
get "/rollouts/$RID/decisions" | jq '.decisions[] | {generation, action, from_replicas, to_replicas, execution_status, reason}'

echo
echo "== 10. second rollout: error rate breaches threshold -> rollback =="
RB=$(post /rollouts '{
  "service": "payment-service",
  "target_version": "v9.0.0",
  "total_replicas": 4,
  "max_replicas_per_batch": 1,
  "min_stable_window_seconds": 10,
  "error_rate_threshold": 0.05
}')
RBID=$(echo "$RB" | jq -r .id)
post "/rollouts/$RBID/replicas" '{"generation":0,"replica_id":"c1","state":"ready","observed_version":"v9.0.0"}' >/dev/null
post "/rollouts/$RBID/metrics" "{
  \"generation\": 0,
  \"window_started_at\": \"$(now_iso)\",
  \"window_seconds\": 15,
  \"request_count\": 100,
  \"error_count\": 20
}" >/dev/null
EVAL=$(post "/rollouts/$RBID/evaluate" '{}')
RDID=$(echo "$EVAL" | jq -r .decision.id)
echo "$EVAL" | jq '{action: .decision.action, reason: .message, error_rate: .decision.detail.metrics.error_rate}'

echo
echo "-- config is frozen while a rollback is pending execution (409)"
curl -s -o /dev/null -w 'config change during pending rollback -> HTTP %{http_code}\n' \
  -X PUT "$BASE/rollouts/$RBID/config" -H 'Content-Type: application/json' \
  -d '{"base_version":0,"error_rate_threshold":0.5}'

echo
echo "== 11. rollback execution succeeds -> terminal rolled_back =="
post "/rollouts/$RBID/decisions/$RDID/result" '{"status":"succeeded","detail":"traffic shifted to v8"}' \
  | jq '{status: .rollout.status, replicas: .rollout.current_replicas, generation: .rollout.current_generation}'

echo
echo "-- a terminal rollout rejects further config changes (409)"
curl -s -o /dev/null -w 'config change after rolled_back -> HTTP %{http_code}\n' \
  -X PUT "$BASE/rollouts/$RBID/config" -H 'Content-Type: application/json' \
  -d '{"base_version":0,"error_rate_threshold":0.5}'

echo
echo "done. inspect stored (including ignored) observations:"
echo "  curl -s $BASE/rollouts/$RID/observations | jq"
