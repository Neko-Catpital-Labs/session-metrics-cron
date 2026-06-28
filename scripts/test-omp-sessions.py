#!/usr/bin/env python3
"""Deterministic tests for omp (Oh My Pi) session cost support.

Covers: model-family detection from the omp model prefix, per-turn usage/cost
aggregation into prompt windows, tool-call extraction, origin tagging, and the
origin x model rollup (omp+codex / omp+claude).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import planning_vs_execution_report as pve  # noqa: E402
from invoker_cost_breakdown_report import origin_model_rollup  # noqa: E402


def _session_lines(model: str, costs: list[float]) -> list[dict]:
    lines = [
        {"type": "session", "id": "s", "timestamp": "2026-06-20T10:00:00Z",
         "cwd": "/work/repo", "title": "t", "titleSource": "x", "version": 1},
        {"type": "message", "id": "u1", "timestamp": "2026-06-20T10:00:01Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "please fix the failing build"}]}},
        {"type": "message", "id": "a1", "timestamp": "2026-06-20T10:00:02Z",
         "message": {"role": "assistant", "model": model,
                     "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"cmd": "ls -la"}}],
                     "usage": {"input": 1000, "output": 50, "cacheRead": 200, "cacheWrite": 0,
                               "reasoningTokens": 10, "totalTokens": 1260,
                               "cost": {"input": 0.01, "output": 0.005, "cacheRead": 0.001, "cacheWrite": 0, "total": costs[0]}}}},
        {"type": "message", "id": "t1", "timestamp": "2026-06-20T10:00:03Z",
         "message": {"role": "toolResult", "toolCallId": "c1", "toolName": "bash",
                     "content": [{"type": "text", "text": "drwxr-xr-x output"}]}},
        {"type": "message", "id": "a2", "timestamp": "2026-06-20T10:00:04Z",
         "message": {"role": "assistant", "model": model,
                     "content": [{"type": "text", "text": "done"}],
                     "usage": {"input": 500, "output": 20, "cacheRead": 1200, "cacheWrite": 0,
                               "reasoningTokens": 5, "totalTokens": 1725, "cost": {"total": costs[1]}}}},
    ]
    return lines


def _write_session(directory: Path, name: str, model: str, costs: list[float]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("\n".join(json.dumps(o) for o in _session_lines(model, costs)) + "\n", encoding="utf-8")
    return path


def approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def main() -> int:
    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        codex_path = _write_session(root / "codex_cwd", "2026-codex.jsonl", "openai-codex/gpt-5.4", [0.016, 0.020])
        claude_path = _write_session(root / "claude_cwd", "2026-claude.jsonl", "anthropic/claude-opus-4-8", [0.040, 0.010])

        codex = pve.parse_omp_session(codex_path)
        claude = pve.parse_omp_session(claude_path)

        check(codex is not None and claude is not None, "parse returned None")
        assert codex and claude

        # Family detection from the omp model prefix.
        check(codex.model == "codex", f"codex family got {codex.model}")
        check(claude.model == "claude", f"claude family got {claude.model}")
        # Origin + cache semantics.
        check(codex.origin == "omp" and claude.origin == "omp", "origin not omp")
        check(codex.input_includes_cache is False, "omp input_includes_cache should be False")
        # Per-turn usage aggregated into the single prompt window.
        check(codex.final_input == 1500, f"final_input {codex.final_input}")
        check(codex.final_cached == 1400, f"final_cached {codex.final_cached}")
        check(codex.final_output == 70, f"final_output {codex.final_output}")
        check(codex.final_reasoning == 15, f"final_reasoning {codex.final_reasoning}")
        check(codex.tool_calls == 1, f"tool_calls {codex.tool_calls}")
        check(codex.function_outputs == 1, f"function_outputs {codex.function_outputs}")
        check(len(codex.prompt_windows) == 1, f"windows {len(codex.prompt_windows)}")
        win = codex.prompt_windows[0]
        check(approx(win.omp_prompt_cost_usd, 0.036), f"omp window cost {win.omp_prompt_cost_usd}")
        check("ls" in codex.shell_verb_counts, f"shell verbs {dict(codex.shell_verb_counts)}")

        # Rows: omp origin, exact omp cost wins over pricing-derived (empty pricing table).
        _sr, prompt_rows, attribution_rows, _cr = pve.build_omp_rows([codex, claude], {})
        check(all(r["origin"] == "omp" for r in prompt_rows), "prompt rows not all omp")
        by_model = {r["model"]: r for r in prompt_rows}
        check(approx(float(by_model["codex"]["derived_total_cost_usd"]), 0.036), f"codex prompt cost {by_model['codex']['derived_total_cost_usd']}")
        check(approx(float(by_model["claude"]["derived_total_cost_usd"]), 0.050), f"claude prompt cost {by_model['claude']['derived_total_cost_usd']}")
        check(any(r["origin"] == "omp" for r in attribution_rows), "no omp attribution rows")

        # Origin x model rollup as the breakdown page consumes it.
        rollup = origin_model_rollup(prompt_rows)
        labels = {r["label"]: r for r in rollup["rows"]}
        check("omp+codex" in labels and "omp+claude" in labels, f"rollup labels {list(labels)}")
        check(approx(labels["omp+codex"]["cost_usd"], 0.036), f"rollup codex {labels['omp+codex']['cost_usd']}")
        check(approx(labels["omp+claude"]["cost_usd"], 0.050), f"rollup claude {labels['omp+claude']['cost_usd']}")
        check(approx(rollup["grand_total"]["cost_usd"], 0.086), f"grand {rollup['grand_total']['cost_usd']}")

        # Native rows keep origin=native (regression: existing codex/claude path).
        native = pve.SessionStats(model="codex", file="x", session_date="2026-06-20", bucket="execution")
        check(native.origin == "native", "default origin should be native")

    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("OK - all omp session cost checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
