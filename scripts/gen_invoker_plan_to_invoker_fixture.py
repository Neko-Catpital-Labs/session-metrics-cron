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


# --------------------------------------------------------------------------
# Codex-shaped lines (response_item / event_msg), matching the real schema
# observed under ~/.codex/sessions on this machine.
# --------------------------------------------------------------------------


def codex_user_message_line(offset_seconds: float, text: str) -> dict[str, Any]:
    return {
        "timestamp": _ts(offset_seconds),
        "type": "event_msg",
        "payload": {"type": "user_message", "message": text, "images": [], "local_images": [], "text_elements": []},
    }


def codex_reasoning_line(offset_seconds: float) -> dict[str, Any]:
    return {
        "timestamp": _ts(offset_seconds),
        "type": "response_item",
        "payload": {"type": "reasoning", "summary": [], "encrypted_content": "opaque"},
    }


def codex_agent_message_line(offset_seconds: float, text: str) -> dict[str, Any]:
    return {
        "timestamp": _ts(offset_seconds),
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": text, "phase": "commentary", "memory_citation": None},
    }


def codex_function_call_line(offset_seconds: float, call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": _ts(offset_seconds),
        "type": "response_item",
        "payload": {"type": "function_call", "name": name, "arguments": json.dumps(arguments), "call_id": call_id},
    }


def codex_function_call_output_line(offset_seconds: float, call_id: str, output: str = "ok") -> dict[str, Any]:
    return {
        "timestamp": _ts(offset_seconds),
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": call_id, "output": output},
    }


def codex_custom_tool_call_line(offset_seconds: float, call_id: str, name: str, patch: str) -> dict[str, Any]:
    return {
        "timestamp": _ts(offset_seconds),
        "type": "response_item",
        "payload": {"type": "custom_tool_call", "status": "completed", "call_id": call_id, "name": name, "input": patch},
    }


def codex_custom_tool_call_output_line(offset_seconds: float, call_id: str, output: str = "ok") -> dict[str, Any]:
    return {
        "timestamp": _ts(offset_seconds),
        "type": "response_item",
        "payload": {"type": "custom_tool_call_output", "call_id": call_id, "output": output},
    }


def scenario_codex_happy_path() -> list[dict[str, Any]]:
    return [
        codex_user_message_line(0, "/invoker-plan-to-invoker please generate the yaml plan"),
        codex_reasoning_line(5),
        codex_agent_message_line(7, "I'll look at the plan first."),
        codex_function_call_line(
            15, "call_sd", "exec_command", {"cmd": "bash skills/plan-to-invoker/scripts/skill-doctor.sh plans/foo.yaml"}
        ),
        codex_function_call_output_line(45, "call_sd", "all checks passed"),
        codex_reasoning_line(50),
        codex_function_call_line(
            52,
            "call_ea",
            "exec_command",
            {"cmd": "bash skills/plan-to-invoker/scripts/extract-assumptions.sh plans/foo.yaml"},
        ),
        codex_function_call_output_line(64, "call_ea", "assumptions ok"),
        codex_agent_message_line(70, "Writing the final plan."),
        codex_custom_tool_call_line(
            72,
            "call_write",
            "apply_patch",
            "*** Begin Patch\n*** Add File: plans/invoker-handoff.yaml\n+name: demo\n*** End Patch",
        ),
        codex_custom_tool_call_output_line(74, "call_write", "Success. Updated the following files:\nA plans/invoker-handoff.yaml\n"),
    ]


def scenario_codex_multi_file_patch() -> list[dict[str, Any]]:
    return [
        codex_user_message_line(0, "/invoker-plan-to-invoker please generate the yaml plan"),
        codex_reasoning_line(3),
        codex_custom_tool_call_line(
            10,
            "call_write",
            "apply_patch",
            "*** Begin Patch\n"
            "*** Update File: docs/README.md\n"
            "+notes\n"
            "*** Add File: plans/invoker-handoff.yaml\n"
            "+name: demo\n"
            "*** End Patch",
        ),
        codex_custom_tool_call_output_line(
            12, "call_write", "Success. Updated the following files:\nM docs/README.md\nA plans/invoker-handoff.yaml\n"
        ),
    ]


def scenario_codex_parallel_calls() -> list[dict[str, Any]]:
    return [
        codex_user_message_line(0, "/invoker-plan-to-invoker please generate the yaml plan"),
        codex_reasoning_line(5),
        codex_agent_message_line(7, "Running three checks in parallel."),
        codex_function_call_line(
            8, "call_a", "exec_command", {"cmd": "bash skills/plan-to-invoker/scripts/validate-plan.sh plans/foo.yaml"}
        ),
        codex_function_call_line(
            8, "call_b", "exec_command", {"cmd": "bash skills/plan-to-invoker/scripts/check-policy-coverage.sh plans/foo.yaml"}
        ),
        codex_function_call_line(
            8, "call_c", "exec_command", {"cmd": "bash skills/plan-to-invoker/scripts/check-stack-manifest.sh plans/foo.yaml"}
        ),
        codex_function_call_output_line(10, "call_a", "validate ok"),
        codex_function_call_output_line(9, "call_b", "policy ok"),
        codex_function_call_output_line(15, "call_c", "manifest ok"),
        codex_agent_message_line(20, "All checks passed, writing the plan."),
        codex_custom_tool_call_line(
            22,
            "call_write",
            "apply_patch",
            "*** Begin Patch\n*** Add File: plans/invoker-handoff.yaml\n+name: demo\n*** End Patch",
        ),
        codex_custom_tool_call_output_line(24, "call_write", "Success. Updated the following files:\nA plans/invoker-handoff.yaml\n"),
    ]


SCENARIOS = {
    "happy_path": scenario_happy_path,
    "unterminated": scenario_unterminated,
    "missing_timestamp": scenario_missing_timestamp,
    "two_step_write": scenario_two_step_write,
    "multiple_invocations": scenario_multiple_invocations,
    "codex_happy_path": scenario_codex_happy_path,
    "codex_multi_file_patch": scenario_codex_multi_file_patch,
    "codex_parallel_calls": scenario_codex_parallel_calls,
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
