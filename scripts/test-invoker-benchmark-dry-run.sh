#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -d "$SCRIPT_DIR/../bin" && -d "$SCRIPT_DIR/../config" ]]; then
  BENCHMARK_SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
  REPO_ROOT="$(cd "$BENCHMARK_SOURCE_ROOT/.." && pwd)"
else
  REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
  BENCHMARK_SOURCE_ROOT="$REPO_ROOT/invoker-benchmarks"
fi
USAGE_COSTING_SCRIPT="$BENCHMARK_SOURCE_ROOT/scripts/usage_costing.py"
if [[ ! -f "$USAGE_COSTING_SCRIPT" ]]; then
  USAGE_COSTING_SCRIPT="$REPO_ROOT/scripts/usage_costing.py"
fi
TOKEN_LEDGER_SCRIPT="$BENCHMARK_SOURCE_ROOT/scripts/token_ledger.py"
if [[ ! -f "$TOKEN_LEDGER_SCRIPT" ]]; then
  TOKEN_LEDGER_SCRIPT="$REPO_ROOT/scripts/token_ledger.py"
fi
TMP_ROOT="$(mktemp -d /tmp/invoker-benchmark-test.XXXXXX)"
EMPTY_ROOT=""
trap 'rm -rf "$TMP_ROOT" ${EMPTY_ROOT:+"$EMPTY_ROOT"}' EXIT

mkdir -p "$TMP_ROOT/config" "$TMP_ROOT/corpus/submit-to-invoker-sessions-2026-05-26" "$TMP_ROOT/bin" "$TMP_ROOT/lib"
cp -R "$BENCHMARK_SOURCE_ROOT/bin/." "$TMP_ROOT/bin/"
cp -R "$BENCHMARK_SOURCE_ROOT/lib/." "$TMP_ROOT/lib/"
cp "$BENCHMARK_SOURCE_ROOT/config/corpus-manifest.json" "$TMP_ROOT/config/corpus-manifest.json"

for index in $(seq -w 1 48); do
  printf '{"session":"%s"}\n' "$index" > "$TMP_ROOT/corpus/submit-to-invoker-sessions-2026-05-26/session-$index.jsonl"
done

cat > "$TMP_ROOT/config/benchmark.env" <<EOF
TZ=Asia/Hong_Kong
BENCHMARK_ROOT=$TMP_ROOT
INVOKER_REPO=https://github.com/Neko-Catpital-Labs/Invoker.git
INVOKER_BRANCH=master
INVOKER_SHA=test-sha
CORPUS_DIR=$TMP_ROOT/corpus/submit-to-invoker-sessions-2026-05-26
MODELS=codex,claude
MODES=baseline_direct,invoker_workflow,invoker_auto_fix
WORKER_CONCURRENCY_PER_HOST=1
MIXPANEL_PROJECT_ID=4027782
MIXPANEL_SCHEMA_VERSION=invoker_benchmark_v1
EOF

cat > "$TMP_ROOT/config/workers.json" <<'EOF'
{
  "version": 1,
  "coordinator": "remote_digital_ocean_1",
  "workers": [
    {"name": "remote_digital_ocean_2", "host": "157.245.231.246", "user": "invoker", "port": 22, "enabled": true},
    {"name": "remote_digital_ocean_3", "host": "165.22.161.97", "user": "invoker", "port": 22, "enabled": true},
    {"name": "remote_digital_ocean_4", "host": "138.68.230.225", "user": "invoker", "port": 22, "enabled": true},
    {"name": "remote_linode_1", "host": "173.230.140.51", "user": "invoker", "port": 22, "enabled": true}
  ]
}
EOF

output="$("$TMP_ROOT/bin/run-nightly-benchmark.sh" --dry-run --env-file "$TMP_ROOT/config/benchmark.env")"
grep -E "conversation_count=|model_count=|mode_count=|job_count=|worker_count=" <<<"$output"
grep -q "job_count=288" <<<"$output"
grep -q "worker_count=4" <<<"$output"

matrix_file="$(find "$TMP_ROOT/runs" -name job-matrix.tsv -print -quit)"
assignments_file="$(find "$TMP_ROOT/runs" -name worker-assignments.tsv -print -quit)"
run_log="$(find "$TMP_ROOT/runs" -name run.log -print -quit)"
config_snapshot="$(find "$TMP_ROOT/runs" -name config.json -print -quit)"
[[ -f "$matrix_file" ]] || { echo "Missing job matrix" >&2; exit 1; }
[[ -f "$assignments_file" ]] || { echo "Missing assignments" >&2; exit 1; }
[[ -f "$run_log" ]] || { echo "Missing run log" >&2; exit 1; }
[[ -f "$config_snapshot" ]] || { echo "Missing config snapshot" >&2; exit 1; }
[[ "$(wc -l < "$matrix_file" | tr -d ' ')" == "288" ]] || { echo "Unexpected matrix size" >&2; exit 1; }
[[ "$(wc -l < "$assignments_file" | tr -d ' ')" == "288" ]] || { echo "Unexpected assignment size" >&2; exit 1; }
grep -q "config models=codex,claude modes=baseline_direct,invoker_workflow,invoker_auto_fix" "$run_log"
python3 - "$config_snapshot" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1]))
assert payload["status"] == "succeeded"
assert payload["phase"] == "dry_run_complete"
assert payload["models"] == ["codex", "claude"]
assert payload["modes"] == ["baseline_direct", "invoker_workflow", "invoker_auto_fix"]
PY

smoke_output="$("$TMP_ROOT/bin/run-nightly-benchmark.sh" --dry-run --smoke --env-file "$TMP_ROOT/config/benchmark.env")"
grep -E "conversation_count=|model_count=|mode_count=|job_count=|worker_count=" <<<"$smoke_output"
grep -q "job_count=3" <<<"$smoke_output"

limit_output="$("$TMP_ROOT/bin/run-nightly-benchmark.sh" --dry-run --limit 6 --env-file "$TMP_ROOT/config/benchmark.env")"
grep -E "conversation_count=|model_count=|mode_count=|job_count=|worker_count=" <<<"$limit_output"
grep -q "job_count=6" <<<"$limit_output"

cat > "$TMP_ROOT/config/job-set.json" <<'EOF'
{
  "jobs": [
    {"file": "session-03.jsonl", "model": "codex", "mode": "invoker_workflow"},
    {"file": "session-01.jsonl", "model": "codex", "mode": "baseline_direct"},
    {"file": "session-02.jsonl", "model": "codex", "mode": "invoker_auto_fix", "run_id": "custom-session-02-autofix"}
  ]
}
EOF
job_set_output="$("$TMP_ROOT/bin/run-nightly-benchmark.sh" --dry-run --job-set "$TMP_ROOT/config/job-set.json" --env-file "$TMP_ROOT/config/benchmark.env")"
grep -E "conversation_count=|model_count=|mode_count=|job_count=|worker_count=" <<<"$job_set_output"
grep -q "job_count=3" <<<"$job_set_output"
job_set_matrix="$(find "$TMP_ROOT/runs" -name job-matrix.tsv -print | sort | tail -1)"
python3 - "$job_set_matrix" "$TMP_ROOT/corpus/submit-to-invoker-sessions-2026-05-26" <<'PY'
import sys
from pathlib import Path
rows = [line.split("\t") for line in Path(sys.argv[1]).read_text().splitlines()]
corpus = Path(sys.argv[2])
assert rows == [
    ["session-03__codex__invoker_workflow", str(corpus / "session-03.jsonl"), "session-03", "codex", "invoker_workflow"],
    ["session-01__codex__baseline_direct", str(corpus / "session-01.jsonl"), "session-01", "codex", "baseline_direct"],
    ["custom-session-02-autofix", str(corpus / "session-02.jsonl"), "session-02", "codex", "invoker_auto_fix"],
], rows
PY

ledger_fixture="$TMP_ROOT/ledger-fixture"
mkdir -p "$ledger_fixture/raw/.codex/sessions"
cat > "$ledger_fixture/session-11.jsonl" <<'EOF'
{"session":"11"}
EOF
cat > "$ledger_fixture/generated-plan.yaml" <<'EOF'
name: ledger fixture
repoUrl: https://example.test/repo.git
mergeMode: manual
tasks:
  - id: setup
    title: Setup
    command: echo setup
  - id: prompt-a
    title: Prompt A
    dependencies: [setup]
    prompt: |
      Do prompt A.
  - id: prompt-b
    title: Prompt B
    dependencies:
      - prompt-a
    prompt: |
      Do prompt B.
EOF
cat > "$ledger_fixture/raw/.codex/sessions/planner.jsonl" <<'EOF'
{"payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":10,"reasoning_output_tokens":5,"total_tokens":135}}},"sessionId":"planner-session","model":"gpt-test","costUSD":0.01}
EOF
cat > "$ledger_fixture/stdout.log" <<'EOF'
{"task_id":"prompt-a","agent_session_id":"agent-a","model":"gpt-test","usage":{"input_tokens":30,"output_tokens":5,"total_tokens":35},"costUSD":0.03}
{"task_id":"prompt-b","agent_session_id":"agent-b","model":"gpt-test","usage":{"input_tokens":40,"output_tokens":6,"total_tokens":46},"costUSD":0.04}
{"task_id":"prompt-b","agent_session_id":"agent-b-fix","kind":"autofix_retry","model":"gpt-test","usage":{"input_tokens":20,"output_tokens":4,"total_tokens":24},"costUSD":0.02}
EOF
cat > "$ledger_fixture/pricing.json" <<'EOF'
{}
EOF
PYTHONPATH="$BENCHMARK_SOURCE_ROOT/scripts" python3 "$TOKEN_LEDGER_SCRIPT" \
  --raw-sessions-dir "$ledger_fixture/raw" \
  --stdout-log "$ledger_fixture/stdout.log" \
  --generated-plan "$ledger_fixture/generated-plan.yaml" \
  --token-usage-out "$ledger_fixture/token-usage.json" \
  --ledger-out "$ledger_fixture/token-ledger.jsonl" \
  --cost-calculation-out "$ledger_fixture/cost-calculation.json" \
  --model codex \
  --batch-id ledger-batch \
  --run-id session-11__codex__invoker_auto_fix \
  --conversation-file "$ledger_fixture/session-11.jsonl" \
  --mode invoker_auto_fix \
  --pricing-source "$ledger_fixture/pricing.json"
python3 - "$ledger_fixture/token-ledger.jsonl" "$ledger_fixture/token-usage.json" <<'PY'
import json
import sys
rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
usage = json.load(open(sys.argv[2]))
assert len(rows) == 4, rows
assert {row["scenario_key"] for row in rows} == {"session-11/invoker_auto_fix/codex"}
assert {row["phase"] for row in rows} == {"planning", "invoker_prompt_task", "invoker_autofix_retry"}
assert usage["scenario_key"] == "session-11/invoker_auto_fix/codex"
assert abs(usage["estimated_cost_usd"] - 0.10) < 0.000001, usage
assert abs(usage["estimated_cost_usd"] - usage["planning_cost_usd"] - usage["invoker_prompt_task_cost_usd"] - usage["autofix_retry_cost_usd"]) < 0.000001
assert usage["model_call_count"] == 4
assert usage["prompt_model_call_count"] == 2
assert usage["autofix_model_call_count"] == 1
assert usage["cost_task_ids"] == ["prompt-a", "prompt-b"], usage
assert usage["dependent_task_ids"] == ["setup", "prompt-a", "prompt-b"], usage
assert usage["dependent_prompt_task_ids"] == ["prompt-a", "prompt-b"], usage
assert usage["dependent_autofix_task_ids"] == ["prompt-b"], usage
assert usage["cost_breakdown_complete"] is True
PY

fake_batch="$TMP_ROOT/runs/fake-batch"
fake_run_id="019e1b94-1c63-7e02-a60f-febd3e3f2ff4__codex__baseline_direct"
mkdir -p "$fake_batch/jobs/$fake_run_id"
cat > "$fake_batch/summary.json" <<'EOF'
{
  "batch_id": "fake-batch",
  "invoker_sha": "test-sha",
  "job_count": 1,
  "setup_status": "succeeded",
  "status_counts": {"succeeded": 1}
}
EOF
cat > "$fake_batch/jobs/$fake_run_id/job.json" <<EOF
{
  "batch_id": "fake-batch",
  "run_id": "$fake_run_id",
  "test_id": "019e1b94-1c63-7e02-a60f-febd3e3f2ff4",
  "conversation_id": "019e1b94-1c63-7e02-a60f-febd3e3f2ff4",
  "corpus_case_id": "session-01",
  "model": "codex",
  "mode": "baseline_direct",
  "scenario": "baseline_direct",
  "execution_surface": "baseline",
  "autofix_enabled": false,
  "invoker_sha": "test-sha",
  "status": "succeeded",
  "result": "pass",
  "exit_code": 0,
  "commits": [],
  "changed_files": [],
  "test_configuration": {},
  "artifacts": {"prompt": "prompt.txt"}
}
EOF
cat > "$fake_batch/jobs/$fake_run_id/token-usage.json" <<'EOF'
{
  "scenario_key": "session-01/baseline_direct/codex",
  "input_tokens": 1000,
  "cache_read_tokens": 300,
  "cache_creation_tokens": 100,
  "fresh_input_tokens": 800,
  "output_tokens": 200,
  "reasoning_tokens": 50,
  "total_tokens": 1250,
  "normalized_total_tokens": 1050,
  "estimated_cost_usd": 0.001555,
  "derived_total_cost_usd": 0.001555,
  "billable_model": "gpt-test",
  "billable_model_source": "session_log",
  "pricing_missing": false,
  "pricing_source": "litellm_model_prices"
}
EOF
cat > "$fake_batch/jobs/$fake_run_id/token-ledger.jsonl" <<'EOF'
{"scenario_key":"session-01/baseline_direct/codex","model_call_id":"call-1","phase":"planning","task_id":"","agent_session_id":"planner","provider":"openai","model":"gpt-test","billable_model":"gpt-test","billable_model_source":"session_log","input_tokens":1000,"cache_read_tokens":300,"cache_creation_tokens":100,"output_tokens":200,"reasoning_tokens":50,"total_tokens":1250,"estimated_cost_usd":0.001555,"derived_total_cost_usd":0.001555,"pricing_missing":false,"pricing_source":"litellm_model_prices","source":"fixture"}
EOF
BENCHMARK_ROOT=/home/invoker/invoker-benchmarks "$TMP_ROOT/bin/emit-mixpanel-events.sh" --batch-dir "$fake_batch" >/dev/null
python3 - "$fake_batch/mixpanel-export.jsonl" <<'PY'
import json
import sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
event_names = {item["event"] for item in events}
assert event_names == {"benchmark_batch", "benchmark_run", "benchmark_task", "benchmark_token_usage", "benchmark_model_call"}
for item in events:
    assert item["properties"]["invoker_sha"] == "test-sha", item
run = next(item["properties"] for item in events if item["event"] == "benchmark_run")
model_call = next(item["properties"] for item in events if item["event"] == "benchmark_model_call")
assert run["test_id"] == "019e1b94-1c63-7e02-a60f-febd3e3f2ff4"
assert run["corpus_case_id"] == "session-01"
assert run["scenario_key"] == "session-01/baseline_direct/codex"
assert run["execution_surface"] == "baseline"
assert run["autofix_enabled"] is False
assert run["estimated_cost_usd"] == 0.001555
assert run["derived_total_cost_usd"] == 0.001555
assert model_call["model_call_id"] == "call-1"
assert model_call["scenario_key"] == "session-01/baseline_direct/codex"
assert model_call["phase"] == "planning"
assert model_call["agent_session_id"] == "planner"
assert run["job_artifact_path"] == "/home/invoker/invoker-benchmarks/runs/fake-batch/jobs/" + run["run_id"]
assert run["failure_stage"] == ""
assert run["failure_reason"] == ""
assert run["failure_message"] == ""
for props in (item["properties"] for item in events):
    forbidden = {
        "cost_formula",
        "pricing_input_cost_per_token",
        "pricing_cache_read_input_token_cost",
        "pricing_cache_creation_input_token_cost",
        "pricing_output_cost_per_token",
        "derived_non_cache_input_cost_usd",
        "derived_cache_read_cost_usd",
        "derived_cache_creation_cost_usd",
        "derived_output_cost_usd",
    }
    overlap = forbidden & set(props)
    if overlap:
        raise AssertionError(f"verbose cost fields leaked to Mixpanel: {sorted(overlap)}")
PY

failed_run_id="failed-invoker__claude__invoker_workflow"
mkdir -p "$fake_batch/jobs/$failed_run_id"
cat > "$fake_batch/jobs/$failed_run_id/job.json" <<EOF
{
  "batch_id": "fake-batch",
  "run_id": "$failed_run_id",
  "test_id": "failed-invoker",
  "conversation_id": "failed-invoker",
  "corpus_case_id": "session-failed",
  "model": "claude",
  "mode": "invoker_workflow",
  "scenario": "invoker_workflow",
  "execution_surface": "invoker",
  "autofix_enabled": false,
  "invoker_sha": "test-sha",
  "status": "failed",
  "result": "fail",
  "exit_code": 1,
  "failure_stage": "invoker_cli_run",
  "failure_reason": "claude_auth_failed",
  "failure_message": "Failed to authenticate. API Error: 401",
  "commits": [],
  "changed_files": [],
  "test_configuration": {},
  "artifacts": {"prompt": "prompt.txt"}
}
EOF
BENCHMARK_ROOT=/home/invoker/invoker-benchmarks "$TMP_ROOT/bin/emit-mixpanel-events.sh" --batch-dir "$fake_batch" >/dev/null
python3 - "$fake_batch/mixpanel-export.jsonl" "$failed_run_id" <<'PY'
import json
import sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
run = next(item["properties"] for item in events if item["event"] == "benchmark_run" and item["properties"].get("run_id") == sys.argv[2])
task = next(item["properties"] for item in events if item["event"] == "benchmark_task" and item["properties"].get("run_id") == sys.argv[2])
assert run["failure_stage"] == "invoker_cli_run"
assert run["failure_reason"] == "claude_auth_failed"
assert run["failure_message"] == "Failed to authenticate. API Error: 401"
assert task["failure_stage"] == "invoker_cli_run"
assert task["failure_reason"] == "claude_auth_failed"
PY

python3 - "$fake_batch/jobs/$fake_run_id/job.json" <<'PY'
import json
import sys
path = sys.argv[1]
payload = json.load(open(path))
payload.pop("invoker_sha")
open(path, "w").write(json.dumps(payload, indent=2, sort_keys=True))
PY
BENCHMARK_ROOT=/home/invoker/invoker-benchmarks "$TMP_ROOT/bin/emit-mixpanel-events.sh" --batch-dir "$fake_batch" >/dev/null
python3 - "$fake_batch/mixpanel-export.jsonl" <<'PY'
import json
import sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
for item in events:
    assert item["properties"]["invoker_sha"] == "test-sha", item
PY

missing_sha_batch="$TMP_ROOT/runs/missing-sha-batch"
missing_sha_run_id="missing-sha__codex__baseline_direct"
mkdir -p "$missing_sha_batch/jobs/$missing_sha_run_id"
cat > "$missing_sha_batch/summary.json" <<'EOF'
{
  "batch_id": "missing-sha-batch",
  "job_count": 1,
  "setup_status": "succeeded",
  "status_counts": {"succeeded": 1}
}
EOF
cat > "$missing_sha_batch/jobs/$missing_sha_run_id/job.json" <<EOF
{
  "batch_id": "missing-sha-batch",
  "run_id": "$missing_sha_run_id",
  "test_id": "missing-sha",
  "conversation_id": "missing-sha",
  "model": "codex",
  "mode": "baseline_direct",
  "scenario": "baseline_direct",
  "status": "succeeded",
  "result": "pass",
  "exit_code": 0
}
EOF
if "$TMP_ROOT/bin/emit-mixpanel-events.sh" --batch-dir "$missing_sha_batch" >"$missing_sha_batch/export.out" 2>&1; then
  echo "Expected benchmark export without invoker_sha to fail" >&2
  exit 1
fi
grep -q "missing invoker_sha" "$missing_sha_batch/export.out"

fake_invoker_repo="$TMP_ROOT/fake-invoker-repo"
mkdir -p "$fake_invoker_repo/packages/cli/dist" "$fake_invoker_repo/packages/app/dist" "$fake_invoker_repo/scripts"
cat > "$fake_invoker_repo/scripts/kill-all-electron.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$fake_invoker_repo/scripts/kill-all-electron.sh"
cat > "$fake_invoker_repo/package.json" <<'EOF'
{
  "private": true,
  "workspaces": ["packages/*"],
  "scripts": {
    "build": "npm run build --workspace @invoker/cli"
  }
}
EOF
cat > "$fake_invoker_repo/packages/cli/package.json" <<'EOF'
{
  "name": "@invoker/cli",
  "private": true,
  "scripts": {
    "build": "node build.js"
  }
}
EOF
cat > "$fake_invoker_repo/packages/cli/build.js" <<'EOF'
const fs = require("fs");
const path = require("path");
const dist = path.join(__dirname, "dist");
fs.mkdirSync(dist, { recursive: true });
fs.writeFileSync(path.join(dist, "build-marker.txt"), "built\n");
EOF
cat > "$fake_invoker_repo/packages/app/package.json" <<'EOF'
{
  "name": "@invoker/app",
  "private": true,
  "scripts": {
    "build": "node build.js"
  }
}
EOF
cat > "$fake_invoker_repo/packages/app/build.js" <<'EOF'
const fs = require("fs");
const path = require("path");
const dist = path.join(__dirname, "dist");
fs.mkdirSync(dist, { recursive: true });
fs.writeFileSync(path.join(dist, "main.js"), "console.log('fake headless app')\n");
EOF
cat > "$fake_invoker_repo/scripts/electron.cjs" <<'EOF'
#!/usr/bin/env node
console.log(["FAKE_INSTALL_SKILLS", ...process.argv.slice(2)].join(" "));
EOF
chmod +x "$fake_invoker_repo/scripts/electron.cjs"
cat > "$fake_invoker_repo/packages/cli/dist/index.js" <<'EOF'
#!/usr/bin/env node
const fs = require("fs");
const path = require("path");

const args = process.argv.slice(2);
const valueAfter = (flag) => {
  const index = args.indexOf(flag);
  return index === -1 ? "" : args[index + 1] || "";
};
const dbDir = valueAfter("--db-dir");
const configPath = valueAfter("--config");
const logPath = process.env.FAKE_INVOKER_CLI_LOG || path.join(process.env.JOB_DIR || process.cwd(), "cli-invocations.jsonl");
const config = configPath && fs.existsSync(configPath) ? JSON.parse(fs.readFileSync(configPath, "utf8")) : {};
const dbExistsBefore = Boolean(dbDir && fs.existsSync(dbDir));
if (dbDir) {
  fs.mkdirSync(dbDir, { recursive: true });
  fs.writeFileSync(path.join(dbDir, "standalone-marker.txt"), "created\n");
}
fs.appendFileSync(logPath, JSON.stringify({ script: process.argv[1], args, dbDir, configPath, config, dbExistsBefore }) + "\n");

switch (process.env.FAKE_INVOKER_FAILURE_KIND || "") {
  case "claude_auth":
    console.error("Failed to authenticate. API Error: 401");
    process.exit(1);
    break;
  case "validation":
    console.error("Strict validation failed: task id is required");
    process.exit(1);
    break;
  case "timeout":
    console.error("Invoker CLI timed out");
    process.exit(124);
    break;
  default:
    process.stdout.write(JSON.stringify({ ok: true }) + "\n");
}
EOF
chmod +x "$fake_invoker_repo/packages/cli/dist/index.js"
cat > "$fake_invoker_repo/run.sh" <<'EOF'
#!/usr/bin/env bash
case "${FAKE_INVOKER_FAILURE_KIND:-}" in
  claude_auth)
    echo "Failed to authenticate. API Error: 401" >&2
    exit 1
    ;;
  validation)
    echo "Strict validation failed: task id is required" >&2
    exit 1
    ;;
  *)
    exit 0
    ;;
esac
EOF
chmod +x "$fake_invoker_repo/run.sh"
git -C "$fake_invoker_repo" init >/dev/null
git -C "$fake_invoker_repo" config user.email benchmark-test@example.com
git -C "$fake_invoker_repo" config user.name "Benchmark Test"
git -C "$fake_invoker_repo" add .
git -C "$fake_invoker_repo" commit -m "fake invoker" >/dev/null
fake_invoker_sha="$(git -C "$fake_invoker_repo" rev-parse HEAD)"

worker_root="$TMP_ROOT/worker-root"
mkdir -p "$worker_root/bin" "$worker_root/config" "$worker_root/corpus" "$worker_root/lib" "$worker_root/scripts"
cp -R "$BENCHMARK_SOURCE_ROOT/bin/." "$worker_root/bin/"
cp -R "$BENCHMARK_SOURCE_ROOT/lib/." "$worker_root/lib/"
cp "$USAGE_COSTING_SCRIPT" "$worker_root/scripts/usage_costing.py"
cp "$TOKEN_LEDGER_SCRIPT" "$worker_root/scripts/token_ledger.py"
printf '{"type":"event_msg","payload":{"type":"user_message","message":"do the thing"}}\n' > "$worker_root/corpus/session-01.jsonl"
cat > "$worker_root/config/benchmark.env" <<EOF
TZ=Asia/Hong_Kong
BENCHMARK_ROOT=$worker_root
INVOKER_REPO=$fake_invoker_repo
INVOKER_BRANCH=master
BENCHMARK_PLAN_CODEX_COMMAND='printf "%s\n" "name: fake plan" "repoUrl: https://example.test/repo.git" "mergeMode: manual" "tasks:" "  - id: t1" "    title: T1" > "\$GENERATED_PLAN"'
BENCHMARK_INVOKER_CLI_BUILD_COMMAND='node packages/cli/build.js'
BENCHMARK_INVOKER_APP_BUILD_COMMAND='node packages/app/build.js'
EOF

if BENCHMARK_ENV_FILE="$worker_root/config/benchmark.env" FAKE_INVOKER_FAILURE_KIND=claude_auth "$worker_root/bin/run-worker-job.sh" --batch-id worker-failures --run-id auth-failure --conversation-file "$worker_root/corpus/session-01.jsonl" --model codex --mode invoker_workflow --invoker-sha "$fake_invoker_sha" >"$worker_root/auth.out" 2>&1; then
  echo "Expected auth failure worker job to fail" >&2
  exit 1
fi
python3 - "$worker_root/runs/worker-failures/jobs/auth-failure/job.json" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1]))
assert payload["failure_stage"] == "invoker_cli_run"
assert payload["failure_reason"] == "claude_auth_failed"
assert "401" in payload["failure_message"]
assert "Failed to authenticate. API Error: 401" in payload["failure_raw_output"]["stderr.log"]
assert payload["invoker_sha"]
job_dir = sys.argv[1].rsplit("/", 1)[0]
assert not __import__("pathlib").Path(job_dir, "checkout").exists()
assert __import__("pathlib").Path(job_dir, "invoker-db").exists()
PY
grep -q "===== BENCHMARK JOB FAILURE =====" "$worker_root/auth.out"
grep -q "Failed to authenticate. API Error: 401" "$worker_root/auth.out"

if BENCHMARK_ENV_FILE="$worker_root/config/benchmark.env" FAKE_INVOKER_FAILURE_KIND=validation "$worker_root/bin/run-worker-job.sh" --batch-id worker-failures --run-id validation-failure --conversation-file "$worker_root/corpus/session-01.jsonl" --model codex --mode invoker_workflow --invoker-sha "$fake_invoker_sha" >"$worker_root/validation.out" 2>&1; then
  echo "Expected validation failure worker job to fail" >&2
  exit 1
fi
python3 - "$worker_root/runs/worker-failures/jobs/validation-failure/job.json" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1]))
assert payload["failure_stage"] == "invoker_cli_run"
assert payload["failure_reason"] == "plan_validation_failed"
assert "validation" in payload["failure_message"].lower()
job_dir = sys.argv[1].rsplit("/", 1)[0]
assert not __import__("pathlib").Path(job_dir, "checkout").exists()
assert __import__("pathlib").Path(job_dir, "invoker-db").exists()
PY

BENCHMARK_ENV_FILE="$worker_root/config/benchmark.env" "$worker_root/bin/run-worker-job.sh" --batch-id worker-failures --run-id successful-invoker --conversation-file "$worker_root/corpus/session-01.jsonl" --model codex --mode invoker_workflow --invoker-sha "$fake_invoker_sha" >"$worker_root/success.out" 2>&1
grep -q "FAKE_INSTALL_SKILLS packages/app/dist/main.js --headless install-skills reinstall" "$worker_root/success.out"
python3 - "$worker_root/runs/worker-failures/jobs/successful-invoker/job.json" "$worker_root/runs/worker-failures/jobs/successful-invoker/cli-invocations.jsonl" <<'PY'
import json
import sys
from pathlib import Path
payload = json.load(open(sys.argv[1]))
invocations = [json.loads(line) for line in open(sys.argv[2]) if line.strip()]
assert len(invocations) == 1, invocations
invocation = invocations[0]
assert invocation["script"].endswith("packages/cli/dist/index.js"), invocation
assert invocation["args"][0:2] == ["run", str(Path(sys.argv[1]).parent / "generated-plan.yaml")], invocation
assert "--standalone" in invocation["args"], invocation
assert invocation["dbDir"] == str(Path(sys.argv[1]).parent / "invoker-db"), invocation
assert invocation["configPath"] == str(Path(sys.argv[1]).parent / "invoker-config.json"), invocation
assert "--json" in invocation["args"], invocation
assert invocation["config"]["autoFixRetries"] == 0, invocation
assert invocation["dbExistsBefore"] is True, invocation
assert payload["status"] == "succeeded"
assert payload["result"] == "pass"
assert payload["failure_stage"] == ""
assert payload["failure_reason"] == ""
assert payload["plan_inspection"]["mergeMode"] == "manual"
assert payload["plan_inspection"]["mergeMode_manual"] is True
assert payload["plan_inspection"]["task_count"] == 1
job_dir = Path(sys.argv[1]).parent
assert not (job_dir / "checkout").exists()
assert not (job_dir / "invoker-db").exists()
PY

prompt_task_env="$worker_root/config/prompt-task-plan.env"
cat > "$prompt_task_env" <<EOF
TZ=Asia/Hong_Kong
BENCHMARK_ROOT=$worker_root
INVOKER_REPO=$fake_invoker_repo
INVOKER_BRANCH=master
BENCHMARK_PLAN_CODEX_COMMAND='printf "%s\n" "name: fake plan" "repoUrl: https://example.test/repo.git" "mergeMode: manual" "tasks:" "  - id: t1" "    title: T1" "    prompt: |" "      Do the benchmark task." "  - id: t2" "    title: T2" "    executionAgent: claude" "    command: true" > "\$GENERATED_PLAN"'
BENCHMARK_INVOKER_CLI_BUILD_COMMAND='node packages/cli/build.js'
EOF
BENCHMARK_ENV_FILE="$prompt_task_env" "$worker_root/bin/run-worker-job.sh" --batch-id worker-failures --run-id prompt-task-plan --conversation-file "$worker_root/corpus/session-01.jsonl" --model codex --mode invoker_workflow --invoker-sha "$fake_invoker_sha" >"$worker_root/prompt-task-plan.out" 2>&1
python3 - "$worker_root/runs/worker-failures/jobs/prompt-task-plan/job.json" "$worker_root/runs/worker-failures/jobs/prompt-task-plan/generated-plan.yaml" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1]))
generated = open(sys.argv[2]).read()
assert payload["status"] == "succeeded"
assert payload["result"] == "pass"
assert payload["failure_stage"] == ""
assert payload["plan_inspection"]["mergeMode"] == "manual"
assert payload["plan_inspection"]["task_count"] == 2
assert payload["plan_inspection"]["prompt_count"] == 1
assert payload["plan_inspection"]["command_count"] == 1
assert payload["plan_inspection"]["executionAgent"] == "codex"
assert payload["plan_inspection"]["executionAgent_inserted_count"] == 1
assert payload["plan_inspection"]["executionAgent_rewritten_count"] == 1
assert "executionAgent: claude" not in generated
assert generated.count("executionAgent: codex") == 2
PY

github_merge_env="$worker_root/config/github-merge.env"
cat > "$github_merge_env" <<EOF
TZ=Asia/Hong_Kong
BENCHMARK_ROOT=$worker_root
INVOKER_REPO=$fake_invoker_repo
INVOKER_BRANCH=master
BENCHMARK_PLAN_CODEX_COMMAND='printf "%s\n" "name: fake plan" "repoUrl: https://example.test/repo.git" "mergeMode: github" "tasks:" "  - id: t1" "    title: T1" > "\$GENERATED_PLAN"'
BENCHMARK_INVOKER_CLI_BUILD_COMMAND='node packages/cli/build.js'
EOF
if BENCHMARK_ENV_FILE="$github_merge_env" "$worker_root/bin/run-worker-job.sh" --batch-id worker-failures --run-id github-merge-plan --conversation-file "$worker_root/corpus/session-01.jsonl" --model codex --mode invoker_workflow --invoker-sha "$fake_invoker_sha" >"$worker_root/github-merge.out" 2>&1; then
  echo "Expected github merge plan generation to fail" >&2
  exit 1
fi
python3 - "$worker_root/runs/worker-failures/jobs/github-merge-plan/job.json" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1]))
assert payload["failure_stage"] == "plan_generation"
assert payload["failure_reason"] == "plan_generation_failed"
assert "mergeMode: github" in payload["failure_message"]
assert payload["plan_inspection"]["mergeMode"] == "github"
assert payload["plan_inspection"]["mergeMode_manual"] is False
PY

temp_plan_env="$worker_root/config/temp-plan-artifact.env"
cat > "$temp_plan_env" <<'EOF'
TZ=Asia/Hong_Kong
BENCHMARK_ROOT=__WORKER_ROOT__
INVOKER_REPO=__FAKE_INVOKER_REPO__
INVOKER_BRANCH=master
BENCHMARK_PLAN_CODEX_COMMAND='tmp_plan="$JOB_DIR/generated-plan-manual.yaml"; printf "%s\n" "name: fake plan" "repoUrl: https://example.test/repo.git" "mergeMode: manual" "tasks:" "  - id: t1" "    title: T1" > "$tmp_plan"; printf "%s\n" "TMPDIR=/dev/shm bash skills/plan-to-invoker/scripts/skill-doctor.sh --skip-assumptions $tmp_plan" > "$GENERATED_PLAN"'
BENCHMARK_INVOKER_CLI_BUILD_COMMAND='node packages/cli/build.js'
EOF
sed -i.bak \
  -e "s|__WORKER_ROOT__|$worker_root|g" \
  -e "s|__FAKE_INVOKER_REPO__|$fake_invoker_repo|g" \
  "$temp_plan_env"
rm -f "$temp_plan_env.bak"
if BENCHMARK_ENV_FILE="$temp_plan_env" "$worker_root/bin/run-worker-job.sh" --batch-id worker-failures --run-id temp-plan-artifact --conversation-file "$worker_root/corpus/session-01.jsonl" --model codex --mode invoker_auto_fix --invoker-sha "$fake_invoker_sha" >"$worker_root/temp-plan-artifact.out" 2>&1; then
  echo "Expected temp plan artifact generation to fail benchmark inspection" >&2
  exit 1
fi
python3 - "$worker_root/runs/worker-failures/jobs/temp-plan-artifact/job.json" "$worker_root/runs/worker-failures/jobs/temp-plan-artifact/generated-plan.yaml" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1]))
generated = open(sys.argv[2]).read()
assert payload["failure_stage"] == "plan_generation"
assert payload["failure_reason"] == "plan_generation_failed"
assert "missing top-level name" in payload["failure_message"]
assert "skill-doctor.sh --skip-assumptions" in generated
assert payload["plan_inspection"]["task_count"] == 0
PY

BENCHMARK_ENV_FILE="$worker_root/config/benchmark.env" "$worker_root/bin/run-worker-job.sh" --batch-id worker-failures --run-id successful-autofix --conversation-file "$worker_root/corpus/session-01.jsonl" --model codex --mode invoker_auto_fix --invoker-sha "$fake_invoker_sha" >"$worker_root/autofix.out" 2>&1
python3 - "$worker_root/runs/worker-failures/jobs/successful-autofix/job.json" "$worker_root/runs/worker-failures/jobs/successful-autofix/cli-invocations.jsonl" <<'PY'
import json
import sys
from pathlib import Path
payload = json.load(open(sys.argv[1]))
invocation = [json.loads(line) for line in open(sys.argv[2]) if line.strip()][0]
assert payload["status"] == "succeeded"
assert invocation["config"]["autoFixRetries"] == 1, invocation
job_dir = Path(sys.argv[1]).parent
assert not (job_dir / "invoker-db").exists()
PY

if BENCHMARK_ENV_FILE="$worker_root/config/benchmark.env" FAKE_INVOKER_FAILURE_KIND=timeout "$worker_root/bin/run-worker-job.sh" --batch-id worker-failures --run-id timeout-failure --conversation-file "$worker_root/corpus/session-01.jsonl" --model codex --mode invoker_workflow --invoker-sha "$fake_invoker_sha" >"$worker_root/timeout.out" 2>&1; then
  echo "Expected timeout-like worker job to fail" >&2
  exit 1
fi
python3 - "$worker_root/runs/worker-failures/jobs/timeout-failure/job.json" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1]))
assert payload["failure_stage"] == "invoker_cli_run"
assert payload["failure_reason"] == "timeout"
assert "timed out" in payload["failure_message"].lower()
PY

cleanup_run_id="cleanup-scope"
cleanup_job_dir="$worker_root/runs/worker-failures/jobs/$cleanup_run_id"
cleanup_processes="$worker_root/cleanup-processes.txt"
cleanup_kill_log="$worker_root/cleanup-kill.log"
cat > "$cleanup_processes" <<EOF
10001 1 /Applications/Electron.app/Contents/MacOS/Electron
10002 1 /usr/bin/electron --user-data-dir /tmp/other-benchmark/jobs/other/invoker-db
10003 1 $cleanup_job_dir/checkout/node_modules/.bin/electron --headless
10004 1 /usr/bin/Electron --user-data-dir $cleanup_job_dir/invoker-db
10005 1 $cleanup_job_dir/checkout/scripts/kill-all-electron.sh
10006 1 /usr/bin/Invoker --config /tmp/other-benchmark/jobs/other/invoker-config.json
10007 1 /usr/bin/node /repo/scripts/electron.cjs --socket $cleanup_job_dir/invoker-db/ipc-transport.sock
EOF
BENCHMARK_ENV_FILE="$worker_root/config/benchmark.env" \
  BENCHMARK_PROCESS_LIST_FILE="$cleanup_processes" \
  BENCHMARK_KILL_LOG="$cleanup_kill_log" \
  BENCHMARK_ELECTRON_CLEANUP_TERM_WAIT_SECONDS=0 \
  "$worker_root/bin/run-worker-job.sh" --batch-id worker-failures --run-id "$cleanup_run_id" --conversation-file "$worker_root/corpus/session-01.jsonl" --model codex --mode invoker_workflow --invoker-sha "$fake_invoker_sha" >"$worker_root/cleanup-scope.out" 2>&1
python3 - "$cleanup_kill_log" <<'PY'
import sys
from pathlib import Path
lines = [line.split() for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
targeted = {int(pid) for _, pid in lines}
assert targeted == {10003, 10004, 10007}, lines
assert all(signal in {"TERM", "KILL"} for signal, _ in lines), lines
assert not ({10001, 10002, 10005, 10006} & targeted), lines
PY

cleanup_failure_env="$worker_root/config/cleanup-failure.env"
cat > "$cleanup_failure_env" <<EOF
TZ=Asia/Hong_Kong
BENCHMARK_ROOT=$worker_root
INVOKER_REPO=$fake_invoker_repo
INVOKER_BRANCH=master
BENCHMARK_PLAN_CODEX_COMMAND='mkdir -p "\$HOME/.codex/sessions"; printf "%s\n" '"'"'{"usage":{"input_tokens":111,"output_tokens":22,"reasoning_tokens":3,"total_tokens":136}}'"'"' > "\$HOME/.codex/sessions/cleanup-failure.jsonl"; printf "%s\n" "name: fake plan" "repoUrl: https://example.test/repo.git" "mergeMode: manual" "tasks:" "  - id: t1" "    title: T1" > "\$GENERATED_PLAN"'
BENCHMARK_INVOKER_CLI_BUILD_COMMAND='node packages/cli/build.js'
EOF
cleanup_failure_run_id="cleanup-failure-with-tokens"
cleanup_failure_job_dir="$worker_root/runs/worker-failures/jobs/$cleanup_failure_run_id"
cleanup_failure_processes="$worker_root/cleanup-failure-processes.txt"
cat > "$cleanup_failure_processes" <<EOF
19999 1 /usr/bin/electron --user-data-dir $cleanup_failure_job_dir/invoker-db
EOF
worker_home="$worker_root/home"
rm -rf "$worker_home"
mkdir -p "$worker_home"
if HOME="$worker_home" \
  BENCHMARK_ENV_FILE="$cleanup_failure_env" \
  BENCHMARK_PROCESS_LIST_FILE="$cleanup_failure_processes" \
  BENCHMARK_ELECTRON_CLEANUP_TERM_WAIT_SECONDS=0 \
  "$worker_root/bin/run-worker-job.sh" --batch-id worker-failures --run-id "$cleanup_failure_run_id" --conversation-file "$worker_root/corpus/session-01.jsonl" --model codex --mode invoker_workflow --invoker-sha "$fake_invoker_sha" >"$worker_root/cleanup-failure.out" 2>&1; then
  echo "Expected scoped cleanup failure worker job to fail" >&2
  exit 1
fi
python3 - "$cleanup_failure_job_dir/job.json" "$cleanup_failure_job_dir/token-usage.json" <<'PY'
import json
import sys
job = json.load(open(sys.argv[1]))
usage = json.load(open(sys.argv[2]))
assert job["failure_stage"] == "electron_cleanup"
assert job["failure_reason"] == "electron_cleanup_failed"
assert usage["input_tokens"] == 111
assert usage["output_tokens"] == 22
assert usage["reasoning_tokens"] == 3
assert usage["total_tokens"] == 136
assert job["token_usage"]["total_tokens"] == 136
PY
cat > "$worker_root/runs/worker-failures/summary.json" <<EOF
{
  "batch_id": "worker-failures",
  "invoker_sha": "$fake_invoker_sha",
  "job_count": 8,
  "setup_status": "succeeded",
  "status_counts": {"failed": 5, "succeeded": 3}
}
EOF
BENCHMARK_ROOT=/home/invoker/invoker-benchmarks "$worker_root/bin/emit-mixpanel-events.sh" --batch-dir "$worker_root/runs/worker-failures" >/dev/null
python3 - "$worker_root/runs/worker-failures/mixpanel-export.jsonl" "$cleanup_failure_run_id" <<'PY'
import json
import sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
token = next(item["properties"] for item in events if item["event"] == "benchmark_token_usage" and item["properties"].get("run_id") == sys.argv[2])
assert token["failure_stage"] == "electron_cleanup"
assert token["failure_reason"] == "electron_cleanup_failed"
assert token["total_tokens"] == 136
PY

EMPTY_ROOT="$(mktemp -d /tmp/invoker-benchmark-empty.XXXXXX)"
mkdir -p "$EMPTY_ROOT/config" "$EMPTY_ROOT/corpus/submit-to-invoker-sessions-2026-05-26" "$EMPTY_ROOT/bin" "$EMPTY_ROOT/lib"
cp -R "$BENCHMARK_SOURCE_ROOT/bin/." "$EMPTY_ROOT/bin/"
cp -R "$BENCHMARK_SOURCE_ROOT/lib/." "$EMPTY_ROOT/lib/"
cp "$BENCHMARK_SOURCE_ROOT/config/corpus-manifest.json" "$EMPTY_ROOT/config/corpus-manifest.json"
cp "$TMP_ROOT/config/workers.json" "$EMPTY_ROOT/config/workers.json"
cat > "$EMPTY_ROOT/config/benchmark.env" <<EOF
TZ=Asia/Hong_Kong
BENCHMARK_ROOT=$EMPTY_ROOT
INVOKER_REPO=https://github.com/Neko-Catpital-Labs/Invoker.git
INVOKER_BRANCH=master
INVOKER_SHA=test-sha
CORPUS_DIR=$EMPTY_ROOT/corpus/submit-to-invoker-sessions-2026-05-26
MODELS=codex,claude
MODES=baseline_direct,invoker_workflow,invoker_auto_fix
WORKER_CONCURRENCY_PER_HOST=1
EOF
if "$EMPTY_ROOT/bin/run-nightly-benchmark.sh" --dry-run --env-file "$EMPTY_ROOT/config/benchmark.env" >"$EMPTY_ROOT/empty.out" 2>&1; then
  echo "Expected empty corpus run to fail" >&2
  exit 1
fi
empty_summary="$(find "$EMPTY_ROOT/runs" -name summary.json -print -quit)"
empty_log="$(find "$EMPTY_ROOT/runs" -name run.log -print -quit)"
[[ -f "$empty_summary" ]] || { echo "Missing empty corpus failure summary" >&2; exit 1; }
[[ -f "$empty_log" ]] || { echo "Missing empty corpus failure log" >&2; exit 1; }
python3 - "$empty_summary" <<'PY'
import json
import sys
payload = json.load(open(sys.argv[1]))
assert payload["setup_status"] == "failed"
assert payload["status_counts"] == {"setup_failed": 1}
PY

echo "OK: invoker benchmark dry-run matrix generation"
