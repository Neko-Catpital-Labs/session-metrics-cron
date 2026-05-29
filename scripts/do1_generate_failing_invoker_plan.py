#!/usr/bin/env python3
"""Generate and validate the known failing Invoker plan on DO1.

This copies the benchmark conversation, runs real Codex with /plan-to-invoker,
extracts only the YAML plan, and validates it with the checkout-local
plan-to-invoker validator. It does not rewrite generated YAML.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    stdin: str | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    stdout_handle = stdout_path.open("w") if stdout_path else subprocess.PIPE
    stderr_handle = stderr_path.open("w") if stderr_path else subprocess.PIPE
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            input=stdin,
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            check=False,
        )
    finally:
        if stdout_path:
            stdout_handle.close()
        if stderr_path:
            stderr_handle.close()

    if check and completed.returncode != 0:
        cmd = " ".join(args)
        raise SystemExit(f"Command failed ({completed.returncode}): {cmd}")
    return completed


def looks_like_plan(text: str) -> bool:
    return bool(
        re.search(r"(?m)^name:\s*\S", text)
        and re.search(r"(?m)^repoUrl:\s*\S", text)
        and re.search(r"(?m)^tasks:\s*(?:$|\[)", text)
    )


def extract_yaml(raw: str) -> str | None:
    blocks = [
        match.group(1).strip() + "\n"
        for match in re.finditer(
            r"```(?:ya?ml|yaml|yml)?[^\n]*\n(.*?)```",
            raw,
            re.IGNORECASE | re.DOTALL,
        )
    ]

    window = ""
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^name:\s*\S", line):
            window = "\n".join(lines[index:]).strip() + "\n"
            break

    for candidate in [*blocks, window, raw.strip() + "\n"]:
        if candidate and looks_like_plan(candidate):
            return candidate
    return None


def summarize_plan(plan: str) -> dict[str, str | int]:
    task_count = sum(1 for line in plan.splitlines() if re.match(r"^\s{2}-\s+id:\s*", line))
    merge_mode_match = re.search(r"(?m)^mergeMode:\s*(\S+)", plan)
    merge_mode = merge_mode_match.group(1).strip().strip("\"'") if merge_mode_match else ""
    return {
        "task_count": task_count,
        "mergeMode": merge_mode,
        "has_runnerKind": "yes" if re.search(r"(?m)^\s*runnerKind:", plan) else "no",
    }


def main() -> int:
    bench_root = Path(env("DO1_BENCH_ROOT", "/home/invoker/invoker-benchmarks"))
    conversation_source = Path(
        env(
            "CONVERSATION_SOURCE",
            str(
                bench_root
                / "corpus/codex-autofix-vs-baseline-master-4f05-20260528-093313/master-4f05-docs-workflow.txt"
            ),
        )
    )
    invoker_repo = env("INVOKER_REPO", "https://github.com/Neko-Catpital-Labs/Invoker.git")
    invoker_ref = env("INVOKER_REF", "edbert/plan-to-invoker-runnerkind-compat")
    run_root = Path(
        env(
            "RUN_ROOT",
            str(bench_root / f"manual-reruns/manual-plan-generation-{datetime.now().strftime('%Y%m%d-%H%M%S')}"),
        )
    )

    if not conversation_source.is_file():
        print(f"Missing conversation source: {conversation_source}", file=sys.stderr)
        return 1
    if shutil.which("codex") is None:
        print("codex CLI not found on PATH", file=sys.stderr)
        return 1
    if shutil.which("git") is None:
        print("git not found on PATH", file=sys.stderr)
        return 1

    run_root.mkdir(parents=True, exist_ok=True)
    checkout = run_root / "checkout"
    conversation_copy = run_root / "conversation-copy.txt"
    codex_input_path = run_root / "codex-input.txt"
    model_output_path = run_root / "model-output.txt"
    model_stderr_path = run_root / "model-stderr.log"
    generated_plan_path = run_root / "generated-plan.yaml"

    print(f"run_root={run_root}")
    print(f"conversation_source={conversation_source}")
    print(f"invoker_repo={invoker_repo}")
    print(f"invoker_ref={invoker_ref}")

    shutil.copyfile(conversation_source, conversation_copy)

    run(
        ["git", "clone", "--branch", invoker_ref, invoker_repo, str(checkout)],
        stdout_path=run_root / "git-clone.log",
        stderr_path=run_root / "git-clone.log.stderr",
    )
    head = run(["git", "-C", str(checkout), "rev-parse", "HEAD"]).stdout.strip()
    (run_root / "invoker-head.txt").write_text(head + "\n")

    codex_input = "\n".join(
        [
            "/plan-to-invoker",
            "Use repository-local plan-to-invoker skill files from this checkout. Do not use stale globally installed skill references.",
            "",
            "For this benchmark, generate Invoker YAML with mergeMode: manual. Do not use mergeMode: github. Generate 5 to 10 meaningful tasks.",
            "",
            "Compatibility constraint: do not emit runnerKind anywhere. Omit routing fields for default worktree execution. Use poolId only for a configured pool, or dockerImage only for Docker tasks.",
            "",
            conversation_copy.read_text(errors="ignore"),
        ]
    )
    codex_input_path.write_text(codex_input)

    run(
        ["codex", "exec", "--skip-git-repo-check", "-"],
        cwd=checkout,
        stdin=codex_input,
        stdout_path=model_output_path,
        stderr_path=model_stderr_path,
    )

    raw_output = model_output_path.read_text(errors="ignore")
    yaml_text = extract_yaml(raw_output)
    if not yaml_text:
        print("No YAML plan found in model output.", file=sys.stderr)
        print(f"raw_model_output={model_output_path}", file=sys.stderr)
        return 1
    generated_plan_path.write_text(yaml_text)

    validate = run(
        ["bash", "skills/plan-to-invoker/scripts/validate-plan.sh", str(generated_plan_path)],
        cwd=checkout,
        stdout_path=run_root / "validate-stdout.log",
        stderr_path=run_root / "validate-stderr.log",
        check=False,
    )

    summary = summarize_plan(yaml_text)
    print(f"invoker_head={head}")
    print(f"generated_plan={generated_plan_path}")
    print(f"task_count={summary['task_count']}")
    print(f"mergeMode={summary['mergeMode']}")
    print(f"has_runnerKind={summary['has_runnerKind']}")
    print(f"validate_exit={validate.returncode}")

    if validate.returncode != 0:
        print(f"Plan validation failed. See: {run_root / 'validate-stderr.log'}", file=sys.stderr)
        return validate.returncode

    print("Plan generation complete.")
    print(f"Copied conversation: {conversation_copy}")
    print(f"Raw model output:    {model_output_path}")
    print(f"Generated plan:      {generated_plan_path}")
    print(f"Validation output:   {run_root / 'validate-stdout.log'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
