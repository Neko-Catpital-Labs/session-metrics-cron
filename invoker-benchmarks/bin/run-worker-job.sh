#!/usr/bin/env bash
set -euo pipefail

BATCH_ID=""
RUN_ID=""
CONVERSATION_FILE=""
MODEL=""
MODE=""
INVOKER_SHA=""

usage() {
  cat <<'EOF'
Usage: run-worker-job.sh --batch-id ID --run-id ID --conversation-file PATH --model codex|claude --mode MODE --invoker-sha SHA
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --batch-id) BATCH_ID="${2:-}"; shift 2 ;;
    --run-id) RUN_ID="${2:-}"; shift 2 ;;
    --conversation-file) CONVERSATION_FILE="${2:-}"; shift 2 ;;
    --model) MODEL="${2:-}"; shift 2 ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --invoker-sha) INVOKER_SHA="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "$BATCH_ID" ]] || die "Missing --batch-id"
[[ -n "$RUN_ID" ]] || die "Missing --run-id"
[[ -n "$CONVERSATION_FILE" ]] || die "Missing --conversation-file"
[[ -n "$MODEL" ]] || die "Missing --model"
[[ -n "$MODE" ]] || die "Missing --mode"
[[ -n "$INVOKER_SHA" ]] || die "Missing --invoker-sha"
CLI_INVOKER_SHA="$INVOKER_SHA"

BENCHMARK_ROOT="${BENCHMARK_ROOT:-/home/invoker/invoker-benchmarks}"
ENV_FILE="${BENCHMARK_ENV_FILE:-$BENCHMARK_ROOT/config/benchmark.env}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE"
  set +a
fi
INVOKER_SHA="$CLI_INVOKER_SHA"
export INVOKER_SHA

export TZ="${TZ:-Asia/Hong_Kong}"
JOB_DIR="$BENCHMARK_ROOT/runs/$BATCH_ID/jobs/$RUN_ID"
CHECKOUT_DIR="$JOB_DIR/checkout"
RAW_SESSIONS_DIR="$JOB_DIR/raw-sessions"
GENERATED_PLAN="$JOB_DIR/generated-plan.yaml"
INVOKER_DB_DIR_JOB="$JOB_DIR/invoker-db"
INVOKER_CONFIG_JOB="$JOB_DIR/invoker-config.json"
PROMPT_FILE="$JOB_DIR/prompt.txt"
STDOUT_LOG="$JOB_DIR/stdout.log"
STDERR_LOG="$JOB_DIR/stderr.log"
STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
STEP_LOG="$JOB_DIR/steps.log"
CURRENT_STAGE=""

mkdir -p "$JOB_DIR" "$RAW_SESSIONS_DIR"
exec > >(tee -a "$STDOUT_LOG") 2> >(tee -a "$STDERR_LOG" >&2)

log_step() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" | tee -a "$STEP_LOG"
}

set_stage() {
  CURRENT_STAGE="$1"
  printf '%s\n' "$CURRENT_STAGE" > "$JOB_DIR/current-stage"
}

write_job_json() {
  local status="$1"
  local exit_code="${2:-0}"
  local failure_stage="${3:-}"
  python3 - "$JOB_DIR/job.json" "$BATCH_ID" "$RUN_ID" "$CONVERSATION_FILE" "$MODEL" "$MODE" "$INVOKER_SHA" "$STARTED_AT" "$status" "$exit_code" "$CHECKOUT_DIR" "$failure_stage" <<'PY'
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

path, batch_id, run_id, conv, model, mode, sha, started, status, exit_code, checkout_arg, failure_stage_arg = sys.argv[1:]
job_dir = Path(path).parent
checkout = Path(checkout_arg)

def run_git(args):
    try:
        return subprocess.check_output(["git", "-C", str(checkout), *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""

commits = []
log = run_git(["log", "--oneline", f"{sha}..HEAD"])
if log:
    commits = log.splitlines()
changed = run_git(["status", "--short"])
steps_path = job_dir / "steps.log"
steps = steps_path.read_text(errors="ignore").splitlines() if steps_path.exists() else []
token_path = job_dir / "token-usage.json"
token_usage = {}
if token_path.exists():
    try:
        token_usage = json.loads(token_path.read_text())
    except Exception:
        token_usage = {}
source_info = {}
conv_path = Path(conv)
for manifest_path in (
    conv_path.parent.with_suffix(".source-manifest.json"),
    conv_path.parent.parent / f"{conv_path.parent.name}.source-manifest.json",
):
    if not manifest_path.exists():
        continue
    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception:
        continue
    for item in manifest.get("sessions", []):
        if isinstance(item, dict) and item.get("file") == conv_path.name:
            source_info = item
            break
    if source_info:
        break

def read_artifact(name, limit=50000):
    artifact_path = job_dir / name
    if not artifact_path.exists():
        return ""
    try:
        text = artifact_path.read_text(errors="ignore")
    except Exception:
        return ""
    return text[-limit:]

def concise_message(*texts):
    for text in texts:
        for line in str(text or "").splitlines():
            line = " ".join(line.strip().split())
            if not line:
                continue
            if len(line) > 240:
                return line[:237] + "..."
            return line
    return ""

def matching_message(text, *needles):
    lowered_needles = [needle.lower() for needle in needles]
    for line in str(text or "").splitlines():
        compact = " ".join(line.strip().split())
        lowered = compact.lower()
        if compact and any(needle in lowered for needle in lowered_needles):
            if len(compact) > 240:
                return compact[:237] + "..."
            return compact
    return ""

def derive_failure(status_value, exit_code_value, stage_value):
    if int(exit_code_value) == 0 and status_value not in {"failed", "timeout", "invalid_job_json"}:
        return "", "", ""
    stderr = read_artifact("stderr.log")
    stdout = read_artifact("stdout.log")
    plan = read_artifact("generated-plan.yaml")
    combined = "\n".join([stderr, stdout, plan])
    lowered = combined.lower()
    stage = stage_value or "unknown"

    if status_value == "timeout" or "timed out" in lowered or "timeout" in lowered:
        return stage, "timeout", matching_message(combined, "timeout", "timed out") or concise_message(stderr, stdout, "timeout")
    if "failed to authenticate. api error: 401" in lowered or "api error: 401" in lowered or "status code: 401" in lowered:
        return stage, "claude_auth_failed", matching_message(combined, "api error: 401", "status code: 401", "failed to authenticate") or concise_message(stderr, stdout, "Claude authentication failed")
    validation_signatures = (
        "strict validation",
        "plan validation failed",
        "failed to validate",
        "validation failed",
        "invalid plan",
        "yaml validation",
    )
    if any(signature in lowered for signature in validation_signatures):
        return stage, "plan_validation_failed", matching_message(combined, *validation_signatures) or concise_message(stderr, stdout, plan)
    if stage == "electron_cleanup":
        return stage, "electron_cleanup_failed", concise_message(stderr, stdout, "Electron cleanup failed")
    if stage == "invoker_headless_run":
        return stage, "headless_run_failed", concise_message(stderr, stdout, "Invoker headless run failed")
    if stage == "checkout":
        return stage, "checkout_failed", concise_message(stderr, stdout, "Checkout failed")
    if stage == "plan_generation":
        return stage, "plan_generation_failed", concise_message(stderr, stdout, plan)
    if stage == "token_usage":
        return stage, "token_usage_failed", concise_message(stderr, stdout, "Token usage extraction failed")
    return stage, "unknown", concise_message(stderr, stdout, plan, "Unknown benchmark failure")

failure_stage, failure_reason, failure_message = derive_failure(status, exit_code, failure_stage_arg)

def derive_source_session_id():
    source_file = str(source_info.get("source_file") or "")
    match = re.search(r"(019[a-z0-9-]{32,})", source_file)
    if match:
        return match.group(1)
    try:
        first = json.loads(conv_path.read_text(errors="ignore").splitlines()[0])
    except Exception:
        return ""
    payload = first.get("payload") if isinstance(first, dict) else None
    if isinstance(payload, dict) and isinstance(payload.get("id"), str):
        return payload["id"]
    return ""
source_session_id = derive_source_session_id()
conversation_id = source_session_id or conv_path.stem
payload = {
    "batch_id": batch_id,
    "run_id": run_id,
    "test_id": conversation_id,
    "conversation_file": conv,
    "conversation_id": conversation_id,
    "corpus_case_id": Path(conv).stem,
    "source_session_id": source_session_id,
    "source_session_file": source_info.get("source_file", ""),
    "source_session_date": source_info.get("session_date", ""),
    "source_session_model": source_info.get("model", ""),
    "model": model,
    "mode": mode,
    "scenario": mode,
    "execution_surface": "baseline" if mode == "baseline_direct" else "invoker",
    "autofix_enabled": mode == "invoker_auto_fix",
    "invoker_sha": sha,
    "started_at": started,
    "finished_at": datetime.now(timezone.utc).isoformat(),
    "status": status,
    "exit_code": int(exit_code),
    "result": "pass" if int(exit_code) == 0 and status not in {"failed", "timeout", "invalid_job_json"} else "fail",
    "failure_stage": failure_stage,
    "failure_reason": failure_reason,
    "failure_message": failure_message,
    "commits": commits,
    "changed_files": [line[3:] if len(line) > 3 else line for line in changed.splitlines()],
    "step_log": steps,
    "test_configuration": {
        "model": model,
        "mode": mode,
        "scenario": mode,
        "conversation_file": conv,
        "conversation_id": conversation_id,
        "test_id": conversation_id,
        "corpus_case_id": Path(conv).stem,
        "source_session_id": source_session_id,
        "source_session_file": source_info.get("source_file", ""),
        "source_session_date": source_info.get("session_date", ""),
        "source_session_model": source_info.get("model", ""),
        "invoker_sha": sha,
        "invoker_repo": os.environ.get("INVOKER_REPO", ""),
        "invoker_branch": os.environ.get("INVOKER_BRANCH", ""),
    },
    "job_artifact_path": str(job_dir),
    "token_usage": token_usage,
    "artifacts": {
        "stdout": "stdout.log",
        "stderr": "stderr.log",
        "steps": "steps.log",
        "prompt": "prompt.txt",
        "cost_calculation": "cost-calculation.json",
        "generated_plan": "generated-plan.yaml",
        "invoker_events": "invoker-events.jsonl",
        "token_usage": "token-usage.json",
        "raw_sessions": "raw-sessions",
        "checkout": "checkout",
    },
}
Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True))
PY
}

on_exit() {
  local code=$?
  if [[ "$code" -ne 0 && ! -f "$JOB_DIR/job.json" ]]; then
    local failure_stage="$CURRENT_STAGE"
    if [[ -f "$JOB_DIR/current-stage" ]]; then
      failure_stage="$(cat "$JOB_DIR/current-stage")"
    fi
    write_job_json failed "$code" "$failure_stage" || true
  fi
  if [[ "$code" -ne 0 ]]; then
    cleanup_job_runtime || true
  fi
}
trap on_exit EXIT

snapshot_session_dirs() {
  find "$HOME/.codex/sessions" "$HOME/.claude" -type f 2>/dev/null | sort > "$JOB_DIR/session-files-before.txt" || true
}

collect_new_sessions() {
  find "$HOME/.codex/sessions" "$HOME/.claude" -type f 2>/dev/null | sort > "$JOB_DIR/session-files-after.txt" || true
  comm -13 "$JOB_DIR/session-files-before.txt" "$JOB_DIR/session-files-after.txt" > "$JOB_DIR/session-files-new.txt" || true
  while IFS= read -r file; do
    [[ -f "$file" ]] || continue
    rel="${file#$HOME/}"
    mkdir -p "$RAW_SESSIONS_DIR/$(dirname "$rel")"
    cp "$file" "$RAW_SESSIONS_DIR/$rel" || true
  done < "$JOB_DIR/session-files-new.txt"
}

clear_non_credential_state() {
  rm -rf \
    "$HOME/.cache/codex" \
    "$HOME/.cache/claude" \
    "$HOME/.codex/benchmark-scratch" \
    "$HOME/.claude/benchmark-scratch" \
    "$HOME/.invoker/benchmark-scratch" 2>/dev/null || true
  for scratch_path in /tmp/invoker-benchmark-*; do
    [[ -e "$scratch_path" ]] || continue
    if [[ "$BENCHMARK_ROOT" == "$scratch_path" || "$BENCHMARK_ROOT" == "$scratch_path/"* ]]; then
      continue
    fi
    rm -rf "$scratch_path" 2>/dev/null || true
  done
}

cleanup_job_runtime() {
  rm -rf "$CHECKOUT_DIR" "$INVOKER_DB_DIR_JOB" 2>/dev/null || true
}

install_checkout() {
  rm -rf "$CHECKOUT_DIR"
  git clone --no-checkout "${INVOKER_REPO:-https://github.com/Neko-Catpital-Labs/Invoker.git}" "$CHECKOUT_DIR"
  git -C "$CHECKOUT_DIR" checkout "$INVOKER_SHA"
  (
    cd "$CHECKOUT_DIR"
    if [[ -f pnpm-lock.yaml ]]; then
      corepack enable >/dev/null 2>&1 || true
      pnpm install --frozen-lockfile
    elif [[ -f package-lock.json ]]; then
      npm ci
    elif [[ -f yarn.lock ]]; then
      yarn install --frozen-lockfile
    fi
    if [[ -n "${BENCHMARK_CHECKOUT_SETUP_COMMAND:-}" ]]; then
      export CHECKOUT_DIR CONVERSATION_FILE PROMPT_FILE MODEL MODE JOB_DIR GENERATED_PLAN INVOKER_SHA
      eval "$BENCHMARK_CHECKOUT_SETUP_COMMAND"
    fi
  )
}

run_template() {
  local template="$1"
  if [[ -z "$template" ]]; then
    return 1
  fi
  (
    cd "$CHECKOUT_DIR"
    export CHECKOUT_DIR CONVERSATION_FILE PROMPT_FILE MODEL MODE JOB_DIR GENERATED_PLAN INVOKER_SHA
    eval "$template"
  )
}

prepare_prompt_file() {
  python3 - "$CONVERSATION_FILE" "$PROMPT_FILE" "${BENCHMARK_MAX_PROMPT_CHARS:-120000}" <<'PY'
import json
import sys
from pathlib import Path

src = Path(sys.argv[1])
out = Path(sys.argv[2])
max_chars = int(sys.argv[3])

def text_from_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("input_text") or item.get("output_text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return ""

def is_substantive(text):
    stripped = text.strip()
    if not stripped:
        return False
    ignored_prefixes = (
        "<environment_context>",
        "<turn_aborted>",
        "<permissions instructions>",
    )
    return not any(stripped.startswith(prefix) for prefix in ignored_prefixes)

prompts = []
if src.suffix.lower() == ".jsonl":
    for line in src.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if not isinstance(payload, dict):
            continue
        text = ""
        if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = payload.get("message") or ""
        elif obj.get("type") == "response_item" and payload.get("role") == "user":
            text = text_from_content(payload.get("content"))
        if is_substantive(text):
            prompts.append(text.strip())

if prompts:
    prompt = prompts[-1]
else:
    prompt = src.read_text(errors="ignore")

if len(prompt) > max_chars:
    prompt = prompt[-max_chars:]

out.write_text(prompt.rstrip() + "\n")
PY
}

default_baseline() {
  (
    cd "$CHECKOUT_DIR"
    case "$MODEL" in
      codex)
        command -v codex >/dev/null || die "codex CLI not found and BENCHMARK_BASELINE_CODEX_COMMAND is unset"
        codex exec --skip-git-repo-check - < "$PROMPT_FILE"
        ;;
      claude)
        command -v claude >/dev/null || die "claude CLI not found and BENCHMARK_BASELINE_CLAUDE_COMMAND is unset"
        claude -p "$(cat "$PROMPT_FILE")"
        ;;
      *) die "Unsupported model: $MODEL" ;;
    esac
  )
}

default_plan() {
  case "$MODEL" in
    codex)
      if ! run_template "${BENCHMARK_PLAN_CODEX_COMMAND:-}"; then
        command -v codex >/dev/null || die "codex CLI not found and BENCHMARK_PLAN_CODEX_COMMAND is unset"
        {
          printf '/plan-to-invoker\n'
          cat "$PROMPT_FILE"
        } | (cd "$CHECKOUT_DIR" && codex exec --skip-git-repo-check -) > "$GENERATED_PLAN"
      fi
      ;;
    claude)
      if ! run_template "${BENCHMARK_PLAN_CLAUDE_COMMAND:-}"; then
        command -v claude >/dev/null || die "claude CLI not found and BENCHMARK_PLAN_CLAUDE_COMMAND is unset"
        (cd "$CHECKOUT_DIR" && claude -p "/plan-to-invoker\n$(cat "$PROMPT_FILE")") > "$GENERATED_PLAN"
      fi
      ;;
    *) die "Unsupported model: $MODEL" ;;
  esac
}

default_invoker_submit() {
  (
    cd "$CHECKOUT_DIR"
    export INVOKER_AUTOFIX="$1"
    if run_template "${BENCHMARK_INVOKER_SUBMIT_COMMAND:-}"; then
      return 0
    fi
    mkdir -p "$INVOKER_DB_DIR_JOB"
    python3 - "$INVOKER_CONFIG_JOB" "${INVOKER_REPO_CONFIG_PATH:-$HOME/.invoker/config.json}" "$INVOKER_AUTOFIX" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
source = Path(sys.argv[2])
autofix = bool(sys.argv[3])
config = {}
if source.exists():
    try:
        loaded = json.loads(source.read_text())
        if isinstance(loaded, dict):
            config = loaded
    except Exception:
        config = {}
config["autoFixRetries"] = 1 if autofix else 0
out.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
PY
    export INVOKER_DB_DIR="$INVOKER_DB_DIR_JOB"
    export INVOKER_REPO_CONFIG_PATH="$INVOKER_CONFIG_JOB"
    set_stage "electron_cleanup"
    ./scripts/kill-all-electron.sh
    set_stage "invoker_headless_run"
    ./run.sh --headless run "$GENERATED_PLAN"
  )
}

extract_token_usage() {
  PYTHONPATH="$BENCHMARK_ROOT/scripts:$BENCHMARK_ROOT/../scripts:$BENCHMARK_ROOT/lib${PYTHONPATH:+:$PYTHONPATH}" python3 - "$RAW_SESSIONS_DIR" "$JOB_DIR/token-usage.json" "$MODEL" "$BATCH_ID" "$RUN_ID" "$CONVERSATION_FILE" "$MODE" "${BENCHMARK_PRICING_URL:-}" <<'PY'
import json
import sys
from pathlib import Path

try:
    from usage_costing import build_cost_calculation, derive_cost, load_pricing_table, provider_for_session_family, resolve_billable_model
except ImportError:
    from usage_pricing import build_cost_calculation, derive_cost, load_pricing_table, provider_for_session_family, resolve_billable_model

root = Path(sys.argv[1])
out = Path(sys.argv[2])
model_arg = sys.argv[3]
batch_id = sys.argv[4]
run_id = sys.argv[5]
conversation_file = Path(sys.argv[6])
scenario = sys.argv[7]
pricing_source = sys.argv[8] or None
totals = {
    "input_tokens": 0,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "output_tokens": 0,
    "reasoning_tokens": 0,
    "total_tokens": 0,
    "estimated_cost_usd": 0.0,
}
latest_total_usage = None
observed_billable_model = ""

def add_usage(obj):
    global latest_total_usage, observed_billable_model
    if isinstance(obj, dict) and obj.get("type") == "turn_context":
        payload = obj.get("payload") or {}
        candidate = payload.get("model")
        if isinstance(candidate, str) and candidate.strip():
            observed_billable_model = candidate.strip()
            return
    payload = obj.get("payload") if isinstance(obj, dict) else None
    if isinstance(payload, dict) and payload.get("type") == "token_count":
        info = payload.get("info")
        if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
            latest_total_usage = info["total_token_usage"]
            return
    usage = obj.get("usage") if isinstance(obj, dict) else None
    if not isinstance(usage, dict):
        usage = obj if isinstance(obj, dict) else {}
    mapping = {
        "input_tokens": ["input_tokens", "inputTokens"],
        "cache_read_tokens": ["cache_read_input_tokens", "cacheReadInputTokens", "cached_input_tokens", "cachedInputTokens"],
        "cache_creation_tokens": ["cache_creation_input_tokens", "cacheCreationInputTokens"],
        "output_tokens": ["output_tokens", "outputTokens"],
        "reasoning_tokens": ["reasoning_tokens", "reasoningTokens", "reasoning_output_tokens"],
        "total_tokens": ["total_tokens", "totalTokens"],
        "estimated_cost_usd": ["estimated_cost_usd", "costUSD", "totalCost"],
    }
    for dest, keys in mapping.items():
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                totals[dest] += value
                break

for path in root.rglob("*"):
    if not path.is_file():
        continue
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        continue
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            add_usage(json.loads(line))
        except Exception:
            pass

if isinstance(latest_total_usage, dict):
    totals["input_tokens"] = int(latest_total_usage.get("input_tokens") or 0)
    totals["cache_read_tokens"] = int(latest_total_usage.get("cached_input_tokens") or latest_total_usage.get("cache_read_input_tokens") or 0)
    totals["cache_creation_tokens"] = int(latest_total_usage.get("cache_creation_input_tokens") or 0)
    totals["output_tokens"] = int(latest_total_usage.get("output_tokens") or 0)
    totals["reasoning_tokens"] = int(latest_total_usage.get("reasoning_output_tokens") or latest_total_usage.get("reasoning_tokens") or 0)
    totals["total_tokens"] = int(latest_total_usage.get("total_tokens") or 0)
if not totals["total_tokens"]:
    totals["total_tokens"] = totals["input_tokens"] + totals["cache_read_tokens"] + totals["cache_creation_tokens"] + totals["output_tokens"] + totals["reasoning_tokens"]
totals["fresh_input_tokens"] = max(totals["input_tokens"] - totals["cache_read_tokens"], 0) + totals["cache_creation_tokens"]
totals["normalized_total_tokens"] = totals["fresh_input_tokens"] + totals["output_tokens"] + totals["reasoning_tokens"]
totals["input_includes_cache"] = model_arg == "codex"
provider = provider_for_session_family(model_arg) or model_arg
billable_model, billable_model_source = resolve_billable_model(provider, observed_billable_model)
cost = derive_cost(
    load_pricing_table(pricing_source),
    billable_model,
    input_tokens=float(totals["input_tokens"]),
    cache_read_tokens=float(totals["cache_read_tokens"]),
    cache_creation_tokens=float(totals["cache_creation_tokens"]),
    output_tokens=float(totals["output_tokens"]),
    input_includes_cache=model_arg == "codex",
)
summary_cost = {
    "derived_total_cost_usd": cost.get("derived_total_cost_usd"),
    "pricing_missing": cost.get("pricing_missing"),
    "pricing_source": cost.get("pricing_source"),
}
totals.update({
    "billable_model": billable_model,
    "billable_model_source": billable_model_source,
    **summary_cost,
})
if cost.get("derived_total_cost_usd") is not None:
    totals["estimated_cost_usd"] = cost["derived_total_cost_usd"]
out.write_text(json.dumps(totals, indent=2, sort_keys=True))
cost_calculation = build_cost_calculation(
    batch_id=batch_id,
    run_id=run_id,
    test_id=run_id.split("__", 1)[0] or conversation_file.stem,
    model=model_arg,
    scenario=scenario,
    billable_model=billable_model,
    billable_model_source=billable_model_source,
    token_totals=totals,
    cost=cost,
)
(out.parent / "cost-calculation.json").write_text(json.dumps(cost_calculation, indent=2, sort_keys=True))
PY
}

echo "START run_id=$RUN_ID model=$MODEL mode=$MODE sha=$INVOKER_SHA"
log_step "CONFIG run_id=$RUN_ID model=$MODEL mode=$MODE conversation=$CONVERSATION_FILE invoker_sha=$INVOKER_SHA"
snapshot_session_dirs
log_step "SNAPSHOT_SESSIONS before"
clear_non_credential_state
log_step "CLEAR_STATE complete"
prepare_prompt_file
log_step "PROMPT_READY file=$PROMPT_FILE bytes=$(wc -c < "$PROMPT_FILE" | tr -d ' ')"
set_stage "checkout"
install_checkout
log_step "CHECKOUT_READY dir=$CHECKOUT_DIR"

status="succeeded"
case "$MODE" in
  baseline_direct)
    baseline_var="BENCHMARK_BASELINE_${MODEL^^}_COMMAND"
    log_step "TEST_START kind=baseline_direct model=$MODEL"
    set_stage "baseline_direct"
    if ! run_template "${!baseline_var:-}"; then
      default_baseline
    fi
    log_step "TEST_PASS kind=baseline_direct model=$MODEL"
    ;;
  invoker_workflow)
    log_step "TEST_START kind=plan_to_invoker model=$MODEL"
    set_stage "plan_generation"
    default_plan
    log_step "TEST_PASS kind=plan_to_invoker model=$MODEL generated_plan=$GENERATED_PLAN"
    log_step "TEST_START kind=invoker_submit autofix=0 model=$MODEL"
    default_invoker_submit ""
    log_step "TEST_PASS kind=invoker_submit autofix=0 model=$MODEL"
    status="review_ready"
    ;;
  invoker_auto_fix)
    log_step "TEST_START kind=plan_to_invoker model=$MODEL"
    set_stage "plan_generation"
    default_plan
    log_step "TEST_PASS kind=plan_to_invoker model=$MODEL generated_plan=$GENERATED_PLAN"
    log_step "TEST_START kind=invoker_submit autofix=1 model=$MODEL"
    default_invoker_submit "1"
    log_step "TEST_PASS kind=invoker_submit autofix=1 model=$MODEL"
    status="review_ready"
    ;;
  *) die "Unsupported mode: $MODE" ;;
esac

collect_new_sessions
log_step "COLLECT_SESSIONS complete"
set_stage "token_usage"
extract_token_usage
log_step "TOKEN_USAGE extracted"
touch "$JOB_DIR/invoker-events.jsonl"
set_stage ""
write_job_json "$status" 0 ""
cleanup_job_runtime
echo "END run_id=$RUN_ID status=$status"
