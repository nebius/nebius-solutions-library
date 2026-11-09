#!/bin/sh
# The acceptance probe of a fleet (README.md quick start, docs/OPERATIONS.md "Checks after any change"): with a tenant
# key, lists the models, runs the built-in run class `acceptance.model` (hello-run) to SUCCEEDED and, with
# `acceptance.example_endpoint` (or --example), defines the example endpoint `llm-example` through the API with the
# first admin key and calls it once (a cold start in a fresh region pulls a 10 GB image: up to 11 min measured;
# the call is retried for 20 minutes). Exit 0 = the fleet serves runs and endpoints end to end.
#
#   tools/verify.sh [--example | --no-example]   from the operator machine: the inputs come from the platform stage of
#                                                the control cluster (`./stack.sh output platform control -json acceptance`)
#   API=... KEY=... MODEL=... [EXAMPLE=1 ADMIN_KEY=... SPEC=...] tools/verify.sh   the same probe with the inputs given:
#                                                what the apply-time Job runs (terraform.tfvars `acceptance.probe = true`,
#                                                stack/platform/tenants.tf) with sh and curl only
# Environment: TFVARS (default ./terraform.tfvars) for the operator form.
set -eu
ROOT="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd || echo .)"
example_arg=""
for a in "$@"; do case "$a" in --example) example_arg=1 ;; --no-example) example_arg=0 ;; *) sed -n 2,14p "$0" >&2; exit 2 ;; esac; done

if [ -z "${API:-}" ]; then
  # operator form: the inputs from the control cluster's platform state, as shell assignments
  eval "$("$ROOT/stack.sh" output platform control -json acceptance | python3 -c '
import json, shlex, sys
a = json.load(sys.stdin)
if not a or not a.get("key"):
    sys.exit("no tenant key on the control cluster (terraform.tfvars tenants.<name>.keys)")
print("API=%s; KEY=%s; ADMIN_KEY=%s; MODEL=%s; EXAMPLE=%s; SPEC=%s" % tuple(shlex.quote(str(v)) for v in (
    a["api_url"], a["key"], a.get("admin_key") or "", a["model"], "1" if a.get("example") else "0", json.dumps(a["spec"]) if a.get("spec") else "")))')"
fi
[ -z "$example_arg" ] || EXAMPLE="$example_arg"
EXAMPLE="${EXAMPLE:-0}"; MODEL="${MODEL:-hello-run}"; ADMIN_KEY="${ADMIN_KEY:-}"; SPEC="${SPEC:-}"
if [ "$EXAMPLE" = 1 ] && { [ -z "$ADMIN_KEY" ] || [ -z "$SPEC" ]; }; then
  echo "!! the example endpoint needs an admin key (tenants.<name>.keys.<key>.admin = true) and a GPU class" >&2; exit 2
fi
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
H="Authorization: Bearer $KEY"

echo "== models ($API)"
curl -sf -H "$H" "$API/v1/models" > "$TMP/models.json"
grep -q "\"$MODEL\"" "$TMP/models.json" || { echo "!! run class $MODEL is not listed"; exit 1; }

echo "== run $MODEL"
id=$(curl -sf -X POST -H "$H" -H 'content-type: application/json' -d '{"mode":"run","name":"acceptance-probe","input":{}}' "$API/v1/models/$MODEL:invoke" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
[ -n "$id" ] || { echo "!! no operation id from the invoke"; exit 1; }
echo "operation $id"
st=""
for _ in $(seq 1 120); do
  st=$(curl -sf -H "$H" "$API/v1/operations/$id" | sed -n 's/.*"status":"\([A-Z_]*\)".*/\1/p')
  echo "$(date -u +%T) $st"
  case "$st" in SUCCEEDED) break ;; FAILED|CANCELLED|EXPORT_FAILED) exit 1 ;; esac
  sleep 10
done
[ "$st" = SUCCEEDED ] || { echo "!! run $id did not succeed in 20 minutes"; exit 1; }

if [ "$EXAMPLE" = 1 ]; then
  mid=$(printf '%s' "$SPEC" | sed -n 's/.*"id": *"\([^"]*\)".*/\1/p')
  echo "== example model $mid: defined through the API (admin key), then called"
  code=$(curl -s -o "$TMP/m" -w '%{http_code}' -X POST -H "Authorization: Bearer $ADMIN_KEY" -H 'content-type: application/json' -d "$SPEC" "$API/v1/models" || echo 000)
  echo "POST /v1/models HTTP $code"; head -c 600 "$TMP/m"; echo
  case "$code" in 201) ;; 409) echo "exists already (kept)" ;; *) exit 1 ;; esac
  for _ in $(seq 1 40); do
    code=$(curl -s -o "$TMP/e" -w '%{http_code}' --max-time 660 -X POST -H "$H" -H 'content-type: application/json' \
      -d '{"mode":"sync","input":{"messages":[{"role":"user","content":"Say hello in three words."}],"max_tokens":16}}' \
      "$API/v1/models/$mid:invoke" || echo 000)
    echo "$(date -u +%T) HTTP $code"
    if [ "$code" = 200 ]; then head -c 400 "$TMP/e"; echo; echo "== ok: run class and example endpoint answered"; exit 0; fi
    sleep 30
  done
  echo "!! the example endpoint did not answer in 20 minutes"; exit 1
fi
echo "== ok: run class $MODEL succeeded"
