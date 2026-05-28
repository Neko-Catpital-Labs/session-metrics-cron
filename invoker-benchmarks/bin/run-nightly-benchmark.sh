#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$DEFAULT_ROOT/config/benchmark.env"
WORKERS_FILE=""
JOB_SET_FILE=""
DRY_RUN=0
SMOKE=0
LIMIT=""
EMIT_MIXPANEL=0
EMIT_MIXPANEL_SET=0

usage() {
  cat <<'EOF'
Usage: run-nightly-benchmark.sh [--dry-run] [--smoke] [--limit N] [--job-set PATH] [--emit-mixpanel] [--no-emit-mixpanel] [--env-file PATH] [--workers-file PATH]

Options:
  --dry-run          Validate config and print the job assignment plan only.
  --smoke            Run one conversation across all modes for the first model on one worker.
  --limit N          Run only the first N matrix jobs.
  --job-set PATH     Run an explicit ordered JSON or TSV job set.
  --emit-mixpanel    Publish Mixpanel events after aggregation.
  --no-emit-mixpanel Write mixpanel-export.jsonl but do not publish.
  --env-file PATH    Benchmark env file. Defaults to config/benchmark.env.
  --workers-file PATH Worker inventory JSON. Defaults to config/workers.json.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --smoke) SMOKE=1; shift ;;
    --limit)
      LIMIT="${2:-}"
      [[ "$LIMIT" =~ ^[0-9]+$ ]] || die "Missing or invalid value for --limit"
      shift 2
      ;;
    --emit-mixpanel) EMIT_MIXPANEL=1; EMIT_MIXPANEL_SET=1; shift ;;
    --no-emit-mixpanel) EMIT_MIXPANEL=0; EMIT_MIXPANEL_SET=1; shift ;;
    --env-file)
      ENV_FILE="${2:-}"
      [[ -n "$ENV_FILE" ]] || die "Missing value for --env-file"
      shift 2
      ;;
    --workers-file)
      WORKERS_FILE="${2:-}"
      [[ -n "$WORKERS_FILE" ]] || die "Missing value for --workers-file"
      shift 2
      ;;
    --job-set)
      JOB_SET_FILE="${2:-}"
      [[ -n "$JOB_SET_FILE" ]] || die "Missing value for --job-set"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -f "$ENV_FILE" ]] || die "Missing env file: $ENV_FILE"

# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

export TZ="${TZ:-Asia/Hong_Kong}"
BENCHMARK_ROOT="${BENCHMARK_ROOT:-$DEFAULT_ROOT}"
WORKERS_FILE="${WORKERS_FILE:-$BENCHMARK_ROOT/config/workers.json}"
JOB_SET_FILE="${JOB_SET_FILE:-${BENCHMARK_JOB_SET_FILE:-}}"
MANIFEST_FILE="${MANIFEST_FILE:-$BENCHMARK_ROOT/config/corpus-manifest.json}"
RUNS_DIR="$BENCHMARK_ROOT/runs"
MODELS="${MODELS:-codex,claude}"
MODES="${MODES:-baseline_direct,invoker_workflow,invoker_auto_fix}"
WORKER_CONCURRENCY_PER_HOST="${WORKER_CONCURRENCY_PER_HOST:-1}"
BENCHMARK_SERIAL_JOBS="${BENCHMARK_SERIAL_JOBS:-1}"
BENCHMARK_JOB_SETTLE_SECONDS="${BENCHMARK_JOB_SETTLE_SECONDS:-2}"
if [[ "$EMIT_MIXPANEL_SET" -eq 0 && "$DRY_RUN" -eq 0 && "$SMOKE" -eq 0 && "${BENCHMARK_EMIT_MIXPANEL_DEFAULT:-1}" != "0" ]]; then
  EMIT_MIXPANEL=1
fi

[[ "$WORKER_CONCURRENCY_PER_HOST" == "1" ]] || die "Only WORKER_CONCURRENCY_PER_HOST=1 is currently supported"
[[ "$BENCHMARK_SERIAL_JOBS" == "0" || "$BENCHMARK_SERIAL_JOBS" == "1" ]] || die "BENCHMARK_SERIAL_JOBS must be 0 or 1"
[[ -n "${CORPUS_DIR:-}" ]] || die "Missing CORPUS_DIR"
[[ -n "${INVOKER_REPO:-}" ]] || die "Missing INVOKER_REPO"
[[ -n "${INVOKER_BRANCH:-}" ]] || die "Missing INVOKER_BRANCH"
if [[ -n "$JOB_SET_FILE" && ! -f "$JOB_SET_FILE" ]]; then
  die "Missing job set file: $JOB_SET_FILE"
fi

if [[ ! -f "$WORKERS_FILE" ]]; then
  if [[ -f "$HOME/.invoker/config.json" ]]; then
    log "workers.json missing; generating from ~/.invoker/config.json"
    "$BENCHMARK_ROOT/bin/sync-worker-credentials.sh" --write-workers "$WORKERS_FILE" --no-ssh
  else
    die "Missing workers file: $WORKERS_FILE"
  fi
fi

mapfile -t WORKERS < <(python3 - "$WORKERS_FILE" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
for worker in data.get("workers", []):
    if worker.get("enabled", True):
        user = worker.get("user") or "invoker"
        host = worker.get("host") or worker.get("name")
        port = int(worker.get("port") or 22)
        name = worker.get("name") or host
        if host:
            print(f"{name}\t{user}@{host}\t{port}")
PY
)

[[ "${#WORKERS[@]}" -gt 0 ]] || die "No enabled workers in $WORKERS_FILE"
if [[ "$SMOKE" -eq 1 ]]; then
  WORKERS=("${WORKERS[0]}")
fi

INVOKER_SHA="${INVOKER_SHA:-}"
if [[ -z "$INVOKER_SHA" ]]; then
  if INVOKER_SHA="$(git ls-remote "$INVOKER_REPO" "refs/heads/$INVOKER_BRANCH" | awk '{print $1}' | head -n 1)" && [[ -n "$INVOKER_SHA" ]]; then
    :
  elif [[ "$DRY_RUN" -eq 1 ]]; then
    INVOKER_SHA="dry-run-unresolved"
  else
    die "Unable to resolve $INVOKER_REPO $INVOKER_BRANCH"
  fi
fi

RUN_STAMP="$(date '+%Y-%m-%d_%H-%M-%S_%Z')"
BATCH_ID="${BATCH_ID:-${RUN_STAMP}_$(printf '%s' "$INVOKER_SHA" | cut -c1-12)_$$}"
BATCH_DIR="$RUNS_DIR/$BATCH_ID"
MATRIX_FILE="$BATCH_DIR/job-matrix.tsv"
ASSIGNMENTS_FILE="$BATCH_DIR/worker-assignments.tsv"
BATCH_LOG="$BATCH_DIR/run.log"
CONFIG_SNAPSHOT="$BATCH_DIR/config.json"

mkdir -p "$BATCH_DIR/jobs"
exec > >(tee -a "$BATCH_LOG") 2> >(tee -a "$BATCH_LOG" >&2)

write_batch_snapshot() {
  local phase="$1"
  local status="$2"
  python3 - "$CONFIG_SNAPSHOT" "$phase" "$status" "$BATCH_ID" "$BENCHMARK_ROOT" "$CORPUS_DIR" "$MANIFEST_FILE" "$WORKERS_FILE" "$JOB_SET_FILE" "$MODELS" "$MODES" "$SMOKE" "${LIMIT:-}" "$DRY_RUN" "$EMIT_MIXPANEL" "$INVOKER_REPO" "$INVOKER_BRANCH" "$INVOKER_SHA" "${WORKERS[@]}" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    out,
    phase,
    status,
    batch_id,
    benchmark_root,
    corpus_dir,
    manifest_file,
    workers_file,
    job_set_file,
    models,
    modes,
    smoke,
    limit,
    dry_run,
    emit_mixpanel,
    invoker_repo,
    invoker_branch,
    invoker_sha,
    *workers,
) = sys.argv[1:]

payload = {
    "batch_id": batch_id,
    "phase": phase,
    "status": status,
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "benchmark_root": benchmark_root,
    "corpus_dir": corpus_dir,
    "manifest_file": manifest_file,
    "workers_file": workers_file,
    "job_set_file": job_set_file,
    "models": [item.strip() for item in models.split(",") if item.strip()],
    "modes": [item.strip() for item in modes.split(",") if item.strip()],
    "smoke": smoke == "1",
    "limit": int(limit) if limit else None,
    "dry_run": dry_run == "1",
    "emit_mixpanel": emit_mixpanel == "1",
    "invoker_repo": invoker_repo,
    "invoker_branch": invoker_branch,
    "invoker_sha": invoker_sha,
    "workers": [
        {"name": parts[0], "target": parts[1], "port": int(parts[2])}
        for item in workers
        for parts in [item.split("\t", 2)]
        if len(parts) == 3
    ],
}
Path(out).write_text(json.dumps(payload, indent=2, sort_keys=True))
PY
}

write_failure_summary() {
  local exit_code="$1"
  local reason="$2"
  python3 - "$BATCH_DIR" "$BATCH_ID" "$INVOKER_SHA" "$exit_code" "$reason" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

batch_dir = Path(sys.argv[1])
batch_id, invoker_sha, exit_code, reason = sys.argv[2:]
summary = {
    "batch_id": batch_id,
    "invoker_sha": invoker_sha,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "job_count": 0,
    "status_counts": {"setup_failed": 1},
    "setup_status": "failed",
    "setup_exit_code": int(exit_code),
    "setup_failure": reason,
    "jobs": [],
}
(batch_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
(batch_dir / "summary.md").write_text(
    "\n".join([
        f"# Invoker benchmark batch {batch_id}",
        "",
        "- Setup status: `failed`",
        f"- Exit code: `{exit_code}`",
        f"- Reason: {reason}",
    ]) + "\n"
)
PY
}

on_batch_exit() {
  local code=$?
  if [[ "$code" -ne 0 && ! -f "$BATCH_DIR/summary.json" ]]; then
    write_batch_snapshot "failed" "failed" || true
    write_failure_summary "$code" "runner exited before summary generation; inspect run.log" || true
  fi
}
trap on_batch_exit EXIT

write_batch_snapshot "initializing" "running"
log "batch_dir=$BATCH_DIR"
log "config models=$MODELS modes=$MODES smoke=$SMOKE limit=${LIMIT:-none} job_set=${JOB_SET_FILE:-none} dry_run=$DRY_RUN emit_mixpanel=$EMIT_MIXPANEL serial_jobs=$BENCHMARK_SERIAL_JOBS"
log "config corpus_dir=$CORPUS_DIR manifest=$MANIFEST_FILE workers_file=$WORKERS_FILE invoker_repo=$INVOKER_REPO invoker_branch=$INVOKER_BRANCH invoker_sha=$INVOKER_SHA"

python3 - "$CORPUS_DIR" "$MANIFEST_FILE" "$JOB_SET_FILE" "$MODELS" "$MODES" "$SMOKE" "${LIMIT:-}" "$MATRIX_FILE" "$ASSIGNMENTS_FILE" "${WORKERS[@]}" <<'PY'
import glob
import json
import re
import sys
from pathlib import Path

corpus_dir = Path(sys.argv[1])
manifest_file = Path(sys.argv[2])
job_set_file = Path(sys.argv[3]) if sys.argv[3] else None
models = [x.strip() for x in sys.argv[4].split(",") if x.strip()]
modes = [x.strip() for x in sys.argv[5].split(",") if x.strip()]
smoke = sys.argv[6] == "1"
limit = int(sys.argv[7]) if sys.argv[7] else None
matrix_file = Path(sys.argv[8])
assignments_file = Path(sys.argv[9])
workers = [item.split("\t", 2) for item in sys.argv[10:]]

manifest = {}
if manifest_file.exists():
    manifest = json.loads(manifest_file.read_text())

source_manifest = {}
for source_manifest_path in (
    corpus_dir.with_suffix(".source-manifest.json"),
    corpus_dir.parent / f"{corpus_dir.name}.source-manifest.json",
):
    if source_manifest_path.exists():
        try:
            source_manifest = json.loads(source_manifest_path.read_text())
        except Exception:
            source_manifest = {}
        break
source_by_file = {
    str(item.get("file")): item
    for item in source_manifest.get("sessions", [])
    if isinstance(item, dict) and item.get("file")
}

def source_session_id(item, fallback):
    source_file = str(item.get("source_file") or "")
    match = re.search(r"(019[a-z0-9-]{32,})", source_file)
    if match:
        return match.group(1)
    return fallback

conversations = []
if "sessions" in manifest:
    for item in manifest["sessions"]:
        rel = item["file"] if isinstance(item, dict) else str(item)
        conversations.append(corpus_dir / rel)
else:
    globs = manifest.get("file_globs") or ["*.jsonl", "*.json", "*.md", "*.txt"]
    seen = set()
    for pattern in globs:
        for path in sorted(glob.glob(str(corpus_dir / pattern))):
            if path not in seen:
                seen.add(path)
                conversations.append(Path(path))

expected = manifest.get("expected_conversation_count")
if not conversations:
    raise SystemExit(f"No corpus conversations found in {corpus_dir}")
if expected and len(conversations) != int(expected):
    raise SystemExit(f"Expected {expected} corpus conversations, found {len(conversations)} in {corpus_dir}")

if smoke:
    conversations = conversations[:1]
    models = models[:1]

jobs = []
conversation_by_name = {conversation.name: conversation for conversation in conversations}
conversation_by_stem = {conversation.stem: conversation for conversation in conversations}

def resolve_conversation(value):
    raw = str(value or "").strip()
    if not raw:
        raise SystemExit("Job set entry is missing file")
    path = Path(raw)
    candidates = []
    if path.is_absolute():
        candidates.append(path)
    else:
        candidates.append(corpus_dir / path)
        candidates.append(conversation_by_name.get(raw))
        candidates.append(conversation_by_stem.get(raw))
    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate
    raise SystemExit(f"Job set entry references unknown corpus file: {raw}")

def add_job(conversation, model, mode, run_id="", session_id=""):
    model = str(model or "").strip()
    mode = str(mode or "").strip()
    if not model or not mode:
        raise SystemExit("Job set entries require model and mode")
    source_item = source_by_file.get(conversation.name, {})
    resolved_session_id = str(session_id or source_session_id(source_item, conversation.stem)).strip()
    resolved_run_id = str(run_id or f"{resolved_session_id}__{model}__{mode}").strip()
    jobs.append((resolved_run_id, str(conversation), resolved_session_id, model, mode))

if job_set_file:
    raw = job_set_file.read_text()
    if job_set_file.suffix.lower() == ".json":
        payload = json.loads(raw)
        entries = payload.get("jobs", payload) if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise SystemExit("JSON job set must be a list or object with a jobs list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise SystemExit("JSON job set entries must be objects")
            conversation = resolve_conversation(entry.get("file") or entry.get("conversation_file") or entry.get("conversation"))
            add_job(conversation, entry.get("model"), entry.get("mode"), entry.get("run_id", ""), entry.get("session_id", ""))
    else:
        for line_number, line in enumerate(raw.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) == 3:
                file_value, model, mode = parts
                run_id = ""
                session_id = ""
            elif len(parts) == 5:
                run_id, file_value, session_id, model, mode = parts
            else:
                raise SystemExit(f"Invalid TSV job set line {line_number}: expected 3 or 5 tab-separated fields")
            add_job(resolve_conversation(file_value), model, mode, run_id, session_id)
else:
    for conversation in conversations:
        source_item = source_by_file.get(conversation.name, {})
        session_id = source_session_id(source_item, conversation.stem)
        for model in models:
            for mode in modes:
                run_id = f"{session_id}__{model}__{mode}"
                jobs.append((run_id, str(conversation), session_id, model, mode))

if limit is not None and not job_set_file:
    jobs = jobs[:limit]

matrix_file.write_text("\n".join("\t".join(row) for row in jobs) + ("\n" if jobs else ""))

lines = []
for index, row in enumerate(jobs):
    worker = workers[index % len(workers)]
    lines.append("\t".join([worker[0], worker[1], worker[2], *row]))
assignments_file.write_text("\n".join(lines) + ("\n" if lines else ""))

print(f"conversation_count={len(conversations)}")
print(f"model_count={len(models)}")
print(f"mode_count={len(modes)}")
print(f"job_count={len(jobs)}")
print(f"worker_count={len(workers)}")
PY

write_batch_snapshot "matrix_generated" "running"
log "batch_id=$BATCH_ID invoker_sha=$INVOKER_SHA"
log "matrix=$(wc -l < "$MATRIX_FILE" | tr -d ' ') jobs assignments=$ASSIGNMENTS_FILE"

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "dry-run worker assignment plan:"
  column -t -s $'\t' "$ASSIGNMENTS_FILE" || cat "$ASSIGNMENTS_FILE"
  write_batch_snapshot "dry_run_complete" "succeeded"
  exit 0
fi

sync_runtime_to_worker() {
  local target="$1"
  local port="$2"
  ssh -p "$port" "$target" "mkdir -p '$BENCHMARK_ROOT/bin' '$BENCHMARK_ROOT/config' '$BENCHMARK_ROOT/lib' '$BENCHMARK_ROOT/corpus' '$BENCHMARK_ROOT/runs'"
  rsync -az -e "ssh -p $port" "$BENCHMARK_ROOT/bin/" "$target:$BENCHMARK_ROOT/bin/"
  rsync -az -e "ssh -p $port" "$BENCHMARK_ROOT/config/" "$target:$BENCHMARK_ROOT/config/"
  rsync -az -e "ssh -p $port" "$BENCHMARK_ROOT/lib/" "$target:$BENCHMARK_ROOT/lib/"
  local costing_source=""
  for candidate in "$BENCHMARK_ROOT/scripts/usage_costing.py" "$SCRIPT_DIR/../../scripts/usage_costing.py"; do
    if [[ -f "$candidate" ]]; then
      costing_source="$candidate"
      break
    fi
  done
  if [[ -n "$costing_source" ]]; then
    ssh -p "$port" "$target" "mkdir -p '$BENCHMARK_ROOT/scripts'"
    rsync -az -e "ssh -p $port" "$costing_source" "$target:$BENCHMARK_ROOT/scripts/usage_costing.py"
  fi
  rsync -az -e "ssh -p $port" "$CORPUS_DIR/" "$target:$CORPUS_DIR/"
  local source_manifest="${CORPUS_DIR}.source-manifest.json"
  if [[ -f "$source_manifest" ]]; then
    rsync -az -e "ssh -p $port" "$source_manifest" "$target:$source_manifest"
  fi
}

run_worker_queue() {
  local worker_name="$1"
  local target="$2"
  local port="$3"
  local queue_file="$4"
  local worker_log="$BATCH_DIR/${worker_name}.log"

  log "syncing runtime to $worker_name"
  sync_runtime_to_worker "$target" "$port" >>"$worker_log" 2>&1

  while IFS=$'\t' read -r run_id conversation_file session_id model mode; do
    [[ -n "$run_id" ]] || continue
    log "dispatch worker=$worker_name run_id=$run_id"
    printf '[%s] START worker=%s run_id=%s conversation=%s model=%s mode=%s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$worker_name" "$run_id" "$conversation_file" "$model" "$mode" >>"$BATCH_DIR/job-events.log"
    if ssh -n -p "$port" "$target" \
      "BENCHMARK_ROOT='$BENCHMARK_ROOT' BENCHMARK_ENV_FILE='$ENV_FILE' '$BENCHMARK_ROOT/bin/run-worker-job.sh' --batch-id '$BATCH_ID' --run-id '$run_id' --conversation-file '$conversation_file' --model '$model' --mode '$mode' --invoker-sha '$INVOKER_SHA'" \
      >>"$worker_log" 2>&1; then
      log "complete worker=$worker_name run_id=$run_id"
      printf '[%s] END worker=%s run_id=%s result=passed\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$worker_name" "$run_id" >>"$BATCH_DIR/job-events.log"
    else
      log "failed worker=$worker_name run_id=$run_id"
      printf '[%s] END worker=%s run_id=%s result=failed\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$worker_name" "$run_id" >>"$BATCH_DIR/job-events.log"
    fi
    mkdir -p "$BATCH_DIR/jobs/$run_id"
    rsync -az -e "ssh -p $port" "$target:$BENCHMARK_ROOT/runs/$BATCH_ID/jobs/$run_id/" "$BATCH_DIR/jobs/$run_id/" >>"$worker_log" 2>&1 || true
  done < "$queue_file"
}

QUEUE_DIR="$BATCH_DIR/queues"
mkdir -p "$QUEUE_DIR"
while IFS=$'\t' read -r worker_name target port run_id conversation_file session_id model mode; do
  printf '%s\t%s\t%s\t%s\t%s\n' "$run_id" "$conversation_file" "$session_id" "$model" "$mode" >>"$QUEUE_DIR/$worker_name.tsv"
done < "$ASSIGNMENTS_FILE"

status=0
if [[ "$BENCHMARK_SERIAL_JOBS" == "1" ]]; then
  synced_workers_file="$BATCH_DIR/synced-workers.txt"
  touch "$synced_workers_file"
  while IFS=$'\t' read -r worker_name target port run_id conversation_file session_id model mode; do
    [[ -n "$run_id" ]] || continue
    worker_log="$BATCH_DIR/${worker_name}.log"
    if ! grep -Fxq "$worker_name" "$synced_workers_file"; then
      log "syncing runtime to $worker_name"
      sync_runtime_to_worker "$target" "$port" >>"$worker_log" 2>&1
      printf '%s\n' "$worker_name" >>"$synced_workers_file"
    fi

    log "dispatch worker=$worker_name run_id=$run_id serial=1"
    printf '[%s] START worker=%s run_id=%s conversation=%s model=%s mode=%s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$worker_name" "$run_id" "$conversation_file" "$model" "$mode" >>"$BATCH_DIR/job-events.log"
    if ssh -n -p "$port" "$target" \
      "BENCHMARK_ROOT='$BENCHMARK_ROOT' BENCHMARK_ENV_FILE='$ENV_FILE' '$BENCHMARK_ROOT/bin/run-worker-job.sh' --batch-id '$BATCH_ID' --run-id '$run_id' --conversation-file '$conversation_file' --model '$model' --mode '$mode' --invoker-sha '$INVOKER_SHA'" \
      >>"$worker_log" 2>&1; then
      log "complete worker=$worker_name run_id=$run_id serial=1"
      printf '[%s] END worker=%s run_id=%s result=passed\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$worker_name" "$run_id" >>"$BATCH_DIR/job-events.log"
    else
      log "failed worker=$worker_name run_id=$run_id serial=1"
      printf '[%s] END worker=%s run_id=%s result=failed\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$worker_name" "$run_id" >>"$BATCH_DIR/job-events.log"
      status=1
    fi
    mkdir -p "$BATCH_DIR/jobs/$run_id"
    rsync -az -e "ssh -p $port" "$target:$BENCHMARK_ROOT/runs/$BATCH_ID/jobs/$run_id/" "$BATCH_DIR/jobs/$run_id/" >>"$worker_log" 2>&1 || true
    if [[ "$BENCHMARK_JOB_SETTLE_SECONDS" =~ ^[0-9]+$ && "$BENCHMARK_JOB_SETTLE_SECONDS" -gt 0 ]]; then
      log "settle worker=$worker_name run_id=$run_id seconds=$BENCHMARK_JOB_SETTLE_SECONDS"
      sleep "$BENCHMARK_JOB_SETTLE_SECONDS"
    fi
  done < "$ASSIGNMENTS_FILE"
else
  pids=()
  for worker in "${WORKERS[@]}"; do
    IFS=$'\t' read -r worker_name target port <<<"$worker"
    queue_file="$QUEUE_DIR/$worker_name.tsv"
    [[ -s "$queue_file" ]] || continue
    run_worker_queue "$worker_name" "$target" "$port" "$queue_file" &
    pids+=("$!")
  done

  for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
      status=1
    fi
  done
fi

write_batch_snapshot "workers_finished" "running"

python3 - "$BATCH_DIR" "$BATCH_ID" "$INVOKER_SHA" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

batch_dir = Path(sys.argv[1])
batch_id = sys.argv[2]
invoker_sha = sys.argv[3]
jobs = []
for path in sorted((batch_dir / "jobs").glob("*/job.json")):
    try:
        jobs.append(json.loads(path.read_text()))
    except json.JSONDecodeError:
        jobs.append({"run_id": path.parent.name, "status": "invalid_job_json"})

counts = {}
for job in jobs:
    counts[job.get("status", "unknown")] = counts.get(job.get("status", "unknown"), 0) + 1

summary = {
    "batch_id": batch_id,
    "invoker_sha": invoker_sha,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "job_count": len(jobs),
    "setup_status": "succeeded",
    "status_counts": counts,
    "jobs": jobs,
}
(batch_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))

lines = [
    f"# Invoker benchmark batch {batch_id}",
    "",
    f"- Invoker SHA: `{invoker_sha}`",
    f"- Jobs collected: {len(jobs)}",
]
for key in sorted(counts):
    lines.append(f"- {key}: {counts[key]}")
(batch_dir / "summary.md").write_text("\n".join(lines) + "\n")
PY

emit_args=("$BENCHMARK_ROOT/bin/emit-mixpanel-events.sh" --batch-dir "$BATCH_DIR")
if [[ "$EMIT_MIXPANEL" -eq 1 ]]; then
  emit_args+=(--emit)
fi
"${emit_args[@]}"

write_batch_snapshot "complete" "$([[ "$status" -eq 0 ]] && echo succeeded || echo failed)"
log "summary=$BATCH_DIR/summary.md"
exit "$status"
