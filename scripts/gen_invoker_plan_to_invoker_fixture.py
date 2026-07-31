#!/usr/bin/env python3
"""Build synthetic Claude Code session JSONL fixtures for invoker_skill_run_timing_report.py.

Every scenario uses hand-picked, round second offsets from a fixed T0 so the
expected totals in tests are exact integers, not fuzzy approximations.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
SESSION_ID = "fixture-session-0001"

_uuid_counter = 0


def _ts(offset_seconds: float | None) -> str | None:
    if offset_seconds is None:
        return None
    return (T0 + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def _next_uuid() -> str:
    global _uuid_counter
    _uuid_counter += 1
    return f"fixture-uuid-{_uuid_counter:04d}"


def user_text_line(offset_seconds: float, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": _ts(offset_seconds),
        "uuid": _next_uuid(),
        "sessionId": SESSION_ID,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def assistant_line(
    offset_seconds: float | None,
    text: str | None = None,
    tool_uses: list[tuple[str, str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for tool_id, tool_name, tool_input in tool_uses or []:
        content.append({"type": "tool_use", "id": tool_id, "name": tool_name, "input": tool_input})
    return {
        "type": "assistant",
        "timestamp": _ts(offset_seconds),
        "uuid": _next_uuid(),
        "sessionId": SESSION_ID,
        "message": {"role": "assistant", "content": content},
    }


def tool_result_line(offset_seconds: float, tool_use_id: str, output: str = "ok") -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": _ts(offset_seconds),
        "uuid": _next_uuid(),
        "sessionId": SESSION_ID,
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": output}]},
    }


def scenario_happy_path() -> list[dict[str, Any]]:
    rows = [
        user_text_line(0, "/plan-to-invoker please generate the yaml plan"),
        assistant_line(5, text="Let me look at the plan first."),
        assistant_line(
            7,
            tool_uses=[
                ("tu_read1", "Read", {"file_path": "plans/source.md"}),
                ("tu_read2", "Read", {"file_path": "plans/notes.md"}),
            ],
        ),
        tool_result_line(8, "tu_read1"),
        tool_result_line(9, "tu_read2"),
        assistant_line(
            17,
            text="Running the skill-doctor gate.",
            tool_uses=[("tu_sd", "Bash", {"command": "bash skills/plan-to-invoker/scripts/skill-doctor.sh plans/foo.yaml"})],
        ),
        tool_result_line(47, "tu_sd", "all checks passed"),
        assistant_line(
            52,
            text="Extracting assumptions.",
            tool_uses=[
                ("tu_ea", "Bash", {"command": "bash skills/plan-to-invoker/scripts/extract-assumptions.sh plans/foo.yaml"})
            ],
        ),
        tool_result_line(64, "tu_ea", "assumptions ok"),
        assistant_line(
            67,
            text="Delegating deeper analysis.",
            tool_uses=[("tu_task", "Task", {"description": "deep analysis", "prompt": "review the plan"})],
        ),
        tool_result_line(97, "tu_task", "analysis complete"),
        assistant_line(
            102,
            text="Writing the final plan.",
            tool_uses=[("tu_write", "Write", {"file_path": "plans/invoker-handoff.yaml", "content": "name: demo\n"})],
        ),
        tool_result_line(104, "tu_write", "wrote file"),
    ]
    return rows


def scenario_unterminated() -> list[dict[str, Any]]:
    return [
        user_text_line(0, "/plan-to-invoker please generate the yaml plan"),
        assistant_line(5, text="Let me look at the plan first."),
        assistant_line(
            17,
            text="Running the skill-doctor gate.",
            tool_uses=[("tu_sd", "Bash", {"command": "bash skills/plan-to-invoker/scripts/skill-doctor.sh plans/foo.yaml"})],
        ),
    ]


def scenario_missing_timestamp() -> list[dict[str, Any]]:
    return [
        user_text_line(0, "/plan-to-invoker please generate the yaml plan"),
        assistant_line(5, text="Let me look at the plan first."),
        assistant_line(
            None,
            text="Running the skill-doctor gate.",
            tool_uses=[("tu_sd", "Bash", {"command": "bash skills/plan-to-invoker/scripts/skill-doctor.sh plans/foo.yaml"})],
        ),
        tool_result_line(40, "tu_sd", "all checks passed"),
        assistant_line(50, text="Continuing after the gate."),
        assistant_line(
            55,
            text="Extracting assumptions.",
            tool_uses=[
                ("tu_ea", "Bash", {"command": "bash skills/plan-to-invoker/scripts/extract-assumptions.sh plans/foo.yaml"})
            ],
        ),
        tool_result_line(67, "tu_ea", "assumptions ok"),
        assistant_line(
            72,
            text="Writing the final plan.",
            tool_uses=[("tu_write", "Write", {"file_path": "plans/invoker-handoff.yaml", "content": "name: demo\n"})],
        ),
        tool_result_line(74, "tu_write", "wrote file"),
    ]


def scenario_two_step_write() -> list[dict[str, Any]]:
    return [
        user_text_line(0, "/plan-to-invoker please generate the yaml plan"),
        assistant_line(
            5,
            text="Writing the markdown handoff first.",
            tool_uses=[("tu_write_md", "Write", {"file_path": "plans/invoker-handoff.md", "content": "# plan\n"})],
        ),
        tool_result_line(6, "tu_write_md", "wrote md"),
        assistant_line(
            10,
            text="Writing a draft yaml.",
            tool_uses=[("tu_write_yaml_draft", "Write", {"file_path": "plans/invoker-handoff.yaml", "content": "name: draft\n"})],
        ),
        tool_result_line(11, "tu_write_yaml_draft", "wrote draft yaml"),
        assistant_line(
            20,
            text="Writing the final yaml.",
            tool_uses=[("tu_write_yaml_final", "Write", {"file_path": "plans/invoker-handoff.yaml", "content": "name: final\n"})],
        ),
        tool_result_line(22, "tu_write_yaml_final", "wrote final yaml"),
    ]


def scenario_multiple_invocations() -> list[dict[str, Any]]:
    return [
        user_text_line(0, "/plan-to-invoker first attempt, please generate the yaml plan"),
        assistant_line(5, text="Working on the first attempt."),
        assistant_line(
            10,
            text="Actually let's restart.",
            tool_uses=[("tu_write_abandoned", "Write", {"file_path": "plans/scratch.txt", "content": "abandoned\n"})],
        ),
        tool_result_line(11, "tu_write_abandoned", "wrote scratch"),
        user_text_line(100, "/plan-to-invoker second attempt, retry with a cleaner scope"),
        assistant_line(105, text="Working on the second attempt."),
        assistant_line(
            110,
            text="Writing final yaml.",
            tool_uses=[("tu_write_final", "Write", {"file_path": "plans/invoker-handoff.yaml", "content": "name: final\n"})],
        ),
        tool_result_line(112, "tu_write_final", "wrote final yaml"),
    ]


SCENARIOS = {
    "happy_path": scenario_happy_path,
    "unterminated": scenario_unterminated,
    "missing_timestamp": scenario_missing_timestamp,
    "two_step_write": scenario_two_step_write,
    "multiple_invocations": scenario_multiple_invocations,
}


def build_fixture_lines(scenario: str) -> list[dict[str, Any]]:
    global _uuid_counter
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario {scenario!r}; choices: {sorted(SCENARIOS)}")
    _uuid_counter = 0
    return SCENARIOS[scenario]()


def render_jsonl(rows: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a synthetic invoker-plan-to-invoker session fixture.")
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    parser.add_argument("--out", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = build_fixture_lines(args.scenario)
    Path(args.out).write_text(render_jsonl(rows))
    print(f"Wrote {len(rows)} lines to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
