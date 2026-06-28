#!/usr/bin/env python3
"""Collect codex + claude + omp sessions across the whole fleet (local + SSH
hosts) and emit the prompt / tool-attribution CSVs the cost breakdown renders.

Costing:
- omp sessions carry exact per-turn cost -> used directly.
- native codex/claude sessions carry token usage only; cost is computed from
  per-component unit rates derived from omp's own (model -> $/token) data, so the
  whole fleet is costed consistently with no external pricing/ccusage dependency.

Hosts come from ~/.invoker/config.json (remoteTargets) plus the local machine.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import planning_vs_execution_report as pve  # noqa: E402

INVOKER_CONFIG = Path.home() / ".invoker" / "config.json"
DEFAULT_KEY = Path.home() / ".ssh" / "id_ed25519"


def load_hosts(include_local: bool) -> list[dict[str, Any]]:
    hosts: list[dict[str, Any]] = []
    if include_local:
        hosts.append({"name": "local", "local": True})
    try:
        cfg = json.loads(INVOKER_CONFIG.read_text())
    except OSError:
        cfg = {}
    for name, target in (cfg.get("remoteTargets") or {}).items():
        host = target.get("host") or target.get("hostname")
        if not host:
            continue
        hosts.append({
            "name": name,
            "local": False,
            "host": host,
            "user": target.get("user") or target.get("username") or "invoker",
            "key": target.get("sshKeyPath") or target.get("identityFile") or str(DEFAULT_KEY),
        })
    return hosts


def rsync_dir(host: dict[str, Any], remote_dir: str, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    ssh = f"ssh -i {host['key']} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes"
    src = f"{host['user']}@{host['host']}:{remote_dir}/"
    cmd = ["rsync", "-az", "--include", "*/", "--include", "*.jsonl", "--exclude", "*", "-e", ssh, src, str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode not in (0, 23, 24):  # 23/24 = partial/vanished, tolerable
        print(f"  rsync {host['name']} {remote_dir} rc={proc.returncode}: {proc.stderr.strip()[:160]}", file=sys.stderr)
    return len(list(dest.rglob("*.jsonl")))


def host_dirs(host: dict[str, Any], stage: Path) -> dict[str, Path | None]:
    """Return {codex, claude, omp} dirs for a host (local real dirs or staged)."""
    if host.get("local"):
        return {
            "codex": Path.home() / ".codex" / "sessions",
            "claude": Path.home() / ".claude" / "projects",
            "omp": Path.home() / ".omp" / "agent" / "sessions",
        }
    base = stage / host["name"]
    out: dict[str, Path | None] = {}
    for kind, remote in (("codex", "~/.codex/sessions"), ("claude", "~/.claude/projects"), ("omp", "~/.omp/agent/sessions")):
        remote_dir = remote.replace("~", f"/home/{host['user']}")
        dest = base / kind
        n = rsync_dir(host, remote_dir, dest)
        out[kind] = dest if n else None
    return out


def derive_family_rates(omp_files: list[Path]) -> dict[str, dict[str, float]]:
    """Per-family per-component $/token from omp's exact cost data."""
    cost: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    toks: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for fp in omp_files:
        try:
            text = fp.read_text(errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or '"usage"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            fam = pve._omp_model_family(msg.get("model") or "")
            if not fam:
                continue
            usage = msg.get("usage") or {}
            comp_cost = usage.get("cost") or {}
            for comp in ("input", "output", "cacheRead", "cacheWrite"):
                toks[fam][comp] += float(usage.get(comp) or 0)
                cost[fam][comp] += float(comp_cost.get(comp) or 0)
    rates: dict[str, dict[str, float]] = {}
    for fam in toks:
        rates[fam] = {c: (cost[fam][c] / toks[fam][c]) if toks[fam][c] else 0.0 for c in ("input", "output", "cacheRead", "cacheWrite")}
    return rates


def native_window_cost(ss: Any, w: Any, rates: dict[str, dict[str, float]]) -> float:
    r = rates.get(ss.model) or {}
    rin, rcr, rcw, rout = r.get("input", 0.0), r.get("cacheRead", 0.0), r.get("cacheWrite", 0.0), r.get("output", 0.0)
    if ss.input_includes_cache:
        billable_input = max(0, w.input_delta - w.cached_delta - w.cache_creation_delta)
    else:
        billable_input = w.input_delta
    return (
        billable_input * rin
        + w.cached_delta * rcr
        + w.cache_creation_delta * rcw
        + (w.output_delta + w.reasoning_delta) * rout
    )


def emit_rows(ss: Any, host: str, rates: dict[str, dict[str, float]],
              prompt_rows: list[dict[str, Any]], attribution_rows: list[dict[str, Any]]) -> None:
    for w in ss.prompt_windows:
        cost = w.omp_prompt_cost_usd if ss.origin == "omp" else native_window_cost(ss, w, rates)
        total_tokens = w.total_delta or (w.input_delta + w.cached_delta + w.cache_creation_delta + w.output_delta + w.reasoning_delta)
        prompt_rows.append({
            "model": ss.model,
            "origin": ss.origin,
            "host": host,
            "file": ss.file,
            "session_date": ss.session_date,
            "bucket": ss.bucket,
            "billable_model": ss.billable_model,
            "session_cwd": ss.session_cwd,
            "prompt_index": w.prompt_index,
            "prompt_preview": pve.shorten(w.prompt_text, 220),
            "first_prompt_preview": pve.shorten(ss.first_prompt, 220),
            "input_tokens_delta": w.input_delta,
            "cache_read_tokens_delta": w.cached_delta,
            "cache_creation_tokens_delta": w.cache_creation_delta,
            "output_tokens_delta": w.output_delta,
            "reasoning_tokens_delta": w.reasoning_delta,
            "total_tokens_delta": total_tokens,
            "derived_total_cost_usd": cost,
            "estimated_cost_usd": cost,
            "pricing_missing": False,
        })
        for dimension, counts in (("function_name", w.function_name_counts), ("shell_verb", w.shell_verb_counts)):
            total_calls = sum(counts.values())
            if not total_calls:
                continue
            for name, calls in counts.items():
                share = calls / total_calls
                attribution_rows.append({
                    "model": ss.model,
                    "origin": ss.origin,
                    "host": host,
                    "file": ss.file,
                    "session_date": ss.session_date,
                    "bucket": ss.bucket,
                    "prompt_index": w.prompt_index,
                    "dimension": dimension,
                    "name": name,
                    "calls": calls,
                    "allocated_total_cost_usd": cost * share,
                    "allocated_total_tokens": total_tokens * share,
                })


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("empty\n")
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-dir", default="/tmp/fleet-sessions")
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "reports"))
    parser.add_argument("--local-only", action="store_true", help="skip SSH hosts (debug)")
    parser.add_argument("--no-collect", action="store_true", help="reuse an existing stage (skip rsync)")
    args = parser.parse_args()

    stage = Path(args.stage_dir)
    hosts = load_hosts(include_local=True)
    if args.local_only:
        hosts = [h for h in hosts if h.get("local")]

    parsers = {"codex": pve.parse_codex_session, "claude": pve.parse_claude_session, "omp": pve.parse_omp_session}
    sessions: list[tuple[Any, str]] = []
    omp_files: list[Path] = []

    for host in hosts:
        name = host["name"]
        if host.get("local") or args.no_collect:
            dirs = host_dirs(host, stage) if host.get("local") else {
                k: (stage / name / k if (stage / name / k).exists() else None) for k in ("codex", "claude", "omp")
            }
        else:
            print(f"collecting {name} ...", flush=True)
            dirs = host_dirs(host, stage)
        counts = {}
        for kind, parser in parsers.items():
            d = dirs.get(kind)
            if not d or not Path(d).exists():
                counts[kind] = 0
                continue
            n = 0
            for fp in sorted(Path(d).rglob("*.jsonl")):
                if kind == "omp":
                    omp_files.append(fp)
                ss = parser(fp)
                if ss:
                    sessions.append((ss, name))
                    n += 1
            counts[kind] = n
        print(f"  {name}: codex={counts['codex']} claude={counts['claude']} omp={counts['omp']}", flush=True)

    rates = derive_family_rates(omp_files)
    print("family rates ($/Mtok):", {f: {c: round(v * 1e6, 3) for c, v in r.items()} for f, r in rates.items()}, flush=True)

    prompt_rows: list[dict[str, Any]] = []
    attribution_rows: list[dict[str, Any]] = []
    for ss, name in sessions:
        emit_rows(ss, name, rates, prompt_rows, attribution_rows)

    out = Path(args.out_dir)
    write_csv(out / "planning-vs-execution-prompts.csv", prompt_rows)
    write_csv(out / "planning-vs-execution-tool-attribution.csv", attribution_rows)
    total = sum(r["derived_total_cost_usd"] for r in prompt_rows)
    print(f"sessions={len(sessions)} prompts={len(prompt_rows)} tool_rows={len(attribution_rows)} total_cost=${total:,.2f}")
    print(f"wrote {out}/planning-vs-execution-prompts.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
