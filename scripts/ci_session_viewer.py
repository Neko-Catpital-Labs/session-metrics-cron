#!/usr/bin/env python3
"""Minimal JSONL session viewer for CI proof sessions."""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_DIR = REPO_ROOT / "reports" / "ci-proof-sessions"
ATTRIBUTION_CSV = REPO_ROOT / "reports" / "usage-command-attribution-v4_5.csv"

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CI proof sessions — JSONL viewer</title>
<style>
  :root { --fg:#1b1f24; --sub:#57606a; --line:#e2e6ea; --bg:#f6f8fa; --link:#0969da; --user:#ddf4ff; --tool:#fff8c5; --meta:#fbefff; }
  * { box-sizing: border-box; }
  body { font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif; color:var(--fg); margin:0; display:grid; grid-template-columns:280px 1fr; min-height:100vh; }
  aside { border-right:1px solid var(--line); background:#fff; padding:1rem; overflow:auto; }
  main { overflow:auto; padding:1rem 1.25rem 2rem; background:var(--bg); }
  h1 { font-size:1.1rem; margin:0 0 .75rem; }
  .session-link { display:block; padding:.45rem .55rem; border:1px solid var(--line); border-radius:8px; margin-bottom:.45rem; text-decoration:none; color:var(--fg); background:#fff; }
  .session-link.active { border-color:var(--link); box-shadow:0 0 0 1px var(--link); }
  .session-link small { display:block; color:var(--sub); margin-top:.15rem; }
  .entry { background:#fff; border:1px solid var(--line); border-radius:8px; margin-bottom:.65rem; overflow:hidden; }
  .entry-head { padding:.35rem .6rem; font-size:.75rem; color:var(--sub); border-bottom:1px solid var(--line); display:flex; gap:.75rem; flex-wrap:wrap; }
  .entry-body { margin:0; padding:.6rem .75rem; white-space:pre-wrap; word-break:break-word; font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; max-height:420px; overflow:auto; }
  .kind-user .entry-head { background:var(--user); }
  .kind-tool .entry-head { background:var(--tool); }
  .kind-meta .entry-head { background:var(--meta); }
  .filter { width:100%; margin-bottom:.75rem; padding:.4rem .5rem; border:1px solid var(--line); border-radius:6px; }
  .stats { color:var(--sub); font-size:.85rem; margin-bottom:1rem; }
  .cost-panel { background:#fff; border:1px solid var(--line); border-radius:10px; padding:.85rem 1rem; margin-bottom:1rem; }
  .cost-panel h2 { margin:0 0 .5rem; font-size:.95rem; }
  .chunk { margin-bottom:.55rem; }
  .chunk-label { display:flex; justify-content:space-between; gap:.75rem; font-size:.78rem; color:var(--sub); margin-bottom:.15rem; }
  .chunk-bar { height:10px; background:#eaeef2; border-radius:999px; overflow:hidden; }
  .chunk-fill { height:100%; background:linear-gradient(90deg,#0969da,#54aeff); border-radius:999px; }
  .chunk-summary { font-size:.9rem; font-weight:600; color:var(--fg); margin:.2rem 0 .35rem; }
  .chunk-detail { font-size:.78rem; color:var(--sub); margin-bottom:.25rem; }
  .chunk-preview { font-size:.75rem; color:var(--sub); font-style:italic; border-left:2px solid var(--line); padding-left:.5rem; margin-top:.25rem; }
  .step-section { margin-bottom:1.25rem; }
  .step-head { background:#fff; border:1px solid var(--line); border-radius:10px; padding:.75rem .9rem; margin-bottom:.55rem; }
  .step-head h3 { margin:0 0 .35rem; font-size:.92rem; }
  .step-meta { font-size:.75rem; color:var(--sub); display:flex; gap:.75rem; flex-wrap:wrap; }
  .entry-prompt-tag { background:#ddf4ff; color:#0550ae; padding:.05rem .35rem; border-radius:4px; font-size:.7rem; }
  .cost-badge { background:#dafbe1; color:#116329; padding:.05rem .4rem; border-radius:4px; font-size:.7rem; font-weight:600; }
  .cost-badge.muted { background:#f6f8fa; color:#57606a; font-weight:500; }
  .cmd-tag { background:#fff8c5; color:#7d4e00; padding:.05rem .35rem; border-radius:4px; font-size:.7rem; }
  .cost-breakdown { background:#f6f8fa; border:1px solid var(--line); border-radius:8px; padding:.65rem .75rem; margin:.55rem 0 .75rem; font-size:.78rem; }
  .cost-breakdown h4 { margin:0 0 .45rem; font-size:.82rem; color:var(--fg); }
  .cost-breakdown p { margin:0 0 .5rem; color:var(--sub); line-height:1.4; }
  .breakdown-grid { display:grid; grid-template-columns:1fr 1fr; gap:.75rem; }
  @media (max-width:900px) { .breakdown-grid { grid-template-columns:1fr; } }
  .breakdown-table { width:100%; border-collapse:collapse; font-size:.75rem; }
  .breakdown-table th, .breakdown-table td { text-align:left; padding:.25rem .35rem; border-bottom:1px solid var(--line); vertical-align:top; }
  .breakdown-table th { color:var(--sub); font-weight:600; }
  .mini-bar { height:6px; background:#eaeef2; border-radius:999px; overflow:hidden; margin-top:.15rem; }
  .mini-fill { height:100%; background:#54aeff; border-radius:999px; }
  .pill-row { display:flex; flex-wrap:wrap; gap:.35rem; margin-top:.25rem; }
  .pill { background:#fff; border:1px solid var(--line); border-radius:999px; padding:.1rem .45rem; font-size:.72rem; color:var(--sub); }
</style>
</head>
<body>
<aside>
  <h1>CI proof sessions</h1>
  <div id="session-list"></div>
</aside>
<main>
  <div class="stats" id="stats"></div>
  <div class="cost-panel" id="cost-panel" hidden>
    <h2>Session steps (cost + summary)</h2>
    <div id="chunk-bars"></div>
  </div>
  <input class="filter" id="filter" placeholder="Filter lines (ci, fix, gh run, test, failure…)" />
  <div id="step-sections"></div>
  <div id="entries"></div>
</main>
<script>
const sessions = SESSIONS_JSON;
const listEl = document.getElementById('session-list');
const entriesEl = document.getElementById('entries');
const statsEl = document.getElementById('stats');
const filterEl = document.getElementById('filter');
const costPanelEl = document.getElementById('cost-panel');
const chunkBarsEl = document.getElementById('chunk-bars');
const stepSectionsEl = document.getElementById('step-sections');
let active = new URLSearchParams(location.search).get('file') || sessions[0]?.file;
let _chunks = [];

function esc(s){ return (s||'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function costBadge(e) {
  if (e.billing_kind === 'command' && e.cost_usd != null) {
    return `<span class="cost-badge">$${Number(e.cost_usd).toFixed(4)}</span>`;
  }
  if (e.billing_kind === 'result') {
    return `<span class="cost-badge muted">result</span>`;
  }
  return '';
}

function entryHead(e, p) {
  const cmd = e.command_index ? `<span class="cmd-tag">cmd ${esc(e.command_index)}</span>` : '';
  const fn = e.function_name ? `<span>${esc(e.function_name)}</span>` : '';
  return `<div class="entry-head">
    <span>#${e.line}</span><span>${esc(e.type)}</span><span>${esc(e.kind)}</span>
    ${cmd}${fn}${costBadge(e)}<span class="entry-prompt-tag">step ${esc(p)}</span>
  </div>`;
}

function renderList() {
  listEl.innerHTML = sessions.map(s => `
    <a class="session-link ${s.file===active?'active':''}" href="?file=${encodeURIComponent(s.file)}">
      <strong>${esc(s.label)}</strong>
      <small>${esc(s.cost)} · ${esc(s.kind)}</small>
    </a>`).join('');
}

async function loadSession(file) {
  active = file;
  renderList();
  statsEl.textContent = 'Loading…';
  entriesEl.innerHTML = '';
  const res = await fetch('/api/session?file=' + encodeURIComponent(file));
  const data = await res.json();
  if (!res.ok || data.error) {
    statsEl.textContent = 'Failed to load: ' + (data.error || res.status);
    entriesEl.innerHTML = '<p>Could not load session JSONL.</p>';
    window._entries = [];
    return;
  }
  window._entries = data.entries || [];
  _chunks = data.prompt_chunks || [];
  const total = data.attributed_cost_usd || 0;
  statsEl.textContent = `${data.label} · ${data.line_count} JSONL lines · ${data.size_mb} MB · $${total.toFixed(2)} attributed`;
  renderCostChunks(_chunks, total);
  renderStepSections(_chunks, window._entries);
  entriesEl.hidden = true;
}

function renderCostChunks(chunks, total) {
  if (!chunks.length) {
    costPanelEl.hidden = true;
    return;
  }
  costPanelEl.hidden = false;
  const sorted = chunks.slice().sort((a,b) => Number(b.cost_usd)-Number(a.cost_usd));
  const max = Math.max(...sorted.map(c => c.cost_usd), 0.0001);
  chunkBarsEl.innerHTML = sorted.map(c => {
    const pct = total > 0 ? (100 * c.cost_usd / total) : 0;
    const width = Math.max(2, 100 * c.cost_usd / max);
    const actions = (c.actions || []).map(a => `<li>${esc(a)}</li>`).join('');
    return `<div class="chunk">
      <div class="chunk-label"><span>Step ${esc(c.prompt_index)} · ${c.commands} commands</span><span>$${Number(c.cost_usd).toFixed(2)} (${pct.toFixed(0)}%)</span></div>
      <div class="chunk-bar"><div class="chunk-fill" style="width:${width}%"></div></div>
      <div class="chunk-summary">${esc(c.summary || 'Session step')}</div>
      ${actions ? `<ul class="chunk-detail">${actions}</ul>` : ''}
      <div class="chunk-preview">User asked: ${esc((c.preview || '').slice(0,180))}</div>
    </div>`;
  }).join('');
}

function renderCostBreakdown(c) {
  const b = c.cost_breakdown;
  if (!b || !c.commands) return '';
  const why = b.why_expensive ? `<p><strong>Why $${Number(c.cost_usd).toFixed(2)}?</strong> ${esc(b.why_expensive)}</p>` : '';
  const tokens = b.token_summary ? `<p>${esc(b.token_summary)}</p>` : '';
  const tools = (b.by_tool || []).map(row => {
    const width = Math.max(2, Number(row.pct || 0));
    return `<tr><td>${esc(row.name)}</td><td>${row.commands}</td><td>$${Number(row.cost_usd).toFixed(2)}</td><td>${Number(row.pct).toFixed(0)}%<div class="mini-bar"><div class="mini-fill" style="width:${width}%"></div></div></td></tr>`;
  }).join('');
  const top = (b.top_commands || []).map(row =>
    `<tr><td>cmd ${esc(row.command_index)}</td><td>${esc(row.function_name)}</td><td>$${Number(row.cost_usd).toFixed(2)}</td><td>${Number(row.pct_of_step).toFixed(0)}%</td><td><code>${esc((row.preview || '').slice(0,90))}</code></td></tr>`
  ).join('');
  const activity = (b.by_activity || []).map(row => `<span class="pill">${esc(row.name)}: ${row.count}</span>`).join('');
  return `<div class="cost-breakdown">
    <h4>Cost breakdown</h4>
    ${why}${tokens}
    <div class="breakdown-grid">
      <div>
        <strong>By tool</strong>
        <table class="breakdown-table"><thead><tr><th>Tool</th><th>Cmds</th><th>Cost</th><th>Share</th></tr></thead><tbody>${tools || '<tr><td colspan="4">No attribution data</td></tr>'}</tbody></table>
      </div>
      <div>
        <strong>Top expensive commands</strong>
        <table class="breakdown-table"><thead><tr><th>Cmd</th><th>Tool</th><th>Cost</th><th>Share</th><th>What it ran</th></tr></thead><tbody>${top || '<tr><td colspan="5">No attribution data</td></tr>'}</tbody></table>
      </div>
    </div>
    ${activity ? `<div style="margin-top:.5rem"><strong>Activity</strong><div class="pill-row">${activity}</div></div>` : ''}
  </div>`;
}

function renderStepSections(chunks, entries) {
  const q = (filterEl.value || '').toLowerCase();
  const byPrompt = {};
  for (const e of entries) {
    const p = e.prompt_index || '0';
    (byPrompt[p] ||= []).push(e);
  }
  const ordered = chunks.length ? chunks.slice().sort((a,b) => Number(a.prompt_index)-Number(b.prompt_index)) : Object.keys(byPrompt).sort((a,b)=>Number(a)-Number(b)).map(p => ({prompt_index:p}));
  stepSectionsEl.innerHTML = ordered.map(c => {
    const p = c.prompt_index;
    const rows = (byPrompt[p] || []).filter(e => !q || (e.text||'').toLowerCase().includes(q));
    if (!rows.length && q) return '';
    const head = `<div class="step-head">
      <h3>Step ${esc(p)}: ${esc(c.summary || 'Session step')}</h3>
      <div class="step-meta">
        ${c.cost_usd != null ? `<span>$${Number(c.cost_usd).toFixed(2)}</span>` : ''}
        ${c.commands ? `<span>${c.commands} commands</span>` : ''}
        ${c.tools ? `<span>Tools: ${esc(c.tools)}</span>` : ''}
      </div>
      ${c.preview ? `<div class="chunk-preview">User asked: ${esc(String(c.preview).slice(0,200))}</div>` : ''}
      ${renderCostBreakdown(c)}
    </div>`;
    const body = rows.map(e => `
      <div class="entry kind-${esc(e.kind)}">
        ${entryHead(e, p)}
        <pre class="entry-body">${esc(e.text)}</pre>
      </div>`).join('');
    const emptyNote = !rows.length && c.commands
      ? `<p class="chunk-detail">This step ran ${c.commands} commands ($${Number(c.cost_usd || 0).toFixed(2)}). Log sample not loaded — try clearing the filter.</p>`
      : '<p class="chunk-detail">No matching log lines for this step.</p>';
    return `<section class="step-section">${head}${body || emptyNote}</section>`;
  }).join('');
  if (!stepSectionsEl.innerHTML) stepSectionsEl.innerHTML = '<p>No matching steps.</p>';
}

function renderEntries(entries) {
  renderStepSections(_chunks, entries);
}

filterEl.addEventListener('input', () => {
  if (!window._entries) return;
  renderStepSections(_chunks, window._entries);
});

renderList();
loadSession(active);
</script>
</body>
</html>
"""


_ATTRIBUTION_CACHE: dict[str, list[dict[str, str]]] | None = None

INTENT_LABELS: dict[str, str] = {
    "failure_diagnosis_inspection": "diagnosing failures and reading code",
    "implementation_planning_inspection": "planning how to implement changes",
    "ci_monitoring": "checking CI / GitHub Actions status",
    "environment_initialization": "setting up the dev environment",
    "test_execution": "running tests",
    "full_validation": "running full validation",
    "repo_orientation": "exploring the repo layout",
    "diff_review": "reviewing diffs and changes",
    "bug_fix_edit": "editing code to fix bugs",
    "feature_implementation_edit": "implementing feature code",
    "refactor_edit": "refactoring code",
    "test_or_proof_edit": "writing tests or proof scripts",
    "pr_creation_or_update": "creating or updating PRs",
    "branch_stack_orchestration": "managing stacked PR branches",
    "fixing_failure": "fixing a reported failure",
    "remote_orchestration": "orchestrating remote agents/workers",
    "process_control": "controlling running processes",
    "planning_or_task_tracking": "planning or tracking tasks",
    "analytics_reporting": "building analytics/report output",
    "documentation_edit": "editing documentation",
    "failure_reproduction": "reproducing a failure",
    "generated_artifact_edit": "editing generated artifacts",
    "analytics_inspection": "inspecting analytics data",
}

TOOL_LABELS: dict[str, str] = {
    "read": "reading files",
    "bash": "running shell commands",
    "exec_command": "running shell commands",
    "write_stdin": "interacting with a running process",
    "search": "searching the codebase",
    "edit": "editing files",
    "write": "writing files",
    "browser": "using the browser",
    "find": "finding files",
}


def _counter_field(rows: list[dict[str, str]], field: str) -> list[tuple[str, int]]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        value = str(row.get(field) or "").strip()
        if value:
            counts[value] += 1
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)


def _infer_user_goal(preview: str) -> str:
    text = preview.lower()
    rules: list[tuple[str, str]] = [
        (r"worker registration|execution model|worker state", "Investigate worker registration changes and open a PR"),
        (r"make a pull request|create a pr|open a pr", "Create a pull request for the current changes"),
        (r"file a ticket|linear", "Create a tracking ticket for follow-up refactor work"),
        (r"blocked", "Mark a PR blocked and document the missing dependency"),
        (r"workflow is blocking|what workflow", "Find which Invoker workflow is blocking progress"),
        (r"rawstatus|raw status", "Clarify status vs rawStatus fields in the PR changes"),
        (r"fix wit|fix with codex|ci failure", "Fix CI failures triggered by an automated worker"),
        (r"gh run|failed check|github actions", "Investigate and fix failing GitHub Actions checks"),
        (r"coderabbit", "Address CodeRabbit review comments across the PR stack"),
        (r"pr skill|broken up|split.*pr", "Split work into smaller PRs and clean up the stack"),
        (r"implement the plan|continue fixing", "Continue implementing an agreed fix plan"),
        (r"invariant", "Work through invariant / safety review feedback"),
    ]
    for pattern, label in rules:
        if re.search(pattern, text, re.I):
            return label
    pr_match = re.search(r"\b(\d{4})\b", preview)
    if pr_match and re.search(r"\bpr\b|pull request|#\s*\d", text, re.I):
        return f"Answer questions and adjust PR #{pr_match.group(1)}"
    cleaned = re.sub(r"\s+", " ", preview).strip()
    if len(cleaned) > 120:
        cleaned = cleaned[:117] + "..."
    return cleaned or "Follow-up request in the session"


def _summarize_chunk_rows(rows: list[dict[str, str]], preview: str) -> dict[str, object]:
    intents = _counter_field(rows, "agent_tool_intention")
    tools = _counter_field(rows, "function_name")
    verbs = _counter_field(rows, "shell_verb")
    goal = _infer_user_goal(preview)

    top_intent = intents[0][0] if intents else ""
    intent_phrase = INTENT_LABELS.get(top_intent, top_intent.replace("_", " ") if top_intent else "working on the request")

    tool_parts: list[str] = []
    for name, count in tools[:3]:
        label = TOOL_LABELS.get(name, name)
        tool_parts.append(f"{label} ({count}x)")
    verb_parts = [name for name, _ in verbs[:3] if name]
    if verb_parts:
        tool_parts.append("shell: " + ", ".join(verb_parts))

    summary = f"{goal}. Mostly {intent_phrase}."
    actions = []
    if tool_parts:
        actions.append("Agent activity: " + "; ".join(tool_parts))
    if intents[:2]:
        extra = ", ".join(INTENT_LABELS.get(name, name.replace("_", " ")) for name, _ in intents[:2])
        actions.append(f"Work type: {extra}")
    sample_cmds = []
    for row in rows:
        preview_text = str(row.get("command_preview") or row.get("stdin_preview") or "").strip()
        if preview_text and preview_text not in sample_cmds:
            sample_cmds.append(preview_text[:100])
        if len(sample_cmds) >= 2:
            break
    for cmd in sample_cmds:
        actions.append(f"Example command: {cmd}")

    tools_label = ", ".join(name for name, _ in tools[:4])
    return {
        "summary": summary,
        "actions": actions[:4],
        "tools": tools_label,
        "goal": goal,
        "intent": intent_phrase,
    }


def _extract_user_text(obj: dict[str, object]) -> str:
    msg = obj.get("message")
    if isinstance(msg, dict) and msg.get("role") == "user":
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(str(part.get("text") or part.get("input_text") or ""))
            return " ".join(p for p in parts if p).strip()
    payload = obj.get("payload")
    if isinstance(payload, dict) and payload.get("type") == "message" and payload.get("role") == "user":
        content = payload.get("content")
        if isinstance(content, list):
            return " ".join(
                str(part.get("text") or part.get("input_text") or "")
                for part in content
                if isinstance(part, dict)
            ).strip()
    return ""


def parse_prompt_windows(path: Path) -> list[dict[str, object]]:
    windows: list[dict[str, object]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_no, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            user_text = _extract_user_text(obj)
            if not user_text:
                continue
            windows.append(
                {
                    "prompt_index": str(len(windows) + 1),
                    "start_line": line_no,
                    "preview": user_text[:500],
                }
            )
    return windows


def prompt_index_for_line(windows: list[dict[str, object]], line_no: int) -> str:
    current = "0"
    for window in windows:
        if int(window["start_line"]) <= line_no:
            current = str(window["prompt_index"])
        else:
            break
    return current


def _path_lookup_keys(path: Path) -> list[str]:
    resolved = str(path.resolve())
    keys = [resolved]
    if resolved.startswith("/private"):
        keys.append(resolved.replace("/private", "", 1))
    keys.append(resolved.replace("/private/tmp/", "/tmp/"))
    return list(dict.fromkeys(keys))


def load_attribution_rows() -> dict[str, list[dict[str, str]]]:
    global _ATTRIBUTION_CACHE
    if _ATTRIBUTION_CACHE is not None:
        return _ATTRIBUTION_CACHE
    by_file: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_basename: dict[str, list[dict[str, str]]] = defaultdict(list)
    if not ATTRIBUTION_CSV.exists():
        _ATTRIBUTION_CACHE = by_file
        return by_file
    with ATTRIBUTION_CSV.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            file_path = row.get("file") or ""
            by_file[file_path].append(row)
            by_basename[Path(file_path).name].append(row)
    # Attach basename index for fallback lookups.
    by_file["__by_basename__"] = by_basename  # type: ignore[assignment]
    _ATTRIBUTION_CACHE = by_file
    return by_file


def attribution_rows_for_session(session_path: Path) -> list[dict[str, str]]:
    store = load_attribution_rows()
    for key in _path_lookup_keys(session_path):
        rows = store.get(key)
        if rows:
            return rows
    basename = session_path.name
    by_basename = store.get("__by_basename__", {})
    if isinstance(by_basename, dict):
        rows = by_basename.get(basename)
        if rows:
            return rows
    return []


def _command_preview(row: dict[str, str]) -> str:
    preview = str(row.get("command_preview") or row.get("stdin_preview") or row.get("delegated_task_preview") or "").strip()
    if preview:
        return preview
    function_name = str(row.get("function_name") or "")
    target = str(row.get("target") or "")
    if function_name == "read" and target:
        return f"read {target}"
    return function_name or "command"


def _classify_shell_activity(command_preview: str, function_name: str) -> str:
    cmd = command_preview.lower().strip()
    if function_name == "read":
        return "read files"
    if function_name == "edit":
        return "edit files"
    if function_name == "search":
        return "search codebase"
    if function_name == "write":
        return "write files"
    if not cmd:
        return function_name or "other"
    if cmd.startswith("git "):
        return "git"
    if cmd.startswith("gh "):
        return "gh / CI checks"
    if "pnpm" in cmd or "npm " in cmd or "vitest" in cmd or "pytest" in cmd:
        return "tests / pnpm"
    if "mergify" in cmd or "stack" in cmd:
        return "stack / mergify"
    if cmd.startswith("sleep") or "yield_time" in cmd:
        return "wait / sleep"
    return "other shell"


def _build_cost_breakdown(prompt_rows: list[dict[str, str]], preview: str) -> dict[str, object]:
    if not prompt_rows:
        return {}

    total = sum(float(row.get("allocated_total_cost_usd") or 0) for row in prompt_rows)
    if total <= 0:
        return {}

    by_tool_counts: dict[str, dict[str, float | int]] = defaultdict(lambda: {"commands": 0, "cost_usd": 0.0})
    by_activity: dict[str, int] = defaultdict(int)
    for row in prompt_rows:
        tool = str(row.get("function_name") or "unknown")
        cost = float(row.get("allocated_total_cost_usd") or 0)
        by_tool_counts[tool]["commands"] = int(by_tool_counts[tool]["commands"]) + 1
        by_tool_counts[tool]["cost_usd"] = float(by_tool_counts[tool]["cost_usd"]) + cost
        activity = _classify_shell_activity(_command_preview(row), tool)
        by_activity[activity] += 1

    by_tool = sorted(
        [
            {
                "name": name,
                "commands": int(stats["commands"]),
                "cost_usd": round(float(stats["cost_usd"]), 4),
                "pct": round(100 * float(stats["cost_usd"]) / total, 1),
            }
            for name, stats in by_tool_counts.items()
        ],
        key=lambda item: item["cost_usd"],
        reverse=True,
    )

    top_commands = sorted(
        [
            {
                "command_index": str(row.get("command_index") or ""),
                "function_name": str(row.get("function_name") or ""),
                "cost_usd": round(float(row.get("allocated_total_cost_usd") or 0), 4),
                "pct_of_step": round(100 * float(row.get("allocated_total_cost_usd") or 0) / total, 1),
                "preview": _command_preview(row)[:160],
                "intent": str(row.get("agent_tool_intention") or ""),
            }
            for row in prompt_rows
        ],
        key=lambda item: item["cost_usd"],
        reverse=True,
    )[:8]

    command_count = len(prompt_rows)
    top_tool = by_tool[0] if by_tool else None
    top_cmd = top_commands[0] if top_commands else None
    top_three_cost = sum(item["cost_usd"] for item in top_commands[:3])
    why_parts = [
        f"This step ran {command_count} tool commands in one user turn.",
        f"The ${total:.2f} total is the prompt-window token bill split across those commands — not a single message fee.",
    ]
    if top_tool:
        why_parts.append(
            f"Most spend went to {top_tool['name']} ({top_tool['commands']} cmds, ${top_tool['cost_usd']:.2f}, {top_tool['pct']:.0f}%)."
        )
    if top_cmd and top_cmd["cost_usd"] >= 0.5:
        why_parts.append(
            f"The single most expensive command was #{top_cmd['command_index']} (${top_cmd['cost_usd']:.2f}): {top_cmd['preview'][:100]}."
        )
    if top_three_cost / total >= 0.4:
        why_parts.append(f"The top 3 commands alone account for ${top_three_cost:.2f} ({100 * top_three_cost / total:.0f}% of this step).")

    token_summary = ""
    first = prompt_rows[0]
    prompt_cost = float(first.get("prompt_derived_total_cost_usd") or total)
    input_tokens = int(float(first.get("prompt_input_tokens") or 0))
    cache_read = int(float(first.get("prompt_cache_read_tokens") or 0))
    output_tokens = int(float(first.get("prompt_output_tokens") or 0))
    if input_tokens or cache_read or output_tokens:
        token_summary = (
            f"Prompt window tokens: {input_tokens:,} input · {cache_read:,} cache read · {output_tokens:,} output "
            f"(window cost ${prompt_cost:.2f})."
        )

    return {
        "why_expensive": " ".join(why_parts),
        "token_summary": token_summary,
        "by_tool": by_tool,
        "by_activity": [{"name": name, "count": count} for name, count in sorted(by_activity.items(), key=lambda item: -item[1])],
        "top_commands": top_commands,
    }


def prompt_cost_chunks(session_path: Path) -> tuple[float, list[dict[str, object]]]:
    rows = attribution_rows_for_session(session_path)
    windows = parse_prompt_windows(session_path)
    by_prompt_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_prompt_rows[str(row.get("prompt_index") or "0")].append(row)

    chunks: list[dict[str, object]] = []
    seen: set[str] = set()
    for window in windows:
        prompt = str(window["prompt_index"])
        seen.add(prompt)
        prompt_rows = by_prompt_rows.get(prompt, [])
        preview = str(window.get("preview") or "")
        if prompt_rows and not preview:
            preview = str(prompt_rows[0].get("prompt_preview") or prompt_rows[0].get("first_prompt_preview") or "")
        meta = _summarize_chunk_rows(prompt_rows, preview) if prompt_rows else _summarize_chunk_rows([], preview)
        cost_usd = sum(float(row.get("allocated_total_cost_usd") or 0) for row in prompt_rows)
        breakdown = _build_cost_breakdown(prompt_rows, preview)
        chunks.append(
            {
                "prompt_index": prompt,
                "start_line": window["start_line"],
                "cost_usd": cost_usd,
                "commands": len(prompt_rows),
                "preview": preview,
                "cost_breakdown": breakdown,
                **meta,
            }
        )

    for prompt, prompt_rows in by_prompt_rows.items():
        if prompt in seen:
            continue
        preview = str(prompt_rows[0].get("prompt_preview") or prompt_rows[0].get("first_prompt_preview") or "")
        meta = _summarize_chunk_rows(prompt_rows, preview)
        chunks.append(
            {
                "prompt_index": prompt,
                "cost_usd": sum(float(row.get("allocated_total_cost_usd") or 0) for row in prompt_rows),
                "commands": len(prompt_rows),
                "preview": preview,
                "cost_breakdown": _build_cost_breakdown(prompt_rows, preview),
                **meta,
            }
        )

    chunks.sort(key=lambda item: int(str(item["prompt_index"])))
    total = sum(float(item["cost_usd"]) for item in chunks)
    return total, chunks


def commands_by_prompt(session_path: Path) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in attribution_rows_for_session(session_path):
        grouped[str(row.get("prompt_index") or "0")].append(row)
    for prompt in grouped:
        grouped[prompt].sort(key=lambda row: int(row.get("command_index") or 0))
    return grouped


def _next_command_row(
    prompt_index: str,
    cursors: dict[str, int],
    grouped: dict[str, list[dict[str, str]]],
) -> dict[str, str] | None:
    rows = grouped.get(prompt_index, [])
    if not rows:
        return None
    next_index = cursors.get(prompt_index, 0) + 1
    row = next((item for item in rows if int(item.get("command_index") or 0) == next_index), None)
    if row is None and next_index <= len(rows):
        row = rows[next_index - 1]
    cursors[prompt_index] = next_index
    return row


def build_line_cost_map(path: Path) -> dict[int, dict[str, object]]:
    windows = parse_prompt_windows(path)
    grouped = commands_by_prompt(path)
    cursors: dict[str, int] = defaultdict(int)
    line_cost: dict[int, dict[str, object]] = {}

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_no, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            entry = summarize_line(line_no, stripped)
            prompt_index = prompt_index_for_line(windows, line_no)
            if prompt_index == "0" or entry.get("billing_kind") != "command":
                continue
            row = _next_command_row(prompt_index, cursors, grouped)
            if not row:
                continue
            line_cost[line_no] = {
                "command_index": str(row.get("command_index") or cursors[prompt_index]),
                "cost_usd": round(float(row.get("allocated_total_cost_usd") or 0), 6),
                "function_name": str(row.get("function_name") or entry.get("tool_name") or ""),
                "agent_tool_intention": str(row.get("agent_tool_intention") or ""),
            }
    return line_cost


def attach_line_cost(entry: dict[str, str], line_cost: dict[int, dict[str, object]]) -> None:
    line_no = int(entry["line"])
    billing_kind = entry.get("billing_kind") or "none"
    if billing_kind == "command":
        cost = line_cost.get(line_no)
        if cost:
            entry.update(
                {
                    "command_index": str(cost["command_index"]),
                    "cost_usd": cost["cost_usd"],
                    "function_name": str(cost["function_name"]),
                    "agent_tool_intention": str(cost["agent_tool_intention"]),
                }
            )
    elif billing_kind == "result":
        entry["cost_usd"] = None


def session_catalog() -> list[dict[str, str]]:
    labels = {
        "01-41.82-ci-failure-worker-omp.jsonl": ("$41.82 · ci_failure_fix", "OMP"),
        "02-29.47-pool-scheduling-codex.jsonl": ("$29.47 · ci_failure_fix", "Codex fleet"),
        "03-29.36-stacked-perf-fix-codex.jsonl": ("$29.36 · ci_failure_fix", "Codex fleet"),
        "04-24.25-invariants-omp.jsonl": ("$24.25 · ci_failure_fix", "OMP"),
        "05-21.48-plan-impl-codex.jsonl": ("$21.48 · ci_failure_fix", "Codex fleet"),
    }
    out = []
    for path in sorted(SESSIONS_DIR.glob("*.jsonl")):
        cost, kind = labels.get(path.name, (path.name, "session"))
        out.append({"file": path.name, "label": path.name, "cost": cost, "kind": kind})
    return out


def summarize_line(line_no: int, raw: str) -> dict[str, str]:
    kind = "meta"
    type_name = "unknown"
    text = raw
    billing_kind = "none"
    tool_name = ""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "line": str(line_no),
            "type": "invalid-json",
            "kind": "meta",
            "text": raw[:4000],
            "billing_kind": "none",
        }

    if isinstance(obj, dict):
        type_name = str(obj.get("type") or obj.get("event") or "object")
        msg = obj.get("message")
        payload = obj.get("payload")
        if isinstance(msg, dict):
            role = str(msg.get("role") or "")
            if role == "user":
                kind = "user"
            elif role == "assistant":
                kind = "message"
            elif role == "toolResult":
                kind = "tool"
                billing_kind = "result"
            content = msg.get("content")
            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            parts.append(str(part.get("text") or ""))
                        elif part.get("type") == "toolCall":
                            tool_name = str(part.get("name") or "")
                            kind = "tool"
                            billing_kind = "command"
                            parts.append(f"toolCall: {part.get('name')} {json.dumps(part.get('arguments', {}))[:500]}")
                        elif part.get("type") == "input_text":
                            parts.append(str(part.get("text") or ""))
                text = "\n".join(p for p in parts if p) or json.dumps(msg, indent=2)[:4000]
            else:
                text = json.dumps(msg, indent=2)[:4000]
        elif isinstance(payload, dict):
            ptype = payload.get("type")
            type_name = str(ptype or type_name)
            if ptype == "message":
                role = payload.get("role")
                if role == "user":
                    kind = "user"
                elif role == "assistant":
                    kind = "message"
                content = payload.get("content")
                if isinstance(content, list):
                    text = "\n".join(
                        str(c.get("text") or c.get("input_text") or c.get("output_text") or "")
                        for c in content
                        if isinstance(c, dict)
                    )[:4000] or json.dumps(payload, indent=2)[:4000]
                else:
                    text = json.dumps(payload, indent=2)[:4000]
            elif ptype == "function_call":
                kind = "tool"
                billing_kind = "command"
                tool_name = str(payload.get("name") or "")
                text = f"function_call: {tool_name} {str(payload.get('arguments') or '')[:500]}"
            elif ptype == "function_call_output":
                kind = "tool"
                billing_kind = "result"
                text = f"function_call_output: {str(payload.get('output') or '')[:500]}"
            else:
                text = json.dumps(payload, indent=2)[:4000]
        else:
            text = json.dumps(obj, indent=2)[:4000]

    lowered = text.lower()
    if kind == "meta" and any(k in lowered for k in ("gh run", "failed check", "ci ", "fix the code", "build/test command failed")):
        kind = "tool"

    entry = {"line": str(line_no), "type": type_name, "kind": kind, "text": text, "billing_kind": billing_kind}
    if tool_name:
        entry["tool_name"] = tool_name
    return entry


def _prompt_line_ranges(windows: list[dict[str, object]]) -> list[tuple[str, int, int | None]]:
    ranges: list[tuple[str, int, int | None]] = []
    for index, window in enumerate(windows):
        start = int(window["start_line"])
        end = int(windows[index + 1]["start_line"]) - 1 if index + 1 < len(windows) else None
        ranges.append((str(window["prompt_index"]), start, end))
    return ranges


def _entry_is_interesting(entry: dict[str, str], line_no: int, user_lines: set[int]) -> bool:
    if line_no in user_lines:
        return True
    if entry["kind"] in {"user", "tool"}:
        return True
    return bool(re.search(r"ci|fix|fail|gh run|test command|toolCall", entry["text"], re.I))


def load_session_entries(path: Path, per_step_limit: int = 18) -> list[dict[str, str]]:
    windows = parse_prompt_windows(path)
    if not windows:
        return []

    line_cost = build_line_cost_map(path)
    ranges = _prompt_line_ranges(windows)
    user_lines = {start for _, start, _ in ranges}
    by_prompt: dict[str, list[dict[str, str]]] = defaultdict(list)
    preamble: list[dict[str, str]] = []

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_no, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            entry = summarize_line(line_no, stripped)
            attach_line_cost(entry, line_cost)
            prompt_index = prompt_index_for_line(windows, line_no)
            entry["prompt_index"] = prompt_index

            if prompt_index == "0":
                if line_no <= 20 or _entry_is_interesting(entry, line_no, user_lines):
                    preamble.append(entry)
                continue

            if not _entry_is_interesting(entry, line_no, user_lines):
                continue
            if len(by_prompt[prompt_index]) >= per_step_limit:
                continue

            step_end = next((end for prompt, _, end in ranges if prompt == prompt_index), None)
            if step_end is not None and line_no > step_end:
                continue
            by_prompt[prompt_index].append(entry)

    entries: list[dict[str, str]] = preamble[:20]
    for prompt_index, start_line, _ in ranges:
        step_entries = by_prompt.get(prompt_index, [])
        if not any(entry["kind"] == "user" for entry in step_entries):
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line_no, raw in enumerate(handle, start=1):
                    if line_no != start_line:
                        continue
                    stripped = raw.strip()
                    if not stripped:
                        break
                    entry = summarize_line(line_no, stripped)
                    attach_line_cost(entry, line_cost)
                    entry["prompt_index"] = prompt_index
                    step_entries.insert(0, entry)
                    break
        entries.extend(step_entries)
    return entries


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            sessions = session_catalog()
            html = INDEX_HTML.replace("SESSIONS_JSON", json.dumps(sessions))
            self._send(200, "text/html; charset=utf-8", html.encode())
            return
        if parsed.path == "/api/session":
            file_name = parse_qs(parsed.query).get("file", [""])[0]
            if not file_name or "/" in file_name or file_name.startswith("."):
                self._send(400, "application/json", b'{"error":"invalid file"}')
                return
            link_path = SESSIONS_DIR / file_name
            if not link_path.exists() or not link_path.is_file():
                self._send(404, "application/json", b'{"error":"not found"}')
                return
            # Symlinks point at real session logs outside this dir; follow them for reads.
            path = link_path.resolve()
            entries = load_session_entries(path)
            attributed_cost, prompt_chunks = prompt_cost_chunks(path)
            payload = {
                "file": file_name,
                "label": file_name,
                "resolved_path": str(path),
                "line_count": sum(1 for _ in path.open(encoding="utf-8", errors="replace")),
                "size_mb": f"{path.stat().st_size / (1024 * 1024):.1f}",
                "attributed_cost_usd": round(attributed_cost, 4),
                "prompt_chunks": prompt_chunks,
                "entries": entries,
            }
            self._send(200, "application/json", json.dumps(payload).encode())
            return
        self._send(404, "text/plain", b"not found")

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    if not SESSIONS_DIR.exists():
        print(f"Missing {SESSIONS_DIR}; run symlinks first.", file=sys.stderr)
        return 1
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(url, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
