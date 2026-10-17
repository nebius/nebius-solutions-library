#!/usr/bin/env bash
# The registry static key's bootstrap and the launcher's secret-input gate, against a fake Nebius CLI and a fake
# Terraform (no cloud): the key is discovered and issued BEFORE it exists (output cloud and plan cloud need no secret
# input), a platform plan is refused without it and carries it as TF_VAR_registry_key with it, and the script's
# issue/refuse/rotate/id-fallback/import paths hold one lock. Run by tools/check.sh (tests).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/cfg" "$T/state"
export PATH="$T/bin:$PATH" SERVERLESS2_CONFIG_DIR="$T/cfg" RK_LOG="$T/log" TF_LOG_FILE="$T/tf.log" REGISTRY_KEY_LOCK_WAIT=1
export TFVARS="$ROOT/terraform.tfvars.example" STATE_CREDS="$T/tfstate.env"
printf 'AWS_ACCESS_KEY_ID=x\nAWS_SECRET_ACCESS_KEY=y\n' > "$STATE_CREDS"
unset REGISTRY_STATIC_KEY NEBIUS_IAM_TOKEN || true
export NGC_API_KEY=ngc HF_TOKEN=hf REGISTRY_CREDENTIALS='{}'
: > "$RK_LOG"

cat > "$T/bin/nebius" <<'FAKE'
#!/usr/bin/env bash
echo "$*" >> "$RK_LOG"
case "$1 $2 ${3:-}" in
  "iam get-access-token ") echo fake-token ;;
  "iam static-key issue") name=$(printf '%s\n' "$@" | grep -A1 -- '--name' | tail -1)
     if [ "${RK_NO_ID:-0}" = 1 ]; then printf '{"token":"tok-%s","operation":{"id":"op1"}}' "$name"
     else printf '{"token":"tok-%s","operation":{"id":"op1","resource_id":"statickey-%s"}}' "$name" "$name"; fi ;;
  "iam static-key find") tok=$(printf '%s\n' "$@" | grep -A1 -- '--token' | tail -1)
     # FindStaticKeyResponse: the key wrapped under static_key (a top-level metadata is NOT what the CLI returns)
     printf '{"static_key":{"metadata":{"id":"found-%s","parent_id":"project-fake","name":"%s-image-cache-x","created_at":"2026-10-14T06:40:17Z"},"spec":{"account":{"service_account":{"id":"serviceaccount-fake"}},"service":"CONTAINER_REGISTRY","expires_at":"2027-10-14T00:00:00Z"},"status":{"active":true}}}' "${tok#tok-}" "${tok#tok-}" ;;
  *) echo "unexpected nebius $*" >&2; exit 9 ;;
esac
FAKE
cat > "$T/bin/terraform" <<'FAKE'
#!/usr/bin/env bash
# the launcher's view of Terraform: console answers with the config plan, output with the cloud outputs, the rest succeeds
args=("$@"); sub=""
for a in "${args[@]}"; do case "$a" in -chdir=*) ;; *) sub="$a"; break ;; esac; done
echo "terraform $* TF_VAR_registry_key=${TF_VAR_registry_key:-<unset>}" >> "$TF_LOG_FILE"
case "$sub" in
  init) exit 0 ;;
  console) python3 -c '
import json
plan = {"name": "demo", "hub_id": "hub", "order": ["hub"], "state": {"bucket": "b", "region": "r", "endpoint": "e"},
        "clusters": {"hub": {"region": "eu-north1", "project_id": "p-hub", "roles": {}}}, "dedicated": {},
        "secrets": {"ngc_api_key": "NGC_API_KEY", "hf_token": "HF_TOKEN", "registry_credentials": "REGISTRY_CREDENTIALS", "registry_key": "REGISTRY_STATIC_KEY"}}
print(json.dumps(json.dumps(plan)))' ;;
  output) case "${args[*]}" in
    *" hub") echo '{"id": "hub", "project_id": "p-hub", "registry": "cr.example/p-hub"}' ;;
    *" clusters") echo '{"hub": {"ops_service_account_id": "sa-hub", "project_id": "p-hub"}}' ;;
    *) echo '{}' ;; esac ;;
  plan|apply|destroy) echo "fake $sub ok" ;;
  *) echo "unexpected terraform $*" >&2; exit 9 ;;
esac
FAKE
chmod +x "$T/bin/nebius" "$T/bin/terraform"
S="$ROOT/stack/scripts/registry-static-key.sh"; L="$ROOT/stack.sh"
fail() { echo "FAIL: $*" >&2; exit 1; }

# 1. no key in the environment: the cloud outputs and the cloud plan work (no secret input is asked for)
out=$("$L" output cloud -json hub 2>&1) || fail "output cloud refused without the key: $out"
grep -q '"project_id": "p-hub"' <<< "$out" || fail "output cloud answer: $out"
out=$("$L" tf cloud -- output -json clusters 2>&1) || fail "tf cloud output refused: $out"
out=$("$L" plan cloud 2>&1) || fail "plan cloud refused without the key: $out"
grep -q "fake plan ok" <<< "$out" || fail "plan cloud: $out"
out=$("$L" tf platform hub -- output -json 2>&1) || fail "read-only tf platform refused: $out"
# 2. the platform plan/apply is refused without it, before any terraform call
: > "$TF_LOG_FILE"
set +e; out=$("$L" plan platform hub 2>&1); rc=$?; set -e
[ "$rc" = 4 ] || fail "plan platform without the key: rc $rc: $out"
grep -q "REGISTRY_STATIC_KEY is not set" <<< "$out" || fail "plan platform message: $out"
grep -q "plan" "$TF_LOG_FILE" && fail "a platform plan ran without the key"
set +e; out=$("$L" tf platform hub -- apply 2>&1); rc=$?; set -e
[ "$rc" = 4 ] || fail "tf platform apply without the key: rc $rc"
# 3. with it, the platform plan runs and carries TF_VAR_registry_key
: > "$TF_LOG_FILE"
out=$(REGISTRY_STATIC_KEY=tok-live "$L" plan platform hub 2>&1) || fail "plan platform with the key: $out"
grep -q "plan .*TF_VAR_registry_key=tok-live" "$TF_LOG_FILE" || fail "TF_VAR_registry_key not exported: $(cat "$TF_LOG_FILE")"
# 4. bootstrap: no key anywhere, no --project/--sa: discovery through the launcher, then one issue with the hub's ids
out=$("$S" demo 2>&1) || fail "bootstrap issue: $out"
grep -q -- "--parent-id p-hub --account-service-account-id sa-hub --service CONTAINER_REGISTRY --name demo-image-cache-" "$RK_LOG" || fail "issue call: $(cat "$RK_LOG")"
grep -q "export REGISTRY_STATIC_KEY='tok-demo-image-cache-" <<< "$out" || fail "export line: $out"
python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); assert d["id"].startswith("statickey-demo-image-cache-"), d' "$T/cfg/demo/registry-key.json"
[ "$(stat -c %a "$T/cfg/demo/registry-key.json")" = 600 ] || fail "key file mode"
grep -q "iam static-key issue" "$RK_LOG" || fail "no issue"
# 5. a second call issues nothing; --rotate issues and names the old id; a response without an id is resolved by find
n=$(grep -c "static-key issue" "$RK_LOG")
"$S" demo > /dev/null 2>&1 || fail "second call"
[ "$(grep -c 'static-key issue' "$RK_LOG")" = "$n" ] || fail "re-issued without --rotate"
out=$("$S" demo --rotate --project p1 --sa sa1 2>&1) || fail "rotate: $out"
grep -q "previous key id statickey-demo-image-cache-" <<< "$out" || fail "rotate did not name the old id: $out"
out=$(RK_NO_ID=1 "$S" demo2 --project p1 --sa sa1 2>&1) || fail "no-id issue: $out"
grep -q "key id found-demo2-image-cache-" <<< "$out" || fail "find fallback: $out"
# 6. import of the former cache (empty id) resolves the id; a second import without --rotate is refused
printf '{"key": "tok-old", "id": ""}' > "$T/old.json"
out=$("$S" demo3 --import "$T/old.json" 2>&1) || fail "import: $out"
grep -q "key id found-old" <<< "$out" || fail "import id: $out"
set +e; "$S" demo3 --import "$T/old.json" > /dev/null 2>&1; rc=$?; set -e
[ "$rc" = 1 ] || fail "import over an existing file was not refused"
# 7. import and issue hold the same lock: with the lock taken, both are refused instead of racing
mkdir -p "$T/cfg/demo4"; exec 8>"$T/cfg/demo4/.registry-key.lock"; flock 8
set +e; out=$("$S" demo4 --import "$T/old.json" 2>&1); rc=$?; set -e
if [ "$rc" != 1 ] || ! grep -q "another issue or import is running" <<< "$out"; then fail "import ignored the lock: rc $rc $out"; fi
set +e; out=$("$S" demo4 --project p1 --sa sa1 2>&1); rc=$?; set -e
[ "$rc" = 1 ] || fail "issue ignored the lock: rc $rc $out"
flock -u 8
[ ! -e "$T/cfg/demo4/registry-key.json" ] || fail "a key file appeared under the lock"
echo "   ok registry key: discovery and cloud plan without the key, platform plan gated, bootstrap issue, rotate, id fallback, import, lock"
