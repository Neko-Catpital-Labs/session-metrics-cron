#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP_ROOT="$(mktemp -d /tmp/invoker-benchmark-test.XXXXXX)"
EMPTY_ROOT=""
trap 'rm -rf "$TMP_ROOT" ${EMPTY_ROOT:+"$EMPTY_ROOT"}' EXIT

mkdir -p "$TMP_ROOT/config" "$TMP_ROOT/corpus/submit-to-invoker-sessions-2026-05-26" "$TMP_ROOT/bin" "$TMP_ROOT/lib"
cp -R "$REPO_ROOT/invoker-benchmarks/bin/." "$TMP_ROOT/bin/"
cp -R "$REPO_ROOT/invoker-benchmarks/lib/." "$TMP_ROOT/lib/"
cp "$REPO_ROOT/invoker-benchmarks/config/corpus-manifest.json" "$TMP_ROOT/config/corpus-manifest.json"

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
BENCHMARK_ROOT=/home/invoker/invoker-benchmarks "$TMP_ROOT/bin/emit-mixpanel-events.sh" --batch-dir "$fake_batch" >/dev/null
python3 - "$fake_batch/mixpanel-export.jsonl" <<'PY'
import json
import sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
run = next(item["properties"] for item in events if item["event"] == "benchmark_run")
assert run["test_id"] == "019e1b94-1c63-7e02-a60f-febd3e3f2ff4"
assert run["corpus_case_id"] == "session-01"
assert run["execution_surface"] == "baseline"
assert run["autofix_enabled"] is False
assert run["estimated_cost_usd"] == 0.001555
assert run["derived_total_cost_usd"] == 0.001555
assert run["job_artifact_path"] == "/home/invoker/invoker-benchmarks/runs/fake-batch/jobs/" + run["run_id"]
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

EMPTY_ROOT="$(mktemp -d /tmp/invoker-benchmark-empty.XXXXXX)"
mkdir -p "$EMPTY_ROOT/config" "$EMPTY_ROOT/corpus/submit-to-invoker-sessions-2026-05-26" "$EMPTY_ROOT/bin" "$EMPTY_ROOT/lib"
cp -R "$REPO_ROOT/invoker-benchmarks/bin/." "$EMPTY_ROOT/bin/"
cp -R "$REPO_ROOT/invoker-benchmarks/lib/." "$EMPTY_ROOT/lib/"
cp "$REPO_ROOT/invoker-benchmarks/config/corpus-manifest.json" "$EMPTY_ROOT/config/corpus-manifest.json"
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
