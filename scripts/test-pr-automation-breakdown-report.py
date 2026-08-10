#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "pr_automation_breakdown_report.py"
spec = importlib.util.spec_from_file_location("pr_automation_breakdown_report", SCRIPT_PATH)
assert spec and spec.loader
report = importlib.util.module_from_spec(spec)
sys.modules["pr_automation_breakdown_report"] = report
spec.loader.exec_module(report)


def row(
    *,
    file: str,
    prompt_index: str,
    prompt_preview: str,
    cost: str,
    duration_seconds: str,
    session_date: str = "2026-08-01",
    host: str = "host-a",
    request_origin: str = "invoker_auto_fix",
    repo: str = "Neko-Catpital-Labs/Invoker",
) -> dict[str, str]:
    return {
        "model": "codex",
        "origin": "native",
        "host": host,
        "file": file,
        "session_date": session_date,
        "bucket": "execution",
        "prompt_index": prompt_index,
        "prompt_preview": prompt_preview,
        "first_prompt_preview": prompt_preview,
        "session_cwd": "/tmp/Invoker",
        "repo": repo,
        "request_origin": request_origin,
        "duration_seconds": duration_seconds,
        "derived_total_cost_usd": cost,
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class PrAutomationBreakdownTests(unittest.TestCase):
    def test_reports_dir_sets_default_input_csv(self) -> None:
        with patch(
            "sys.argv",
            [
                "pr_automation_breakdown_report.py",
                "--reports-dir",
                "/tmp/custom-reports",
            ],
        ):
            args = report.parse_args()

        self.assertEqual(args.input, "/tmp/custom-reports/planning-vs-execution-prompts.csv")

    def test_attempts_detail_and_uncapped_thrashing_targets(self) -> None:
        rows = [
            row(
                file="/tmp/session-a.jsonl",
                prompt_index="1",
                prompt_preview="Autofix for invoker Neko-Catpital-Labs/Invoker#6550 first row",
                cost="1.25",
                duration_seconds="5000",
                host="host-a",
            ),
            row(
                file="/tmp/session-a.jsonl",
                prompt_index="not-a-number",
                prompt_preview="Autofix for invoker Neko-Catpital-Labs/Invoker#6550 representative row",
                cost="4.75",
                duration_seconds="10",
                host="host-a",
            ),
            row(
                file="/tmp/session-b.jsonl",
                prompt_index="2",
                prompt_preview="Autofix for invoker Neko-Catpital-Labs/Invoker#6550 second session",
                cost="3.00",
                duration_seconds="30",
                host="host-b",
            ),
            row(
                file="/tmp/session-c.jsonl",
                prompt_index="9",
                prompt_preview="Autofix for invoker Neko-Catpital-Labs/Invoker#7000 single session",
                cost="2.00",
                duration_seconds="20",
            ),
            row(
                file="/tmp/session-d.jsonl",
                prompt_index="10",
                prompt_preview="Autofix for invoker Neko-Catpital-Labs/Invoker#8000 single session",
                cost="1.00",
                duration_seconds="15",
            ),
        ]

        window = report.build_window(rows, duration_cap_seconds=100.0)
        self.assertEqual(window["total_cost_usd"], 12.0)
        self.assertEqual(window["categories"][0]["prompts"], 5)
        self.assertEqual(window["categories"][0]["raw_seconds"], 5075.0)
        self.assertEqual(window["categories"][0]["active_seconds"], 175.0)
        first = window["thrash_targets"][0]
        self.assertEqual(first["attempts"], 2)
        self.assertEqual(len(first["attempts_detail"]), first["attempts"])
        self.assertEqual(first["attempts_detail"][0]["session_id"], "session-a")
        self.assertEqual(first["attempts_detail"][0]["prompt_index"], "not-a-number")
        self.assertEqual(first["attempts_detail"][0]["cost_usd"], 6.0)

        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            csv_path = tmpdir / "prompts.csv"
            json_path = tmpdir / "report.json"
            markdown_path = tmpdir / "report.md"
            html_path = tmpdir / "report.html"
            write_csv(csv_path, rows)
            args = argparse.Namespace(
                input=str(csv_path),
                json_out=str(json_path),
                markdown_out=str(markdown_path),
                html_out=str(html_path),
                top_thrash=1,
                duration_cap_seconds=100.0,
            )
            data = report.build_report(args)
            json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            markdown_path.write_text(report.render_markdown(data, args), encoding="utf-8")
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["thrash"]["top"]), 1)
            self.assertEqual(len(payload["thrash_targets"]), 3)
            self.assertEqual(len(payload["thrash_targets"][0]["attempts_detail"]), payload["thrash_targets"][0]["attempts"])
            self.assertEqual(markdown_path.read_text(encoding="utf-8").count("| `Neko-Catpital-Labs/Invoker#"), 1)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
