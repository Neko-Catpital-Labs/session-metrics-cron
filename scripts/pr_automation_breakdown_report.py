#!/usr/bin/env python3
"""Build a PR automation cost breakdown from planning-vs-execution prompt rows."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]

AUTOMATED_REQUEST_ORIGINS = {
    "generated_invoker_task",
    "invoker_auto_fix",
    "invoker_task_failure_fix",
    "merge_failure_fix",
    "ci_failure_fix",
}
AUTOMATED_TASK_TYPES = {
    "workflow_repair",
}
AUTOMATED_TEXT_MARKERS = (
    "generated task for invoker",
    "invoker generated task",
    "autofix for invoker",
    "invoker autofix",
    "fix invoker task failure",
    "invoker task failed",
    "fix with agent",
)
DURATION_FIELDS = (
    "raw_seconds",
    "duration_seconds",
    "active_seconds",
    "elapsed_seconds",
    "wall_seconds",
    "seconds",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PR automation retry-thrash breakdown.")
    parser.add_argument("--input", default=str(REPO_ROOT / "reports" / "planning-vs-execution-prompts.csv"))
    parser.add_argument("--json-out", default=str(REPO_ROOT / "reports" / "pr-automation-breakdown.json"))
    parser.add_argument("--markdown-out", default=str(REPO_ROOT / "reports" / "pr-automation-breakdown.md"))
    parser.add_argument("--html-out", default=str(REPO_ROOT / "reports" / "pr-automation-breakdown.html"))
    parser.add_argument("--top-thrash", type=int, default=20)
    parser.add_argument("--duration-cap-seconds", type=float, default=3600.0)
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def prompt_cost(row: dict[str, str]) -> float:
    return (
        to_float(row.get("derived_total_cost_usd"))
        or to_float(row.get("estimated_cost_usd"))
        or to_float(row.get("allocated_total_cost_usd"))
    )


def row_duration_seconds(row: dict[str, str]) -> float:
    for field in DURATION_FIELDS:
        if row.get(field) not in (None, ""):
            return to_float(row.get(field))
    if row.get("duration_ms") not in (None, ""):
        return to_float(row.get("duration_ms")) / 1000.0
    return 0.0


def compact(text: Any, limit: int = 120) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


def money(value: float) -> str:
    return f"${value:,.2f}"


def pct(value: float, total: float) -> str:
    return f"{(value / total * 100.0) if total else 0.0:.1f}%"


def row_text(row: dict[str, str]) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in (
            "repo",
            "repository",
            "session_cwd",
            "prompt_preview",
            "previous_prompt_preview",
            "first_prompt_preview",
            "final_answer_preview",
            "request_pattern",
            "task_type",
            "task_type_label",
            "request_origin",
            "primary_why",
            "work_motivation",
            "prompt_task_kind",
        )
    )


def is_automated_row(row: dict[str, str]) -> bool:
    request_origin = str(row.get("request_origin") or row.get("primary_why") or "").strip()
    task_type = str(row.get("task_type") or "").strip()
    if request_origin in AUTOMATED_REQUEST_ORIGINS or task_type in AUTOMATED_TASK_TYPES:
        return True
    lowered = row_text(row).lower()
    return any(marker in lowered for marker in AUTOMATED_TEXT_MARKERS)


def infer_repo(row: dict[str, str], text: str) -> str:
    for key in ("repo", "repository"):
        value = str(row.get(key) or "").strip()
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
            return value
    match = re.search(r"github\.com[:/]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    cwd = str(row.get("session_cwd") or "").rstrip("/")
    if cwd:
        return Path(cwd).name or "unknown"
    return "unknown"


def extract_repo_and_pr(row: dict[str, str]) -> tuple[str, int] | None:
    text = row_text(row)
    url_match = re.search(
        r"github\.com[:/]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(?:pull|pulls|issues)/(\d+)",
        text,
        re.IGNORECASE,
    )
    if url_match:
        return url_match.group(1), int(url_match.group(2))

    repo_pr_match = re.search(
        r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s*(?:#|(?:pull request|pr)\s*#?)\s*(\d+)\b",
        text,
        re.IGNORECASE,
    )
    if repo_pr_match:
        return repo_pr_match.group(1), int(repo_pr_match.group(2))

    pr_match = re.search(r"\b(?:pull request|pr)\s*#?\s*(\d+)\b", text, re.IGNORECASE)
    if pr_match:
        return infer_repo(row, text), int(pr_match.group(1))

    return None


def category_for_row(row: dict[str, str], hit: tuple[str, int] | None) -> tuple[str, str]:
    if is_automated_row(row) and hit:
        return "automated_pr_repair", "Automated PR repair"
    if is_automated_row(row):
        return "automated_other", "Automated other"
    return "other", "Other"


def add_category_metrics(
    categories: dict[str, dict[str, Any]],
    category: str,
    label: str,
    *,
    cost: float,
    active_seconds: float,
    raw_seconds: float,
) -> None:
    bucket = categories.setdefault(
        category,
        {
            "category": category,
            "label": label,
            "cost_usd": 0.0,
            "active_seconds": 0.0,
            "prompts": 0,
            "raw_seconds": 0.0,
        },
    )
    bucket["cost_usd"] += cost
    bucket["active_seconds"] += active_seconds
    bucket["prompts"] += 1
    bucket["raw_seconds"] += raw_seconds


def rounded_category(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["cost_usd"] = round(to_float(out["cost_usd"]), 4)
    out["active_seconds"] = round(to_float(out["active_seconds"]), 4)
    out["raw_seconds"] = round(to_float(out["raw_seconds"]), 4)
    return out


def build_attempts_detail(sessions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for session_id, session in sessions.items():
        rows.append(
            {
                "session_id": session_id,
                "prompt_index": session["prompt_index"],
                "cost_usd": round(to_float(session["cost_usd"]), 4),
                "session_date": session["session_date"],
                "host": session["host"],
            }
        )
    return sorted(rows, key=lambda item: (-item["cost_usd"], item["session_id"], str(item["prompt_index"])))


def build_window(rows: list[dict[str, str]], *, duration_cap_seconds: float = 3600.0) -> dict[str, Any]:
    total_cost = 0.0
    categories: dict[str, dict[str, Any]] = {}
    thrash_groups: dict[tuple[str, int], dict[str, Any]] = {}

    for row in rows:
        cost = prompt_cost(row)
        raw_seconds = row_duration_seconds(row)
        active_seconds = min(raw_seconds, duration_cap_seconds) if duration_cap_seconds and raw_seconds else raw_seconds
        total_cost += cost

        hit = extract_repo_and_pr(row)
        category, label = category_for_row(row, hit)
        add_category_metrics(
            categories,
            category,
            label,
            cost=cost,
            active_seconds=active_seconds,
            raw_seconds=raw_seconds,
        )
        if not hit or not is_automated_row(row):
            continue

        group = thrash_groups.setdefault(hit, {"cost_usd": 0.0, "sessions": {}})
        group["cost_usd"] += cost
        file_path = str(row.get("file") or "")
        session_id = Path(file_path).stem or file_path
        if not session_id:
            session_id = "unknown"
        sessions: dict[str, dict[str, Any]] = group["sessions"]
        session = sessions.setdefault(
            session_id,
            {
                "cost_usd": 0.0,
                "session_date": str(row.get("session_date") or ""),
                "host": str(row.get("host") or ""),
                "prompt_index": str(row.get("prompt_index") or ""),
                "representative_cost_usd": -1.0,
            },
        )
        session["cost_usd"] += cost
        if cost > to_float(session.get("representative_cost_usd")):
            session["representative_cost_usd"] = cost
            session["prompt_index"] = str(row.get("prompt_index") or "")

    thrash_rows: list[dict[str, Any]] = []
    for (repo, pr_number), group in thrash_groups.items():
        attempts_detail = build_attempts_detail(group["sessions"])
        thrash_rows.append(
            {
                "repo": repo,
                "pr_number": pr_number,
                "target": f"{repo}#{pr_number}",
                "attempts": len(attempts_detail),
                "cost_usd": round(to_float(group["cost_usd"]), 4),
                "attempts_detail": attempts_detail,
            }
        )
    thrash_rows.sort(key=lambda item: (-item["cost_usd"], -item["attempts"], item["repo"], item["pr_number"]))
    category_rows = sorted(
        (rounded_category(row) for row in categories.values()),
        key=lambda item: (-item["cost_usd"], item["category"]),
    )
    return {
        "total_cost_usd": round(total_cost, 4),
        "categories": category_rows,
        "thrash_targets": thrash_rows,
        "thrash_cost_usd": round(sum(row["cost_usd"] for row in thrash_rows if row["attempts"] > 1), 4),
        "normal_repair_cost_usd": round(sum(row["cost_usd"] for row in thrash_rows if row["attempts"] <= 1), 4),
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    rows = read_csv(Path(args.input))
    window = build_window(rows, duration_cap_seconds=args.duration_cap_seconds)
    top = window["thrash_targets"][: args.top_thrash]
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input": str(Path(args.input)),
        "total_cost_usd": window["total_cost_usd"],
        "categories": window["categories"],
        "thrash": {
            "top": top,
            "thrash_cost_usd": window["thrash_cost_usd"],
            "normal_repair_cost_usd": window["normal_repair_cost_usd"],
        },
        "thrash_targets": window["thrash_targets"],
    }


def render_markdown(data: dict[str, Any], args: argparse.Namespace) -> str:
    lines = [
        "# PR Automation Breakdown",
        "",
        f"- Total cost: **{money(data['total_cost_usd'])}**",
        f"- Thrash cost: **{money(data['thrash']['thrash_cost_usd'])}**",
        f"- Normal repair cost: **{money(data['thrash']['normal_repair_cost_usd'])}**",
        "",
        "## Categories",
        "",
        "| Category | Prompts | Cost | Active seconds | Raw seconds |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in data["categories"]:
        lines.append(
            f"| {row['label']} | {row['prompts']:,} | {money(row['cost_usd'])} | "
            f"{row['active_seconds']:,.1f} | {row['raw_seconds']:,.1f} |"
        )
    lines.extend(
        [
            "",
            f"## Top PR Retry Targets (top {args.top_thrash})",
            "",
            "| Target | Attempts | Cost | Share |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in data["thrash"]["top"]:
        lines.append(
            f"| `{row['target']}` | {row['attempts']:,} | {money(row['cost_usd'])} | "
            f"{pct(row['cost_usd'], data['total_cost_usd'])} |"
        )
    lines.append("")
    return "\n".join(lines)


PAGE_CSS = """
:root { --fg:#1b1f24; --sub:#57606a; --line:#e2e6ea; --bg:#f6f8fa; --panel:#fff; --link:#2563eb; --bad:#b91c1c; --tag:#eef2ff; --selected:#eff6ff; --selected-line:#2563eb; --highlight:#fff7cc; --seg-automated:#2563eb; --seg-other:#cbd5e1; }
* { box-sizing:border-box; }
html, body { overflow-y:auto; }
body { font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,sans-serif; color:var(--fg); margin:0; padding:1.25rem; max-width:1480px; margin:0 auto; background:#fff; }
a { color:var(--link); text-decoration:none; }
a:hover { text-decoration:underline; }
h1 { font-size:1.55rem; margin:0 0 .15rem; }
h2, h3, h4 { margin:.2rem 0 .6rem; }
.sub, .muted { color:var(--sub); }
.nav { display:flex; gap:1rem; flex-wrap:wrap; margin-bottom:.65rem; font-size:.92rem; }
#err { color:var(--bad); margin:.8rem 0; }
.controls-card { display:flex; flex-wrap:wrap; gap:1rem 1.2rem; align-items:flex-end; padding:.9rem; background:var(--bg); border:1px solid var(--line); border-radius:10px; margin-bottom:1rem; }
.group { display:flex; flex-direction:column; gap:.35rem; min-width:0; }
.lbl { font-size:.72rem; text-transform:uppercase; letter-spacing:.03em; color:var(--sub); }
.select, .search, .num-input, .date-input { border:1px solid var(--line); border-radius:8px; background:#fff; padding:.42rem .55rem; font-size:.88rem; min-height:36px; }
.search { min-width:220px; }
.num-input { width:90px; }
button.btn { border:1px solid var(--line); background:#fff; padding:.42rem .75rem; border-radius:8px; cursor:pointer; font-size:.85rem; }
button.btn:hover { background:var(--bg); }
label.checkbox { display:flex; align-items:center; gap:.4rem; font-size:.85rem; }
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:.75rem; margin:1rem 0; }
.kpi { background:var(--bg); border:1px solid var(--line); border-radius:10px; padding:.75rem .9rem; }
.kpi .v { font-size:1.35rem; font-weight:700; }
.kpi .l { color:var(--sub); font-size:.72rem; text-transform:uppercase; letter-spacing:.03em; }
.charts-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(360px,1fr)); gap:1rem; margin:1rem 0; }
.chart-card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:.85rem .95rem; }
.chart-card h3 { font-size:.95rem; }
.chart-card canvas { max-height:260px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; box-shadow:0 1px 2px rgba(16,24,40,.05); margin-bottom:1rem; }
.panel-head { padding:.85rem .95rem; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap; }
.panel-body { padding:.85rem .95rem; }
table { width:100%; border-collapse:collapse; font-size:.86rem; }
th, td { padding:.42rem .5rem; border-bottom:1px solid #eef1f4; text-align:left; vertical-align:top; }
th.n, td.n { text-align:right; font-variant-numeric:tabular-nums; }
th.sortable { cursor:pointer; user-select:none; white-space:nowrap; }
th.sortable .sort-indicator { display:inline-block; width:.9em; color:var(--sub); }
th.sortable.is-sorted .sort-indicator { color:var(--fg); }
tr.target-row { cursor:pointer; }
tr.target-row:hover { background:#f8fbff; }
tr.target-row.is-open { background:var(--selected); box-shadow: inset 3px 0 0 var(--selected-line); }
tr.attempts-row > td { background:var(--bg); padding:.6rem .8rem 1rem; border-bottom:1px solid var(--line); }
.attempt-row { display:flex; justify-content:space-between; gap:.75rem; align-items:center; padding:.5rem .6rem; border:1px solid var(--line); border-radius:8px; background:#fff; margin-bottom:.4rem; cursor:pointer; }
.attempt-row:hover { background:#f8fbff; }
.attempt-row.is-selected { background:var(--selected); border-color:var(--selected-line); box-shadow: inset 3px 0 0 var(--selected-line); }
.attempt-row.is-unavailable { cursor:not-allowed; opacity:.6; }
.attempt-meta { color:var(--sub); font-size:.8rem; display:flex; gap:.6rem; flex-wrap:wrap; }
.attempt-money { font-weight:600; }
.pill-bar { display:flex; width:100%; min-width:80px; height:.55rem; border-radius:999px; overflow:hidden; background:var(--seg-other); }
.pill-bar .seg-automated { background:var(--seg-automated); height:100%; }
.pill-bar .seg-other { background:var(--seg-other); height:100%; }
.empty { border:1px dashed var(--line); border-radius:10px; padding:1rem; color:var(--sub); background:var(--bg); }
.chunk-card { border:1px solid var(--line); border-radius:12px; background:var(--bg); margin-bottom:.7rem; }
.chunk-card.is-active { border-color:var(--selected-line); box-shadow: inset 3px 0 0 var(--selected-line); background:var(--selected); }
.chunk-summary { padding:.7rem .8rem; cursor:pointer; }
.chunk-summary:hover { background:rgba(37,99,235,.03); }
.chunk-title-row { display:flex; align-items:flex-start; justify-content:space-between; gap:.75rem; }
.chunk-title { font-weight:600; margin-bottom:.2rem; }
.chunk-toggle { color:var(--link); font-size:.8rem; white-space:nowrap; }
.chunk-cost { font-weight:600; margin:.3rem 0 .15rem; }
.chunk-meta { color:var(--sub); font-size:.78rem; display:flex; flex-wrap:wrap; gap:.45rem; margin:.25rem 0 .45rem; }
.chunk-preview { color:var(--sub); font-size:.82rem; white-space:pre-wrap; word-break:break-word; margin-top:.35rem; }
.chunk-body { border-top:1px solid var(--line); padding:.75rem .8rem .8rem; background:#fff; }
.chunk-body[hidden] { display:none; }
.conversation-transcript { margin:.5rem 0 0; border:1px solid #eef1f4; border-radius:8px; background:var(--bg); padding:.7rem .75rem; white-space:pre-wrap; word-break:break-word; font-size:.82rem; }
.table-wrap { overflow-x:auto; }
#commandTable tbody tr.is-highlighted { background:var(--highlight); }
#commandTable td.preview { min-width:320px; white-space:normal; word-break:break-word; }
.detail-empty { border:1px dashed var(--line); border-radius:10px; padding:1rem; color:var(--sub); background:var(--bg); }
.detail-head { display:flex; justify-content:space-between; gap:1rem; align-items:flex-start; }
.detail-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:.65rem; margin:.8rem 0; }
.detail-card { background:var(--bg); border:1px solid var(--line); border-radius:10px; padding:.7rem .8rem; }
.detail-card .l { color:var(--sub); font-size:.72rem; text-transform:uppercase; letter-spacing:.03em; }
@media (max-width:960px){
  body { padding:1rem; }
  .search { width:100%; min-width:0; }
}
"""

PAGE_JS = """
const state = {
  from: "",
  to: "",
  host: "",
  repo: "",
  minAttempts: 1,
  thrashOnly: false,
  search: "",
  expandedTarget: null,
  selectedAttempt: null,
  selectedStepIndex: -1,
  categorySort: { key: "cost_usd", dir: "desc" },
  thrashSort: { key: "cost_usd", dir: "desc" },
};

let REPORT = null;
const chartInstances = {};
const sessionDetailCache = {};

const CATEGORY_ACCESSORS = {
  label: row => String(row.label || ""),
  prompts: row => Number(row.prompts || 0),
  cost_usd: row => Number(row.cost_usd || 0),
  active_seconds: row => Number(row.active_seconds || 0),
  raw_seconds: row => Number(row.raw_seconds || 0),
};

const TARGET_ACCESSORS = {
  target: row => String(row.target || ""),
  attempts: row => Number(row.attempts || 0),
  cost_usd: row => Number(row.cost_usd || 0),
  avg_cost: row => Number(row.attempts || 0) > 0 ? Number(row.cost_usd || 0) / Number(row.attempts) : 0,
  share_pct: row => REPORT && REPORT.total_cost_usd ? Number(row.cost_usd || 0) / REPORT.total_cost_usd : 0,
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"})[ch]);
}
function money(value) { return "$" + Number(value || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function count(value) { return Number(value || 0).toLocaleString(); }
function pctText(value) { return (Number(value || 0) * 100).toFixed(1) + "%"; }
function hoursText(seconds) { return (Number(seconds || 0) / 3600).toLocaleString(undefined, { maximumFractionDigits: 1 }) + "h"; }
function compactText(value, limit = 180) {
  let text = String(value || "").replace(/\\s+/g, " ").trim();
  if (limit > 0 && text.length > limit) text = text.slice(0, limit - 3).trimEnd() + "...";
  return text;
}

function readBootstrap() {
  const node = document.getElementById("prAutomationData");
  if (!node) throw new Error("Missing embedded report data.");
  return JSON.parse(node.textContent);
}

function isLocalFileMode() {
  return location.protocol === "file:";
}

async function init() {
  try {
    let data = readBootstrap();
    if (location.protocol.startsWith("http")) {
      try {
        const res = await fetch("/api/pr-automation-breakdown");
        if (res.ok) data = await res.json();
      } catch (fetchError) {
        // Keep the embedded bootstrap data when the live endpoint is unavailable.
      }
    }
    REPORT = data;
    bindControls();
    render();
  } catch (error) {
    const err = document.getElementById("err");
    if (err) err.textContent = "Failed to load dashboard data: " + (error && error.message ? error.message : error);
  }
}

function rangeBounds() {
  return [state.from || "", state.to || ""];
}

function factPasses(row, lo, hi) {
  const date = row.session_date;
  if (date !== undefined && date !== null && date !== "") {
    if (lo && date < lo) return false;
    if (hi && date > hi) return false;
  }
  const host = row.host;
  if (state.host && host !== undefined && host !== null && host !== "" && host !== state.host) return false;
  return true;
}

function attemptPasses(a, lo, hi) {
  const date = a.session_date || "";
  if (lo && date && date < lo) return false;
  if (hi && date && date > hi) return false;
  if (state.host && String(a.host || "") !== state.host) return false;
  return true;
}

function aggregateCategories(facts) {
  return facts.slice().sort((a, b) => b.cost_usd - a.cost_usd || String(a.category).localeCompare(String(b.category)));
}

function aggregateDaily(facts, dim) {
  const byDate = new Map();
  const dims = new Set();
  for (const row of facts) {
    const date = row.session_date || "unknown";
    const key = dim(row) || "unknown";
    dims.add(key);
    if (!byDate.has(date)) byDate.set(date, new Map());
    const seg = byDate.get(date);
    seg.set(key, (seg.get(key) || 0) + Number(row.cost_usd || 0));
  }
  const dates = [...byDate.keys()].sort();
  const dimList = [...dims].sort();
  return {
    dates,
    dims: dimList,
    series: dimList.map(key => dates.map(date => (byDate.get(date) || new Map()).get(key) || 0)),
  };
}

function aggregateTargets(lo, hi) {
  const repoFilter = (state.repo || "").trim().toLowerCase();
  const searchFilter = (state.search || "").trim().toLowerCase();
  const minAttempts = Number(state.minAttempts) || 0;
  return (REPORT.thrash_targets || [])
    .map(target => {
      const attempts = (target.attempts_detail || []).filter(a => attemptPasses(a, lo, hi));
      const cost = attempts.reduce((sum, a) => sum + Number(a.cost_usd || 0), 0);
      return Object.assign({}, target, {
        attempts_detail: attempts,
        attempts: attempts.length,
        cost_usd: Number(cost.toFixed(4)),
        thrash: attempts.length > 1,
      });
    })
    .filter(target => {
      if (repoFilter && !String(target.repo || "").toLowerCase().includes(repoFilter)) return false;
      if (searchFilter) {
        const haystack = `${target.repo} ${target.target}`.toLowerCase();
        if (!haystack.includes(searchFilter)) return false;
      }
      if (target.attempts < minAttempts) return false;
      if (state.thrashOnly && !target.thrash) return false;
      return true;
    });
}

function kpiTotals(facts, targets) {
  const totalCost = facts.reduce((sum, row) => sum + Number(row.cost_usd || 0), 0);
  const automatedCost = facts
    .filter(row => row.category === "automated_pr_repair" || row.category === "automated_other")
    .reduce((sum, row) => sum + Number(row.cost_usd || 0), 0);
  const automatedPct = totalCost ? automatedCost / totalCost : 0;
  const activeSeconds = facts.reduce((sum, row) => sum + Number(row.active_seconds || 0), 0);
  const promptCount = facts.reduce((sum, row) => sum + Number(row.prompts || 0), 0);
  const thrashTargets = targets.filter(target => target.thrash);
  const thrashCost = thrashTargets.reduce((sum, target) => sum + Number(target.cost_usd || 0), 0);
  return {
    totalCost,
    automatedCost,
    automatedPct,
    activeSeconds,
    promptCount,
    thrashCost,
    repeatedCount: thrashTargets.length,
  };
}

function sortRows(rows, sortState, accessors) {
  const accessor = accessors[sortState.key] || accessors.cost_usd;
  const dir = sortState.dir === "asc" ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const av = accessor(a);
    const bv = accessor(b);
    if (av < bv) return -1 * dir;
    if (av > bv) return 1 * dir;
    return Math.abs(Number(b.cost_usd || 0)) - Math.abs(Number(a.cost_usd || 0));
  });
}

function bindSortHeaders(tableId, sortState, rerenderFn) {
  const table = document.getElementById(tableId);
  if (!table) return;
  table.querySelectorAll("th.sortable").forEach(th => {
    th.addEventListener("click", () => {
      const key = th.dataset.sortKey;
      if (!key) return;
      if (sortState.key === key) {
        sortState.dir = sortState.dir === "asc" ? "desc" : "asc";
      } else {
        sortState.key = key;
        sortState.dir = "desc";
      }
      rerenderFn();
    });
  });
}

function sortIndicatorHtml(sortState, key) {
  if (sortState.key !== key) return '<span class="sort-indicator">↕</span>';
  return `<span class="sort-indicator">${sortState.dir === "asc" ? "↑" : "↓"}</span>`;
}

function destroyChart(id) {
  if (chartInstances[id]) {
    chartInstances[id].destroy();
    delete chartInstances[id];
  }
}

function chartOrNull(id, config) {
  if (typeof Chart === "undefined") return null;
  const canvas = document.getElementById(id);
  if (!canvas) return null;
  destroyChart(id);
  const chart = new Chart(canvas.getContext("2d"), config);
  chartInstances[id] = chart;
  return chart;
}

const CHART_COLORS = ["#2563eb", "#f97316", "#16a34a", "#9333ea", "#0891b2", "#dc2626", "#64748b"];

function stackedDailyChart(agg) {
  if (!agg.dates.length) {
    destroyChart("dailyChart");
    return null;
  }
  return chartOrNull("dailyChart", {
    type: "bar",
    data: {
      labels: agg.dates,
      datasets: agg.dims.map((dim, index) => ({
        label: dim,
        data: agg.series[index],
        backgroundColor: CHART_COLORS[index % CHART_COLORS.length],
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: { x: { stacked: true }, y: { stacked: true } },
    },
  });
}

function categorySplitChart(rows) {
  if (!rows.length) {
    destroyChart("categoryChart");
    return null;
  }
  return chartOrNull("categoryChart", {
    type: "bar",
    data: {
      labels: rows.map(row => row.label),
      datasets: [{
        label: "Cost",
        data: rows.map(row => row.cost_usd),
        backgroundColor: rows.map(row => (row.category === "other" ? "#cbd5e1" : "#2563eb")),
      }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
    },
  });
}

function thrashChart(targets) {
  const top = targets.slice().sort((a, b) => b.cost_usd - a.cost_usd).slice(0, 10);
  if (!top.length) {
    destroyChart("thrashChart");
    return null;
  }
  return chartOrNull("thrashChart", {
    type: "bar",
    data: {
      labels: top.map(row => row.target),
      datasets: [{
        label: "Cost",
        data: top.map(row => row.cost_usd),
        backgroundColor: "#2563eb",
      }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
    },
  });
}

function renderKpis(totals) {
  const root = document.getElementById("kpis");
  if (!root) return;
  root.innerHTML = [
    [money(totals.totalCost), "Total cost"],
    [money(totals.automatedCost) + ` (${pctText(totals.automatedPct)})`, "Automated cost"],
    [hoursText(totals.activeSeconds), "Active hours"],
    [count(totals.promptCount), "Prompts"],
    [money(totals.thrashCost), "Thrash cost"],
    [count(totals.repeatedCount), "Repeated targets"],
  ].map(([value, label]) => `<div class="kpi"><div class="v">${value}</div><div class="l">${escapeHtml(label)}</div></div>`).join("");
}

function renderCharts(facts, targets) {
  const flattenedAttempts = targets.flatMap(target =>
    (target.attempts_detail || []).map(a => Object.assign({}, a, { repo: target.repo, pr_number: target.pr_number }))
  );
  stackedDailyChart(aggregateDaily(flattenedAttempts, row => row.host || "unknown"));
  categorySplitChart(aggregateCategories(facts));
  thrashChart(targets);
}

function pillBarHtml(row, totalCost) {
  const isAutomated = row.category !== "other";
  const share = totalCost ? Number(row.cost_usd || 0) / totalCost : 0;
  const pctValue = Math.max(0, Math.min(100, share * 100));
  const autoWidth = isAutomated ? pctValue : 0;
  const otherWidth = isAutomated ? 0 : pctValue;
  return `<div class="pill-bar" title="${pctText(share)} of total cost"><span class="seg-automated" style="width:${autoWidth}%"></span><span class="seg-other" style="width:${otherWidth}%"></span></div>`;
}

function renderCategoryTable(rows) {
  const body = document.getElementById("categoryTableBody");
  if (!body) return;
  const totalCost = REPORT ? Number(REPORT.total_cost_usd || 0) : 0;
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">No categories match the current filters.</td></tr>';
    return;
  }
  body.innerHTML = rows.map(row => `<tr>
    <td>${escapeHtml(row.label)}</td>
    <td class="n">${count(row.prompts)}</td>
    <td class="n">${money(row.cost_usd)}</td>
    <td class="n">${hoursText(row.active_seconds)}</td>
    <td class="n">${hoursText(row.raw_seconds)}</td>
    <td>${pillBarHtml(row, totalCost)}</td>
  </tr>`).join("");
}

function targetKey(target) {
  return `${target.repo}#${target.pr_number}`;
}

function attemptKey(attempt) {
  return `${attempt.session_id}::${attempt.prompt_index}`;
}

function renderAttempts(target) {
  const attempts = target.attempts_detail || [];
  if (!attempts.length) {
    return '<div class="empty">No attempts match the current filters for this target.</div>';
  }
  return attempts.map(attempt => {
    const selected = state.selectedAttempt && attemptKey(state.selectedAttempt) === attemptKey(attempt);
    const unavailable = isLocalFileMode();
    const classes = ["attempt-row"];
    if (selected) classes.push("is-selected");
    if (unavailable) classes.push("is-unavailable");
    return `<div class="${classes.join(" ")}" data-session-id="${escapeHtml(attempt.session_id)}" data-prompt-index="${escapeHtml(attempt.prompt_index)}">
      <div class="attempt-meta">
        <span>${escapeHtml(attempt.session_date || "")}</span>
        <span>${escapeHtml(attempt.host || "")}</span>
        <span>${escapeHtml(attempt.session_id)} · prompt ${escapeHtml(attempt.prompt_index)}</span>
      </div>
      <div class="attempt-money">${money(attempt.cost_usd)}</div>
    </div>`;
  }).join("");
}

function renderThrashTable(targets) {
  const body = document.getElementById("thrashTableBody");
  if (!body) return;
  if (!targets.length) {
    body.innerHTML = `<tr><td colspan="5"><div class="empty">No thrash targets match the current filters. <button class="btn" id="clearFiltersBtn" type="button">Clear filters</button></div></td></tr>`;
    return;
  }
  const totalCost = REPORT ? Number(REPORT.total_cost_usd || 0) : 0;
  const rows = [];
  for (const target of targets) {
    const key = targetKey(target);
    const isOpen = state.expandedTarget === key;
    rows.push(`<tr class="target-row${isOpen ? " is-open" : ""}" data-target-key="${escapeHtml(key)}">
      <td>${escapeHtml(target.target)}</td>
      <td class="n">${count(target.attempts)}</td>
      <td class="n">${money(target.cost_usd)}</td>
      <td class="n">${money(target.attempts ? target.cost_usd / target.attempts : 0)}</td>
      <td class="n">${pctText(totalCost ? target.cost_usd / totalCost : 0)}</td>
    </tr>`);
    if (isOpen) {
      rows.push(`<tr class="attempts-row" data-target-key="${escapeHtml(key)}"><td colspan="5">${renderAttempts(target)}</td></tr>`);
    }
  }
  body.innerHTML = rows.join("");
}

function render() {
  if (!REPORT) return;
  const [lo, hi] = rangeBounds();
  const facts = (REPORT.categories || []).filter(row => factPasses(row, lo, hi));
  const targets = aggregateTargets(lo, hi);
  renderKpis(kpiTotals(facts, targets));
  renderCharts(facts, targets);
  renderCategoryTable(sortRows(aggregateCategories(facts), state.categorySort, CATEGORY_ACCESSORS));
  renderThrashTable(sortRows(targets, state.thrashSort, TARGET_ACCESSORS));
  const categoryHead = document.getElementById("categoryTableHead");
  if (categoryHead) {
    categoryHead.querySelectorAll("th.sortable").forEach(th => {
      th.classList.toggle("is-sorted", th.dataset.sortKey === state.categorySort.key);
      const indicator = th.querySelector(".sort-indicator");
      if (indicator) indicator.outerHTML = sortIndicatorHtml(state.categorySort, th.dataset.sortKey);
    });
  }
  const thrashHead = document.getElementById("thrashTableHead");
  if (thrashHead) {
    thrashHead.querySelectorAll("th.sortable").forEach(th => {
      th.classList.toggle("is-sorted", th.dataset.sortKey === state.thrashSort.key);
      const indicator = th.querySelector(".sort-indicator");
      if (indicator) indicator.outerHTML = sortIndicatorHtml(state.thrashSort, th.dataset.sortKey);
    });
  }
}

function findTarget(key) {
  const [lo, hi] = rangeBounds();
  return aggregateTargets(lo, hi).find(target => targetKey(target) === key) || null;
}

function summarizeStep(chunk) {
  const entries = chunk.conversation_entries || [];
  const userEntry = entries.find(entry => entry.role_label === "User" && compactText(entry.text, 0));
  const preview = compactText(userEntry ? userEntry.text : (chunk.message_preview || ""), 220);
  return preview || "No preview available for this step.";
}

function stepCommandRows(payload, chunk) {
  const start = Number(chunk.start_command_index);
  const end = Number(chunk.end_command_index);
  return (payload.commands || []).filter(row => {
    const index = Number(row.command_index);
    return index >= start && index <= end;
  });
}

function renderStepBody(payload, chunk) {
  const rows = stepCommandRows(payload, chunk);
  const totals = rows.reduce((acc, row) => {
    acc.cost += Number(row.cost_usd || 0);
    acc.context += Number(row.headline_context_cost_usd || 0);
    acc.cacheRead += Number(row.headline_cache_read_cost_usd || 0);
    acc.output += Number(row.headline_output_cost_usd || 0);
    return acc;
  }, { cost: 0, context: 0, cacheRead: 0, output: 0 });
  const entries = chunk.conversation_entries || [];
  const transcript = entries.length
    ? entries.map(entry => `${entry.role_label || "Meta"}${entry.tool_name ? " · " + entry.tool_name : ""}\\n${entry.text || ""}`).join("\\n\\n")
    : "";
  return `<div class="table-wrap"><table><thead><tr><th>Metric</th><th class="n">Value</th></tr></thead><tbody>
      <tr><td>Total cost</td><td class="n">${money(totals.cost)}</td></tr>
      <tr><td>Context</td><td class="n">${money(totals.context)}</td></tr>
      <tr><td>Cache read</td><td class="n">${money(totals.cacheRead)}</td></tr>
      <tr><td>Output</td><td class="n">${money(totals.output)}</td></tr>
    </tbody></table></div>
    ${transcript ? `<pre class="conversation-transcript">${escapeHtml(transcript)}</pre>` : ""}`;
}

function renderSessionDetail(attempt) {
  const key = attemptKey(attempt);
  const entry = sessionDetailCache[key];
  const root = document.getElementById("sessionDetail");
  if (!root) return;
  if (!entry) return;
  if (entry.error) {
    root.innerHTML = `<div class="detail-empty">${escapeHtml(entry.error)}</div>`;
    return;
  }
  const payload = entry.payload;
  const timeline = payload.timeline || [];
  const commands = payload.commands || [];
  const activeChunk = timeline[state.selectedStepIndex] || null;
  root.innerHTML = `
    <div class="detail-head">
      <div>
        <h3>${escapeHtml(payload.short_title || `${payload.session_id} prompt ${payload.prompt_index}`)}</h3>
        <div class="sub">${escapeHtml(payload.session_id || "")} · prompt ${escapeHtml(payload.prompt_index || "")} · ${escapeHtml(payload.session_date || "")}</div>
      </div>
      <div class="muted">Total ${money(payload.total_cost_usd || 0)}</div>
    </div>
    <div class="detail-grid">
      <div class="detail-card"><div class="v">${money(payload.headline_context_cost_usd || 0)}</div><div class="l">Context</div></div>
      <div class="detail-card"><div class="v">${money(payload.headline_cache_read_cost_usd || 0)}</div><div class="l">Cache read</div></div>
      <div class="detail-card"><div class="v">${money(payload.headline_output_cost_usd || 0)}</div><div class="l">Output</div></div>
    </div>
    <h4>Fixing-cause rollup</h4>
    <div class="table-wrap"><table><thead><tr><th>Cause</th><th class="n">Cost</th><th class="n">Share</th><th class="n">Events</th></tr></thead><tbody>
      ${(payload.fixing_cause_rollup || []).map(row => `<tr><td>${escapeHtml(row.cause || "")}</td><td class="n">${money(row.cost_usd || 0)}</td><td class="n">${Number(row.cost_pct || 0).toFixed(1)}%</td><td class="n">${count(row.events || 0)}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">No fixing-cause rows.</td></tr>'}
    </tbody></table></div>
    <h4>Step timeline</h4>
    <div id="stepTimeline">
      ${timeline.map((chunk, index) => {
        const isActive = index === state.selectedStepIndex;
        const rows = stepCommandRows(payload, chunk);
        return `<section class="chunk-card${isActive ? " is-active" : ""}" data-step-index="${index}">
          <div class="chunk-summary" data-step-index="${index}">
            <div class="chunk-title-row">
              <div class="chunk-title">${escapeHtml(chunk.display_title || `Step ${index + 1} · ${chunk.workflow_phase || ""}`)} · ${count(rows.length)} commands</div>
              <div class="chunk-toggle">${isActive ? "Collapse" : "Expand"}</div>
            </div>
            <div class="chunk-meta"><span>${escapeHtml(chunk.workflow_phase || "")}</span><span>${escapeHtml(chunk.efficiency_label || "")}</span><span>${escapeHtml(chunk.fixing_cause || "")}</span></div>
            <div class="chunk-preview">${escapeHtml(summarizeStep(chunk))}</div>
          </div>
          <div class="chunk-body" data-step-index="${index}" ${isActive ? "" : "hidden"}>${isActive ? renderStepBody(payload, chunk) : ""}</div>
        </section>`;
      }).join("") || '<div class="empty">No step timeline recorded for this session.</div>'}
    </div>
    <h4>Command table</h4>
    <div class="table-wrap">
      <table id="commandTable">
        <thead><tr><th>#</th><th class="n">Cost</th><th class="n">Context</th><th class="n">Cache read</th><th class="n">Output</th><th>Phase</th><th>Fixing cause</th><th>Tool</th><th>Preview</th></tr></thead>
        <tbody>
          ${commands.map(row => {
            const commandIndex = Number(row.command_index);
            const highlighted = activeChunk && commandIndex >= Number(activeChunk.start_command_index) && commandIndex <= Number(activeChunk.end_command_index);
            const tool = [row.function_name, row.shell_verb].filter(Boolean).join(" / ");
            return `<tr data-command-index="${escapeHtml(row.command_index)}" class="${highlighted ? "is-highlighted" : ""}">
              <td class="n">${escapeHtml(row.command_index)}</td>
              <td class="n">${money(row.cost_usd || 0)}</td>
              <td class="n">${money(row.headline_context_cost_usd || 0)}</td>
              <td class="n">${money(row.headline_cache_read_cost_usd || 0)}</td>
              <td class="n">${money(row.headline_output_cost_usd || 0)}</td>
              <td>${escapeHtml(row.workflow_phase || "")}</td>
              <td>${escapeHtml(row.fixing_cause || "")}</td>
              <td>${escapeHtml(tool)}</td>
              <td class="preview">${escapeHtml(row.preview || "")}</td>
            </tr>`;
          }).join("")}
        </tbody>
      </table>
    </div>`;
}

function setSelectedStep(index) {
  state.selectedStepIndex = state.selectedStepIndex === index ? -1 : index;
  if (state.selectedAttempt) renderSessionDetail(state.selectedAttempt);
}

async function loadAttempt(attempt) {
  state.selectedAttempt = attempt;
  state.selectedStepIndex = -1;
  const key = attemptKey(attempt);
  const root = document.getElementById("sessionDetail");
  if (isLocalFileMode()) {
    sessionDetailCache[key] = { error: "Session drill-down requires the served page; open this report through the local insights server instead of a local file." };
    renderSessionDetail(attempt);
    if (root) root.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  if (!sessionDetailCache[key]) {
    if (root) root.innerHTML = '<div class="detail-empty">Loading session detail…</div>';
    try {
      const res = await fetch(`/api/cost-explorer-window?session_id=${encodeURIComponent(attempt.session_id)}&prompt_index=${encodeURIComponent(attempt.prompt_index)}`);
      if (res.status === 404) {
        sessionDetailCache[key] = { error: "Session detail not found for this attempt." };
      } else if (!res.ok) {
        sessionDetailCache[key] = { error: `Failed to load session detail (HTTP ${res.status}).` };
      } else {
        sessionDetailCache[key] = { payload: await res.json() };
      }
    } catch (error) {
      sessionDetailCache[key] = { error: "Failed to load session detail: " + (error && error.message ? error.message : error) };
    }
  }
  renderSessionDetail(attempt);
  if (root) root.scrollIntoView({ behavior: "smooth", block: "start" });
}

function clearFilters() {
  state.from = "";
  state.to = "";
  state.host = "";
  state.repo = "";
  state.minAttempts = 1;
  state.thrashOnly = false;
  state.search = "";
  const fromInput = document.getElementById("fromDate");
  const toInput = document.getElementById("toDate");
  const hostInput = document.getElementById("hostFilter");
  const repoInput = document.getElementById("repoFilter");
  const minAttemptsInput = document.getElementById("minAttemptsFilter");
  const thrashOnlyInput = document.getElementById("thrashOnlyFilter");
  const searchInput = document.getElementById("searchFilter");
  if (fromInput) fromInput.value = "";
  if (toInput) toInput.value = "";
  if (hostInput) hostInput.value = "";
  if (repoInput) repoInput.value = "";
  if (minAttemptsInput) minAttemptsInput.value = "1";
  if (thrashOnlyInput) thrashOnlyInput.checked = false;
  if (searchInput) searchInput.value = "";
  render();
}

function bindControls() {
  const hostSelect = document.getElementById("hostFilter");
  if (hostSelect) {
    const hosts = new Set();
    (REPORT.thrash_targets || []).forEach(target => (target.attempts_detail || []).forEach(a => { if (a.host) hosts.add(a.host); }));
    hostSelect.innerHTML = '<option value="">All hosts</option>' + [...hosts].sort().map(host => `<option value="${escapeHtml(host)}">${escapeHtml(host)}</option>`).join("");
  }
  const bind = (id, eventName, handler) => {
    const node = document.getElementById(id);
    if (node) node.addEventListener(eventName, handler);
  };
  bind("fromDate", "change", event => { state.from = event.target.value; render(); });
  bind("toDate", "change", event => { state.to = event.target.value; render(); });
  bind("hostFilter", "change", event => { state.host = event.target.value; render(); });
  bind("repoFilter", "input", event => { state.repo = event.target.value; render(); });
  bind("minAttemptsFilter", "input", event => { state.minAttempts = Number(event.target.value) || 0; render(); });
  bind("thrashOnlyFilter", "change", event => { state.thrashOnly = event.target.checked; render(); });
  bind("searchFilter", "input", event => { state.search = event.target.value; render(); });

  bindSortHeaders("categoryTable", state.categorySort, render);
  bindSortHeaders("thrashTable", state.thrashSort, render);

  const thrashBody = document.getElementById("thrashTableBody");
  if (thrashBody) {
    thrashBody.addEventListener("click", event => {
      const attemptRow = event.target.closest(".attempt-row");
      if (attemptRow) {
        if (attemptRow.classList.contains("is-unavailable")) {
          loadAttempt({ session_id: attemptRow.dataset.sessionId, prompt_index: attemptRow.dataset.promptIndex });
          return;
        }
        loadAttempt({ session_id: attemptRow.dataset.sessionId, prompt_index: attemptRow.dataset.promptIndex });
        return;
      }
      const clearBtn = event.target.closest("#clearFiltersBtn");
      if (clearBtn) {
        clearFilters();
        return;
      }
      const targetRow = event.target.closest("tr.target-row");
      if (targetRow) {
        const key = targetRow.dataset.targetKey;
        state.expandedTarget = state.expandedTarget === key ? null : key;
        const [lo, hi] = rangeBounds();
        renderThrashTable(sortRows(aggregateTargets(lo, hi), state.thrashSort, TARGET_ACCESSORS));
      }
    });
  }

  const sessionDetail = document.getElementById("sessionDetail");
  if (sessionDetail) {
    sessionDetail.addEventListener("click", event => {
      const summary = event.target.closest(".chunk-summary[data-step-index]");
      if (!summary) return;
      setSelectedStep(Number(summary.dataset.stepIndex));
    });
  }
}

init();
"""

PAGE_HTML_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PR Automation Breakdown</title>
<style>__PAGE_CSS__</style>
</head>
<body>
  <div class="nav"><a href="/insights">← All insights</a><a href="/cost-summary">Fleet cost</a></div>
  <h1>PR Automation Breakdown</h1>
  <div class="sub">Retry-thrash cost across automated PR repair sessions. Filter, sort, and drill into a target's attempts.</div>
  <div id="err"></div>

  <section class="controls-card">
    <div class="group">
      <span class="lbl">From date</span>
      <input class="date-input" type="date" id="fromDate">
    </div>
    <div class="group">
      <span class="lbl">To date</span>
      <input class="date-input" type="date" id="toDate">
    </div>
    <div class="group">
      <span class="lbl">Host</span>
      <select class="select" id="hostFilter"><option value="">All hosts</option></select>
    </div>
    <div class="group">
      <span class="lbl">Repo contains</span>
      <input class="search" type="search" id="repoFilter" placeholder="owner/repo">
    </div>
    <div class="group">
      <span class="lbl">Min attempts</span>
      <input class="num-input" type="number" min="0" step="1" id="minAttemptsFilter" value="1">
    </div>
    <div class="group">
      <label class="checkbox"><input type="checkbox" id="thrashOnlyFilter"> Thrash only (&gt;1 attempt)</label>
    </div>
    <div class="group">
      <span class="lbl">Search target</span>
      <input class="search" type="search" id="searchFilter" placeholder="repo or PR number">
    </div>
    <div class="group">
      <button class="btn" type="button" id="clearFiltersTopBtn" onclick="clearFilters()">Clear filters</button>
    </div>
  </section>

  <div class="kpis" id="kpis"></div>

  <div class="charts-grid">
    <div class="chart-card"><h3>Daily thrash cost by host</h3><canvas id="dailyChart"></canvas></div>
    <div class="chart-card"><h3>Cost by category</h3><canvas id="categoryChart"></canvas></div>
    <div class="chart-card"><h3>Top retry-thrash targets</h3><canvas id="thrashChart"></canvas></div>
  </div>

  <section class="panel">
    <div class="panel-head"><h2>Categories</h2></div>
    <div class="panel-body">
      <div class="table-wrap">
        <table id="categoryTable">
          <thead id="categoryTableHead"><tr>
            <th class="sortable" data-sort-key="label">Category <span class="sort-indicator">↕</span></th>
            <th class="sortable n" data-sort-key="prompts">Prompts <span class="sort-indicator">↕</span></th>
            <th class="sortable n" data-sort-key="cost_usd">Cost <span class="sort-indicator">↕</span></th>
            <th class="sortable n" data-sort-key="active_seconds">Active hours <span class="sort-indicator">↕</span></th>
            <th class="sortable n" data-sort-key="raw_seconds">Raw hours <span class="sort-indicator">↕</span></th>
            <th>Automated share</th>
          </tr></thead>
          <tbody id="categoryTableBody"></tbody>
        </table>
      </div>
    </div>
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Retry-thrash targets</h2><div class="sub">Click a target to see its attempts; click an attempt to load full session detail below.</div></div>
    <div class="panel-body">
      <div class="table-wrap">
        <table id="thrashTable">
          <thead id="thrashTableHead"><tr>
            <th class="sortable" data-sort-key="target">Target <span class="sort-indicator">↕</span></th>
            <th class="sortable n" data-sort-key="attempts">Attempts <span class="sort-indicator">↕</span></th>
            <th class="sortable n" data-sort-key="cost_usd">Cost <span class="sort-indicator">↕</span></th>
            <th class="sortable n" data-sort-key="avg_cost">Avg cost/attempt <span class="sort-indicator">↕</span></th>
            <th class="sortable n" data-sort-key="share_pct">Share of total <span class="sort-indicator">↕</span></th>
          </tr></thead>
          <tbody id="thrashTableBody"></tbody>
        </table>
      </div>
    </div>
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Session detail</h2></div>
    <div class="panel-body" id="sessionDetail"><div class="detail-empty">Select an attempt from a retry-thrash target to see its full session detail.</div></div>
  </section>

  <script id="prAutomationData" type="application/json">__REPORT_JSON__</script>
  <script src="/cost-assets/chart.umd.min.js"></script>
  <script>__PAGE_JS__</script>
</body>
</html>
"""


def render_html(data: dict[str, Any], args: argparse.Namespace) -> str:
    payload_json = json.dumps(data).replace("<", "\\u003c")
    page = PAGE_HTML_SHELL.replace("__PAGE_CSS__", PAGE_CSS)
    page = page.replace("__PAGE_JS__", PAGE_JS)
    page = page.replace("__REPORT_JSON__", payload_json)
    return page


def main() -> int:
    args = parse_args()
    data = build_report(args)
    json_path = Path(args.json_out)
    markdown_path = Path(args.markdown_out)
    html_path = Path(args.html_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    markdown_path.write_text(render_markdown(data, args), encoding="utf-8")
    html_path.write_text(render_html(data, args), encoding="utf-8")
    print(f"JSON written: {json_path}")
    print(f"Markdown written: {markdown_path}")
    print(f"HTML written: {html_path}")
    print(f"Thrash targets: {len(data['thrash_targets'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
