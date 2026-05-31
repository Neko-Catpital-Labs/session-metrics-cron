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
PLAN_INSPECTION="$JOB_DIR/plan-inspection.json"
INVOKER_DB_DIR_JOB="$JOB_DIR/invoker-db"
INVOKER_IPC_SOCKET_JOB="$INVOKER_DB_DIR_JOB/ipc-transport.sock"
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
scenario_key = token_usage.get("scenario_key") or f"{Path(conv).stem}/{mode}/{model}"
plan_inspection_path = job_dir / "plan-inspection.json"
plan_inspection = {}
if plan_inspection_path.exists():
    try:
        plan_inspection = json.loads(plan_inspection_path.read_text())
    except Exception:
        plan_inspection = {}
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

def read_first_existing_artifact(names, limit=50000):
    for name in names:
        text = read_artifact(name, limit=limit)
        if text:
            return text
    return ""

def tail_lines(text, line_limit=120, char_limit=20000):
    lines = str(text or "").splitlines()
    trimmed = "\n".join(lines[-line_limit:])
    if len(trimmed) > char_limit:
        return trimmed[-char_limit:]
    return trimmed

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

def parse_json_lines(text):
    objects = []
    for line in str(text or "").splitlines():
        compact = line.strip()
        if not compact.startswith("{"):
            continue
        try:
            parsed = json.loads(compact)
        except Exception:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
    return objects

def invoker_cli_failure_message(stdout, stderr):
    result_object = None
    for item in reversed(parse_json_lines(stdout)):
        result = item.get("result")
        workflow = item.get("workflow")
        if isinstance(result, dict) or isinstance(workflow, dict):
            result_object = item
            break

    failed_task = ""
    for line in str(stdout or "").splitlines():
        match = re.search(r"findNewlyReadyTasks\([^/()]+/([^()]+)\)", line)
        if match:
            failed_task = match.group(1)

    details = []
    if result_object:
        result = result_object.get("result") if isinstance(result_object.get("result"), dict) else {}
        workflow = result_object.get("workflow") if isinstance(result_object.get("workflow"), dict) else {}
        workflow_id = result.get("workflowId") or workflow.get("id") or ""
        status = result.get("status") or workflow.get("status") or "failed"
        if workflow_id:
            details.append(f"workflow {workflow_id} {status}")
        completed = result.get("completedTasks")
        failed = result.get("failedTasks")
        if completed is not None:
            details.append(f"completedTasks={completed}")
        if failed is not None:
            details.append(f"failedTasks={failed}")
    if failed_task:
        details.append(f"first failed/ready task={failed_task}")

    error_line = matching_message(
        "\n".join([stderr, stdout]),
        "error:",
        "failed:",
        "failed to",
        "exception",
        "enoent",
        "permission denied",
        "authentication failed",
    )
    if error_line:
        details.append(error_line)

    if details:
        return "Invoker CLI run failed: " + "; ".join(details)
    return concise_message(stderr, stdout, "Invoker CLI run failed")

def derive_failure(status_value, exit_code_value, stage_value):
    if int(exit_code_value) == 0 and status_value not in {"failed", "timeout", "invalid_job_json"}:
        return "", "", ""
    stderr = read_artifact("stderr.log")
    stdout = read_artifact("stdout.log")
    plan = read_first_existing_artifact(("generated-plan.yaml", "generated_plan.yaml"))
    combined = "\n".join([stderr, stdout, plan])
    lowered = combined.lower()
    stage = stage_value or "unknown"

    git_ref_signatures = (
        "cannot lock ref",
        "unable to create directory for .git/refs/heads",
        "unable to create",
        ".git/refs/heads",
    )
    if "cannot lock ref" in lowered and ".git/refs/heads" in lowered:
        return stage, "invoker_git_ref_create_failed", matching_message(combined, *git_ref_signatures) or concise_message(stderr, stdout, "Invoker git ref creation failed")
    if "not logged in" in lowered and "please run /login" in lowered:
        return stage, "model_auth_failed", matching_message(combined, "not logged in", "please run /login") or concise_message(stderr, stdout, "Model authentication failed")
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
    if stage == "invoker_cli_build":
        return stage, "invoker_cli_build_failed", concise_message(stderr, stdout, "Invoker CLI build failed")
    timeout_stages = {"timeout", "job_timeout", "watchdog_timeout"}
    timeout_message = matching_message(combined, "timed out", "timeout after", "operation timed out", "watchdog timeout", "job timeout")
    if status_value == "timeout" or stage in timeout_stages or stage.endswith("_timeout") or timeout_message:
        return stage, "timeout", timeout_message or concise_message(stderr, stdout, "timeout")
    if stage == "invoker_cli_run":
        return stage, "invoker_cli_run_failed", invoker_cli_failure_message(stdout, stderr)
    if stage == "checkout":
        return stage, "checkout_failed", concise_message(stderr, stdout, "Checkout failed")
    if stage == "plan_generation":
        plan_generation_message = matching_message(
            combined,
            "Plan generation failed benchmark inspection",
            "mergeMode: github",
            "mergeMode",
        )
        return stage, "plan_generation_failed", plan_generation_message or concise_message(stderr, stdout, plan)
    if stage == "token_usage":
        return stage, "token_usage_failed", concise_message(stderr, stdout, "Token usage extraction failed")
    return stage, "unknown", concise_message(stderr, stdout, plan, "Unknown benchmark failure")

failure_stage, failure_reason, failure_message = derive_failure(status, exit_code, failure_stage_arg)

failure_raw_output = {}
if int(exit_code) != 0 or status in {"failed", "timeout", "invalid_job_json"}:
    for artifact_name in (
        "current-stage",
        "steps.log",
        "stderr.log",
        "stdout.log",
        "generated-plan.yaml",
        "generated_plan.yaml",
        "plan-inspection.json",
    ):
        text = read_artifact(artifact_name, limit=100000)
        if text:
            failure_raw_output[artifact_name] = tail_lines(text)

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
    "scenario_key": scenario_key,
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
    "failure_raw_output": failure_raw_output,
    "commits": commits,
    "changed_files": [line[3:] if len(line) > 3 else line for line in changed.splitlines()],
    "step_log": steps,
    "test_configuration": {
        "model": model,
        "mode": mode,
        "scenario": mode,
        "scenario_key": scenario_key,
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
    "plan_inspection": plan_inspection,
    "artifacts": {
        "stdout": "stdout.log",
        "stderr": "stderr.log",
        "steps": "steps.log",
        "prompt": "prompt.txt",
        "cost_calculation": "cost-calculation.json",
        "token_ledger": "token-ledger.jsonl",
        "generated_plan": "generated-plan.yaml",
        "generated_plan_alt": "generated_plan.yaml",
        "plan_inspection": "plan-inspection.json",
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
  if [[ "$code" -ne 0 ]]; then
    local failure_stage="$CURRENT_STAGE"
    if [[ -f "$JOB_DIR/current-stage" ]]; then
      failure_stage="$(cat "$JOB_DIR/current-stage")"
    fi
    if [[ ! -f "$JOB_DIR/job.json" || "$failure_stage" == "electron_cleanup" ]]; then
      if [[ -f "$JOB_DIR/session-files-before.txt" ]]; then
        collect_new_sessions || true
        extract_token_usage || true
      fi
      write_job_json failed "$code" "$failure_stage" || true
    fi
    print_failure_artifacts "$failure_stage" || true
  fi
  if [[ "$code" -ne 0 ]]; then
    if [[ "$CURRENT_STAGE" != "electron_cleanup" ]]; then
      set_stage "electron_cleanup"
      scoped_electron_cleanup || true
    fi
    cleanup_job_runtime preserve-invoker-db || true
  fi
}
trap on_exit EXIT

print_failure_artifacts() {
  local failure_stage="${1:-unknown}"
  {
    echo ""
    echo "===== BENCHMARK JOB FAILURE ====="
    echo "run_id=$RUN_ID model=$MODEL mode=$MODE stage=$failure_stage"
    for artifact in current-stage steps.log stderr.log stdout.log generated-plan.yaml generated_plan.yaml plan-inspection.json job.json; do
      local path="$JOB_DIR/$artifact"
      [[ -f "$path" ]] || continue
      echo ""
      echo "----- $artifact -----"
      tail -n 120 "$path" || true
    done
    echo "===== END BENCHMARK JOB FAILURE ====="
    echo ""
  } >&2
}

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
  cleanup_invoker_managed_worktrees_and_refs
}

cleanup_invoker_managed_worktrees_and_refs() {
  [[ -n "${HOME:-}" && "$HOME" != "/" ]] || return 0
  local invoker_home="$HOME/.invoker"
  local repos_dir="$invoker_home/repos"
  local worktrees_dir="$invoker_home/worktrees"

  if [[ -d "$repos_dir" ]]; then
    local repo
    for repo in "$repos_dir"/*; do
      [[ -d "$repo/.git" ]] || continue

      local managed_branch
      while IFS= read -r managed_branch; do
        [[ -n "$managed_branch" ]] || continue
        cleanup_invoker_remote_branch "$repo" origin "$managed_branch"
        cleanup_invoker_remote_branch "$repo" upstream "$managed_branch"
      done < <(collect_invoker_managed_worktree_branches "$repo" "$worktrees_dir")

      local worktree
      while IFS= read -r worktree; do
        [[ -n "$worktree" ]] || continue
        if [[ "$worktree" == "$worktrees_dir" || "$worktree" == "$worktrees_dir/"* ]]; then
          git -C "$repo" worktree remove --force "$worktree" >/dev/null 2>&1 || true
        fi
      done < <(git -C "$repo" worktree list --porcelain 2>/dev/null | awk '/^worktree / { sub(/^worktree /, ""); print }' || true)

      git -C "$repo" worktree prune >/dev/null 2>&1 || true
      git -C "$repo" for-each-ref --format='%(refname)' \
        refs/heads/experiment refs/heads/invoker refs/heads/reconciliation 2>/dev/null \
        | while IFS= read -r ref; do
            [[ -n "$ref" ]] || continue
            git -C "$repo" update-ref -d "$ref" >/dev/null 2>&1 || true
          done
    done
  fi

  rm -rf "$worktrees_dir" 2>/dev/null || true
}

collect_invoker_managed_worktree_branches() {
  local repo="$1"
  local worktrees_dir="$2"
  local current_worktree=""
  local current_branch=""

  emit_managed_branch() {
    [[ -n "$current_worktree" && -n "$current_branch" ]] || return 0
    [[ "$current_worktree" == "$worktrees_dir" || "$current_worktree" == "$worktrees_dir/"* ]] || return 0
    case "$current_branch" in
      main|master|develop|development|trunk) return 0 ;;
    esac
    printf '%s\n' "$current_branch"
  }

  while IFS= read -r line; do
    if [[ -z "$line" ]]; then
      emit_managed_branch
      current_worktree=""
      current_branch=""
      continue
    fi
    case "$line" in
      worktree\ *) current_worktree="${line#worktree }" ;;
      branch\ refs/heads/*) current_branch="${line#branch refs/heads/}" ;;
      branch\ *) current_branch="" ;;
    esac
  done < <(git -C "$repo" worktree list --porcelain 2>/dev/null || true)
  emit_managed_branch
}

cleanup_invoker_remote_branch() {
  local repo="$1"
  local remote="$2"
  local branch="$3"

  [[ -n "$branch" ]] || return 0
  git -C "$repo" remote get-url "$remote" >/dev/null 2>&1 || return 0

  local output
  if output="$(git -C "$repo" push "$remote" ":refs/heads/$branch" 2>&1)"; then
    return 0
  fi

  case "$output" in
    *"remote ref does not exist"*|*"not found"*|*"unable to delete"*"not found"*) return 0 ;;
  esac
  echo "WARN: failed to delete managed worktree branch '$branch' from remote '$remote': $output" >&2
  return 0
}

cleanup_job_runtime() {
  if [[ "${1:-}" == "preserve-invoker-db" ]]; then
    rm -rf "$CHECKOUT_DIR" 2>/dev/null || true
    return 0
  fi
  rm -rf "$CHECKOUT_DIR" "$INVOKER_DB_DIR_JOB" 2>/dev/null || true
}

process_listing() {
  if [[ -n "${BENCHMARK_PROCESS_LIST_FILE:-}" ]]; then
    cat "$BENCHMARK_PROCESS_LIST_FILE"
    return 0
  fi
  ps -axo pid=,ppid=,command=
}

current_process_tree_pids() {
  local listing_file="$JOB_DIR/process-listing.txt"
  process_listing > "$listing_file"
  python3 - "$listing_file" "$$" "${BASHPID:-$$}" "$PPID" <<'PY'
import os
import sys
from collections import defaultdict
from pathlib import Path

listing_path = sys.argv[1]
roots = {int(pid) for pid in sys.argv[2:] if str(pid).isdigit()}
parents = {}
children = defaultdict(list)
for line in Path(listing_path).read_text(errors="ignore").splitlines():
    parts = line.strip().split(None, 2)
    if len(parts) < 2:
        continue
    try:
        pid = int(parts[0])
        ppid = int(parts[1])
    except ValueError:
        continue
    parents[pid] = ppid
    children[ppid].append(pid)

excluded = set(roots)
for root in list(roots):
    pid = root
    while pid in parents and parents[pid] not in excluded:
        pid = parents[pid]
        excluded.add(pid)

stack = list(roots)
while stack:
    pid = stack.pop()
    for child in children.get(pid, []):
        if child in excluded:
            continue
        excluded.add(child)
        stack.append(child)

for pid in sorted(excluded):
    print(pid)
PY
}

current_job_electron_pids() {
  local listing_file="$JOB_DIR/process-listing.txt"
  local excluded_file="$JOB_DIR/process-exclude-pids.txt"
  process_listing > "$listing_file"
  current_process_tree_pids > "$excluded_file" || true
  python3 - "$listing_file" "$CHECKOUT_DIR" "$INVOKER_DB_DIR_JOB" "$INVOKER_IPC_SOCKET_JOB" "$INVOKER_CONFIG_JOB" "$JOB_DIR" "$excluded_file" <<'PY'
import sys
from pathlib import Path

listing_path = Path(sys.argv[1])
needles = [value for value in sys.argv[2:7] if value]
excluded_path = Path(sys.argv[7])
excluded = set()
try:
    excluded = {int(line.strip()) for line in excluded_path.read_text().splitlines() if line.strip().isdigit()}
except Exception:
    pass

for line in listing_path.read_text(errors="ignore").splitlines():
    parts = line.strip().split(None, 2)
    if len(parts) < 3:
        continue
    try:
        pid = int(parts[0])
    except ValueError:
        continue
    command = parts[2]
    lowered = command.lower()
    if pid in excluded:
        continue
    if "kill-all-electron.sh" in lowered:
        continue
    if not any(needle in command for needle in needles):
        continue
    print(pid)
PY
}

signal_process() {
  local signal="$1"
  local pid="$2"
  if [[ -n "${BENCHMARK_KILL_LOG:-}" ]]; then
    printf '%s %s\n' "$signal" "$pid" >> "$BENCHMARK_KILL_LOG"
    return 0
  fi
  kill "-$signal" "$pid"
}

pid_is_running() {
  local pid="$1"
  if [[ -n "${BENCHMARK_PROCESS_LIST_FILE:-}" ]]; then
    grep -Eq "^[[:space:]]*$pid[[:space:]]" "$BENCHMARK_PROCESS_LIST_FILE"
    return $?
  fi
  kill -0 "$pid" 2>/dev/null
}

scoped_electron_cleanup() {
  local pids=()
  local pid
  mapfile -t pids < <(current_job_electron_pids)
  if [[ "${#pids[@]}" -eq 0 ]]; then
    return 0
  fi
  log_step "ELECTRON_CLEANUP scoped_term pids=${pids[*]}"
  for pid in "${pids[@]}"; do
    signal_process TERM "$pid"
  done
  sleep "${BENCHMARK_ELECTRON_CLEANUP_TERM_WAIT_SECONDS:-2}"
  mapfile -t pids < <(current_job_electron_pids)
  for pid in "${pids[@]}"; do
    if pid_is_running "$pid"; then
      log_step "ELECTRON_CLEANUP scoped_kill pid=$pid"
      signal_process KILL "$pid"
    fi
  done
}

default_checkout_setup() {
  if [[ ! -d packages/cli || ! -d packages/app ]]; then
    return 0
  fi

  set_stage "checkout_setup"
  eval "${BENCHMARK_INVOKER_CLI_BUILD_COMMAND:-pnpm --filter @invoker/cli build}"
  eval "${BENCHMARK_INVOKER_APP_BUILD_COMMAND:-pnpm --filter @invoker/app build}"
  unset ELECTRON_RUN_AS_NODE
  eval "${BENCHMARK_INVOKER_INSTALL_SKILLS_COMMAND:-node scripts/electron.cjs packages/app/dist/main.js --headless install-skills reinstall}"
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
      set_stage "checkout_setup"
      export CHECKOUT_DIR CONVERSATION_FILE PROMPT_FILE MODEL MODE JOB_DIR GENERATED_PLAN INVOKER_SHA INVOKER_DB_DIR_JOB INVOKER_IPC_SOCKET_JOB INVOKER_CONFIG_JOB
      eval "$BENCHMARK_CHECKOUT_SETUP_COMMAND"
    else
      default_checkout_setup
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
    export CHECKOUT_DIR CONVERSATION_FILE PROMPT_FILE MODEL MODE JOB_DIR GENERATED_PLAN INVOKER_SHA INVOKER_DB_DIR_JOB INVOKER_IPC_SOCKET_JOB INVOKER_CONFIG_JOB
    eval "$template"
  )
}

prepare_prompt_file() {
  python3 - "$CONVERSATION_FILE" "$PROMPT_FILE" "${BENCHMARK_MAX_PROMPT_CHARS:-120000}" "${BENCHMARK_PLAN_PROMPT_SOURCE:-latest_user}" <<'PY'
import json
import sys
from pathlib import Path

src = Path(sys.argv[1])
out = Path(sys.argv[2])
max_chars = int(sys.argv[3])
source_mode = sys.argv[4]

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
if source_mode in {"plan_to_invoker_direct", "direct_skill"}:
    prompt = (
        "/plan-to-invoker\n\n"
        "Use the invoker-plan-to-invoker skill to convert this session transcript into Invoker YAML. "
        "Return only valid YAML, no Markdown fences. Let the skill choose prompt: versus command: tasks naturally. "
        "onFinish: none is fine for this scan.\n\n"
        f"Session file: {src}\n\n"
        f"{src.read_text(errors='ignore')}"
    )
elif source_mode in {"full_conversation", "full_jsonl", "raw_jsonl"}:
    prompt = f"Session file: {src}\n\n{src.read_text(errors='ignore')}"
elif src.suffix.lower() == ".jsonl":
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

if source_mode in {"plan_to_invoker_direct", "direct_skill", "full_conversation", "full_jsonl", "raw_jsonl"}:
    pass
elif prompts:
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
  local benchmark_plan_constraint="${BENCHMARK_PLAN_CONSTRAINT:-For this benchmark, generate Invoker YAML from the session input. Use mergeMode: manual. Do not use mergeMode: github. Do not include top-level or task-level externalDependencies; isolated benchmark runs must not depend on external services, upstream workflow records, upstream branches, experiment artifacts, local session files, git commits, pull requests, or long test suites. Write the final YAML plan to the absolute path in the GENERATED_PLAN environment variable, not to a relative file. Do not submit the plan. Let the plan-to-invoker skill choose prompt: versus command: tasks naturally. Do not print the YAML as your final answer; after writing GENERATED_PLAN, print only a short confirmation.}"
  case "$MODEL" in
    codex)
      if ! run_template "${BENCHMARK_PLAN_CODEX_COMMAND:-}"; then
        command -v codex >/dev/null || die "codex CLI not found and BENCHMARK_PLAN_CODEX_COMMAND is unset"
        {
          printf '/plan-to-invoker\n'
          printf '%s\n\n' "$benchmark_plan_constraint"
          cat "$PROMPT_FILE"
        } | (
          cd "$CHECKOUT_DIR"
          export GENERATED_PLAN JOB_DIR CONVERSATION_FILE PROMPT_FILE
          codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox -
        )
      fi
      ;;
    claude)
      if ! run_template "${BENCHMARK_PLAN_CLAUDE_COMMAND:-}"; then
        command -v claude >/dev/null || die "claude CLI not found and BENCHMARK_PLAN_CLAUDE_COMMAND is unset"
        (
          cd "$CHECKOUT_DIR"
          export GENERATED_PLAN JOB_DIR CONVERSATION_FILE PROMPT_FILE
          claude --add-dir "$JOB_DIR" --permission-mode acceptEdits -p "/plan-to-invoker
$benchmark_plan_constraint

$(cat "$PROMPT_FILE")"
        )
      fi
      ;;
    *) die "Unsupported model: $MODEL" ;;
  esac
}

inspect_generated_plan() {
  python3 - "$GENERATED_PLAN" "$PLAN_INSPECTION" "$CHECKOUT_DIR" "$MODEL" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

plan_path = Path(sys.argv[1])
inspection_path = Path(sys.argv[2])
checkout_path = Path(sys.argv[3])
benchmark_model = sys.argv[4].strip()

if not plan_path.exists():
    for fallback in (
        checkout_path / "generated-plan.yaml",
        checkout_path / "generated_plan.yaml",
        plan_path.parent / "generated_plan.yaml",
    ):
        if fallback.exists():
            plan_path.write_text(fallback.read_text(errors="ignore"))
            break

raw = plan_path.read_text(errors="ignore") if plan_path.exists() else ""

def looks_like_plan(text):
    return bool(re.search(r"(?m)^name:\s*\S", text) and re.search(r"(?m)^repoUrl:\s*\S", text) and re.search(r"(?m)^tasks:\s*(?:$|\[)", text))

def fenced_blocks(text):
    pattern = re.compile(r"```(?:ya?ml|yaml|yml)?[^\n]*\n(.*?)```", re.IGNORECASE | re.DOTALL)
    return [match.group(1).strip() + "\n" for match in pattern.finditer(text)]

def line_window(text):
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if re.match(r"^name:\s*\S", line):
            start = index
            break
    if start is None:
        return text.strip() + ("\n" if text.strip() else "")
    return "\n".join(lines[start:]).strip() + "\n"

candidates = fenced_blocks(raw)
candidates.append(line_window(raw))
candidates.append(raw.strip() + ("\n" if raw.strip() else ""))

yaml_text = ""
for candidate in candidates:
    if looks_like_plan(candidate):
        yaml_text = candidate
        break
if not yaml_text and candidates:
    yaml_text = candidates[0]

def force_execution_agent(text, agent_name):
    if agent_name not in {"codex", "claude"}:
        return text, 0, 0

    lines = text.splitlines()
    output = []
    in_tasks = False
    task_lines = []
    inserted_count = 0
    rewritten_count = 0

    def flush_task(block):
        nonlocal inserted_count, rewritten_count
        if not block:
            return

        has_prompt = any(re.match(r"^\s+prompt:\s*(?:\||>|$)", line) for line in block)
        has_agent = False
        rewritten = []
        for line in block:
            match = re.match(r"^(\s+)executionAgent:\s*.*$", line)
            if match:
                has_agent = True
                if line.strip() != f"executionAgent: {agent_name}":
                    rewritten_count += 1
                rewritten.append(f"{match.group(1)}executionAgent: {agent_name}")
            else:
                rewritten.append(line)

        if has_prompt and not has_agent:
            first = rewritten[0]
            item_indent = re.match(r"^(\s*)-\s+", first)
            field_indent = (item_indent.group(1) + "  ") if item_indent else "    "
            rewritten.insert(1, f"{field_indent}executionAgent: {agent_name}")
            inserted_count += 1

        output.extend(rewritten)

    for line in lines:
        if re.match(r"^tasks:\s*$", line):
            flush_task(task_lines)
            task_lines = []
            in_tasks = True
            output.append(line)
            continue

        if in_tasks and re.match(r"^[A-Za-z0-9_-]+:", line):
            flush_task(task_lines)
            task_lines = []
            in_tasks = False
            output.append(line)
            continue

        if in_tasks and re.match(r"^\s{2}-\s+", line):
            flush_task(task_lines)
            task_lines = [line]
            continue

        if in_tasks and task_lines:
            task_lines.append(line)
        else:
            output.append(line)

    flush_task(task_lines)
    trailing_newline = "\n" if text.endswith("\n") else ""
    return "\n".join(output) + trailing_newline, inserted_count, rewritten_count

yaml_text, execution_agent_inserted_count, execution_agent_rewritten_count = force_execution_agent(yaml_text, benchmark_model)

top_level = {}
for match in re.finditer(r"(?m)^([A-Za-z0-9_-]+):(?:\s*(.*))?$", yaml_text):
    top_level[match.group(1)] = (match.group(2) or "").strip()

task_count = 0
prompt_count = len(re.findall(r"(?m)^\s+prompt:\s*(?:\||>|$)", yaml_text))
command_count = len(re.findall(r"(?m)^\s+command:\s*", yaml_text))
in_tasks = False
for line in yaml_text.splitlines():
    if re.match(r"^tasks:\s*$", line):
        in_tasks = True
        continue
    if in_tasks and re.match(r"^[A-Za-z0-9_-]+:", line):
        in_tasks = False
    if in_tasks and re.match(r"^\s{2}-\s+", line):
        task_count += 1
if top_level.get("tasks", "").startswith("["):
    task_count = max(task_count, top_level["tasks"].count("{"))

merge_mode = top_level.get("mergeMode", "").strip().strip('"\'')
inspection = {
    "extracted_yaml": yaml_text != raw,
    "has_name": bool(top_level.get("name")),
    "has_repoUrl": bool(top_level.get("repoUrl")),
    "has_tasks": "tasks" in top_level,
    "mergeMode": merge_mode,
    "mergeMode_manual": merge_mode == "manual",
    "task_count": task_count,
    "prompt_count": prompt_count,
    "command_count": command_count,
    "executionAgent": benchmark_model if benchmark_model in {"codex", "claude"} else "",
    "executionAgent_inserted_count": execution_agent_inserted_count,
    "executionAgent_rewritten_count": execution_agent_rewritten_count,
}
inspection_path.write_text(json.dumps(inspection, indent=2, sort_keys=True) + "\n")

errors = []
if not inspection["has_name"]:
    errors.append("missing top-level name")
if not inspection["has_repoUrl"]:
    errors.append("missing top-level repoUrl")
if not inspection["has_tasks"]:
    errors.append("missing top-level tasks")
if task_count <= 0:
    errors.append("no generated tasks found")
if merge_mode != "manual":
    if merge_mode == "github":
        errors.append("generated plan used mergeMode: github despite benchmark constraint")
    elif merge_mode:
        errors.append(f"generated plan used mergeMode: {merge_mode} instead of manual")
    else:
        errors.append("generated plan omitted mergeMode: manual")
if re.search(r"(?m)^externalDependencies:\s*$", yaml_text):
    errors.append("generated plan used top-level externalDependencies despite isolated benchmark constraint")
if re.search(r"(?m)^\s{4,}externalDependencies:\s*$", yaml_text):
    errors.append("generated plan used task-level externalDependencies despite isolated benchmark constraint")

if yaml_text:
    plan_path.write_text(yaml_text)

if errors:
    raise SystemExit("Plan generation failed benchmark inspection: " + "; ".join(errors))
PY
}

default_invoker_cli_run() {
  (
    cd "$CHECKOUT_DIR"
    export INVOKER_AUTOFIX="$1"
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
    export INVOKER_IPC_SOCKET="$INVOKER_IPC_SOCKET_JOB"
    export INVOKER_REPO_CONFIG_PATH="$INVOKER_CONFIG_JOB"
    export CHECKOUT_DIR CONVERSATION_FILE PROMPT_FILE MODEL MODE JOB_DIR GENERATED_PLAN INVOKER_SHA INVOKER_DB_DIR_JOB INVOKER_IPC_SOCKET_JOB INVOKER_CONFIG_JOB
    if [[ -n "${BENCHMARK_INVOKER_SUBMIT_COMMAND:-}" ]]; then
      set_stage "invoker_cli_run"
      run_template "$BENCHMARK_INVOKER_SUBMIT_COMMAND"
      return 0
    fi
    set_stage "invoker_cli_build"
    eval "${BENCHMARK_INVOKER_CLI_BUILD_COMMAND:-pnpm --filter @invoker/cli build}"
    local cli_path="${BENCHMARK_INVOKER_CLI_PATH:-packages/cli/dist/index.js}"
    [[ -f "$CHECKOUT_DIR/$cli_path" ]] || die "Built Invoker CLI not found: $CHECKOUT_DIR/$cli_path"
    set_stage "invoker_cli_run"
    node "$CHECKOUT_DIR/$cli_path" run "$GENERATED_PLAN" \
      --standalone \
      --db-dir "$INVOKER_DB_DIR_JOB" \
      --config "$INVOKER_CONFIG_JOB" \
      --json
  )
}

extract_token_usage() {
  PYTHONPATH="$BENCHMARK_ROOT/scripts:$BENCHMARK_ROOT/../scripts:$BENCHMARK_ROOT/lib${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$BENCHMARK_ROOT/scripts/token_ledger.py" \
      --raw-sessions-dir "$RAW_SESSIONS_DIR" \
      --stdout-log "$STDOUT_LOG" \
      --generated-plan "$GENERATED_PLAN" \
      --token-usage-out "$JOB_DIR/token-usage.json" \
      --ledger-out "$JOB_DIR/token-ledger.jsonl" \
      --cost-calculation-out "$JOB_DIR/cost-calculation.json" \
      --model "$MODEL" \
      --batch-id "$BATCH_ID" \
      --run-id "$RUN_ID" \
      --conversation-file "$CONVERSATION_FILE" \
      --mode "$MODE" \
      --pricing-source "${BENCHMARK_PRICING_URL:-}"
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
    inspect_generated_plan
    log_step "TEST_PASS kind=plan_to_invoker model=$MODEL generated_plan=$GENERATED_PLAN"
    log_step "TEST_START kind=invoker_cli_run autofix=0 model=$MODEL"
    default_invoker_cli_run ""
    log_step "TEST_PASS kind=invoker_cli_run autofix=0 model=$MODEL"
    status="succeeded"
    ;;
  invoker_auto_fix)
    log_step "TEST_START kind=plan_to_invoker model=$MODEL"
    set_stage "plan_generation"
    default_plan
    inspect_generated_plan
    log_step "TEST_PASS kind=plan_to_invoker model=$MODEL generated_plan=$GENERATED_PLAN"
    log_step "TEST_START kind=invoker_cli_run autofix=1 model=$MODEL"
    default_invoker_cli_run "1"
    log_step "TEST_PASS kind=invoker_cli_run autofix=1 model=$MODEL"
    status="succeeded"
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
set_stage "electron_cleanup"
scoped_electron_cleanup
set_stage ""
cleanup_job_runtime
echo "END run_id=$RUN_ID status=$status"
