#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "splitter_metric_tree_app.py"
spec = importlib.util.spec_from_file_location("splitter_metric_tree_app", SCRIPT_PATH)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
sys.modules["splitter_metric_tree_app"] = app
spec.loader.exec_module(app)


class SplitterMetricTreeAppTests(unittest.TestCase):
    def test_query_from_params_defaults_and_caps_history(self) -> None:
        query = app.query_from_params({"history_runs": ["9999"]})

        self.assertEqual(query.metric_path, "planToResponseGraphScore")
        self.assertEqual(query.variant, "hinted")
        self.assertEqual(query.history_runs, app.MAX_HISTORY_RUNS)

    def test_query_from_params_rejects_bad_metric_path(self) -> None:
        with self.assertRaises(ValueError):
            app.query_from_params({"metric_path": ["foo; DROP TABLE x"]})

    def test_query_from_params_rejects_bad_variant(self) -> None:
        with self.assertRaises(ValueError):
            app.query_from_params({"variant": ["all"]})

    def test_sql_does_not_select_weighted_contribution_fields(self) -> None:
        latest_sql = app.parameterized_sql(app.latest_tree_sql("project.dataset.table"))
        history_sql = app.parameterized_sql(app.history_sql("project.dataset.table"))

        app.assert_no_weighted_fields(latest_sql)
        app.assert_no_weighted_fields(history_sql)
        self.assertNotIn("weighted_value", latest_sql.lower())
        self.assertNotIn("weighted_contribution", latest_sql.lower())
        self.assertIn("nodes.head_sha", latest_sql)
        self.assertIn("run_id", history_sql)
        self.assertIn("head_sha", history_sql)
        self.assertIn("effective_weight", history_sql)
        self.assertIn("display_value", latest_sql)
        self.assertIn("diagnostic_value", latest_sql)
        self.assertIn("is_score", latest_sql)
        self.assertIn("display_unit", history_sql)
        self.assertIn("@metric_path", latest_sql)
        self.assertIn("@variant", latest_sql)

    def test_literal_sql_escapes_params_for_metabase_backend(self) -> None:
        query = app.MetricTreeQuery(metric_path="root.child", variant="hinted", history_runs=20)
        sql = app.literal_sql(app.latest_tree_sql("project.dataset.table"), query)

        self.assertIn("'root.child'", sql)
        self.assertIn("'hinted'", sql)
        self.assertNotIn("@metric_path", sql)

    def test_run_metabase_dataset_shapes_rows(self) -> None:
        calls: list[dict[str, Any]] = []

        def fake_request(url: str, api_key: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            calls.append(payload)
            return {
                "data": {
                    "cols": [{"name": "metric_path"}, {"name": "score"}],
                    "rows": [["root", 0.9]],
                }
            }

        original = app.metabase_request
        app.metabase_request = fake_request
        try:
            rows = app.run_metabase_dataset("https://metabase.example", "key", 2, "SELECT 1 AS score")
        finally:
            app.metabase_request = original

        self.assertEqual(rows, [{"metric_path": "root", "score": 0.9}])
        self.assertEqual(calls[0]["database"], 2)

    def test_warehouse_sql_builders_reference_table_and_columns(self) -> None:
        intent_sql = app.warehouse_by_intent_sql("proj.ds.command_costs")
        self.assertIn("`proj.ds.command_costs`", intent_sql)
        self.assertIn("agent_tool_intention AS intent", intent_sql)
        self.assertIn("SUM(allocated_total_cost_usd)", intent_sql)
        cache_sql = app.warehouse_cache_sql("proj.ds.command_costs")
        self.assertIn("allocated_cache_read_tokens", cache_sql)
        self.assertIn("allocated_fresh_input_tokens", cache_sql)
        self.assertIn("session_date AS date", cache_sql)

    def test_warehouse_sql_applies_date_filter(self) -> None:
        no_filter = app.warehouse_by_intent_sql("p.d.t")
        self.assertNotIn("WHERE", no_filter)
        filtered = app.warehouse_by_intent_sql("p.d.t", "2026-04-01", "2026-04-30")
        self.assertIn("session_date >= '2026-04-01'", filtered)
        self.assertIn("session_date <= '2026-04-30'", filtered)
        cache_filtered = app.warehouse_cache_sql("p.d.t", None, "2026-05-29")
        self.assertIn("session_date <= '2026-05-29'", cache_filtered)
        self.assertNotIn("session_date >=", cache_filtered)
        fixing_sql = app.warehouse_fixing_causes_sql("p.d.t", "2026-04-01", "2026-04-30")
        self.assertIn("Failure diagnosis thrash", fixing_sql)
        self.assertIn("CI/merge monitoring thrash", fixing_sql)
        ci_sql = app.warehouse_ci_branch_sql("p.d.t")
        self.assertIn("ci_merge_monitoring", ci_sql)
        self.assertIn("branch_stack", ci_sql)
        phase_sql = app.warehouse_phase_efficiency_sql("p.d.t")
        self.assertIn("workflow_phase", phase_sql)
        self.assertIn("efficiency_label", phase_sql)

    def test_fixing_payloads_shape_rows(self) -> None:
        causes = app.fixing_causes_payload([
            {"cause": "CI/merge monitoring thrash", "cost_usd": 10.0, "tokens": 100.0, "commands": 5},
            {"cause": "Failure diagnosis thrash", "cost_usd": 20.0, "tokens": 200.0, "commands": 7},
        ])
        self.assertEqual(causes["total_cost_usd"], 30.0)
        self.assertAlmostEqual(causes["rows"][0]["share_pct"], 66.67, places=2)
        phase = app.phase_efficiency_payload([
            {"workflow_phase": "repair_loop", "efficiency_label": "thrash", "cost_usd": 3.0, "tokens": 30.0, "commands": 2},
        ])
        self.assertEqual(phase["rows"][0]["workflow_phase"], "repair_loop")
        ci = app.ci_branch_payload([
            {"workflow_phase": "ci_merge_monitoring", "intent": "ci_monitoring", "motivation": "branch_stack", "cost_usd": 4.0, "tokens": 40.0, "commands": 1},
        ])
        self.assertEqual(ci["total_cost_usd"], 4.0)
        self.assertEqual(ci["by_intent"][0]["intent"], "ci_monitoring")

    def test_sanitize_date_accepts_valid_rejects_garbage(self) -> None:
        self.assertEqual(app.sanitize_date("2026-04-01"), "2026-04-01")
        self.assertEqual(app.sanitize_date("  2026-12-31 "), "2026-12-31")
        self.assertIsNone(app.sanitize_date("0000-00-00"))
        self.assertIsNone(app.sanitize_date("2026/04/01"))
        self.assertIsNone(app.sanitize_date("2026-13-01"))
        self.assertIsNone(app.sanitize_date("'; DROP TABLE x"))
        self.assertIsNone(app.sanitize_date(None))

    def test_usage_by_intent_payload_sorts_by_cost_and_totals(self) -> None:
        payload = app.usage_by_intent_payload([
            {"intent": "a", "cost_usd": 1.5, "tokens": 10, "commands": 2},
            {"intent": "b", "cost_usd": 3.0, "tokens": 20, "commands": 5},
        ])
        self.assertEqual([r["intent"] for r in payload["rows"]], ["b", "a"])
        self.assertEqual(payload["total_cost_usd"], 4.5)

    def test_cache_hit_payload_computes_pct_overall_and_range(self) -> None:
        payload = app.cache_hit_payload([
            {"date": "2026-05-02", "fresh_input": 100, "cache_read": 900, "cache_creation": 50, "output": 10},
            {"date": "2026-05-01", "fresh_input": 0, "cache_read": 0, "cache_creation": 0, "output": 0},
        ])
        row = next(r for r in payload["rows"] if r["date"] == "2026-05-02")
        self.assertEqual(row["cache_hit_pct"], 90.0)
        self.assertEqual(payload["overall_cache_hit_pct"], 90.0)
        self.assertEqual(payload["date_range"], ["2026-05-01", "2026-05-02"])

    def test_run_warehouse_query_uses_metabase_backend(self) -> None:
        captured: list[dict[str, Any]] = []

        def fake_request(url: str, api_key: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            captured.append(payload)
            return {"data": {"cols": [{"name": "intent"}], "rows": [["x"]]}}

        original = app.metabase_request
        app.metabase_request = fake_request
        try:
            rows = app.run_warehouse_query(
                "SELECT 1",
                backend="metabase",
                project_id="p",
                metabase_url="u",
                metabase_api_key="k",
                metabase_database_id=7,
            )
        finally:
            app.metabase_request = original
        self.assertEqual(rows, [{"intent": "x"}])
        self.assertEqual(captured[0]["database"], 7)


    def test_cost_explorer_loaders_read_summary_index_and_window_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            windows_dir = root / "windows"
            windows_dir.mkdir()
            summary = {
                "headline_totals": {"total_attributed_cost_usd": 12.5, "prompt_window_count": 1, "command_count": 3},
                "fixing_causes": [],
            }
            window_payload = {
                "session_id": "session-a",
                "prompt_index": "2",
                "timeline": [{"start_command_index": 3, "end_command_index": 5, "fixing_cause": "Repeated repair/test loops"}],
                "commands": [{"command_index": 3, "preview": "pnpm test"}],
                "fixing_cause_rollup": [{"cause": "Repeated repair/test loops", "cost_usd": 2.5, "cost_pct": 50.0, "events": 2}],
            }
            (root / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
            (root / "windows.csv").write_text(
                "session_date,session_id,prompt_index,window_file\n2026-07-15,session-a,2,session-a-p2.json\n",
                encoding="utf-8",
            )
            (windows_dir / "session-a-p2.json").write_text(json.dumps(window_payload), encoding="utf-8")
            cache = app.TTLCache(60)
            loaded_summary = app.load_cost_explorer_summary(root, cache)
            loaded_index = app.load_cost_explorer_windows_index(root, cache)
            loaded_window = app.load_cost_explorer_window(root, "session-a-p2.json", cache)
        self.assertEqual(loaded_summary["headline_totals"]["prompt_window_count"], 1)
        self.assertEqual(loaded_index[0]["window_file"], "session-a-p2.json")
        self.assertEqual(loaded_window["timeline"][0]["fixing_cause"], "Repeated repair/test loops")
        self.assertEqual(loaded_window["commands"][0]["preview"], "pnpm test")

    def test_cost_explorer_summary_loader_rejects_stale_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "summary.json").write_text(json.dumps({"windows": [{"session_id": "old-shape"}]}), encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                app.load_cost_explorer_summary(root, app.TTLCache(60))

    def test_cost_explorer_limit_and_table_defaults(self) -> None:
        self.assertEqual(app.clamp_cost_explorer_limit(None), 50)
        self.assertEqual(app.clamp_cost_explorer_limit("500"), 200)
        self.assertEqual(app.clamp_cost_explorer_offset(None), 0)
        self.assertEqual(app.clamp_cost_explorer_offset("-5"), 0)
        self.assertEqual(app.derive_cost_explorer_table("proj.ds.command_costs"), "proj.ds.cost_explorer_commands_v1")
        with self.assertRaises(ValueError):
            app.clamp_cost_explorer_limit("oops")

    def test_warehouse_routes_registered(self) -> None:
        source = SCRIPT_PATH.read_text()
        self.assertIn('"/api/usage-by-intent"', source)
        self.assertIn('"/api/cache-hit"', source)
        self.assertIn('"/fixing-cost"', source)
        self.assertIn('"/cost-explorer"', source)
        self.assertIn('"/api/fixing-causes"', source)
        self.assertIn('"/api/ci-branch-summary"', source)
        self.assertIn('"/api/cost-explorer-summary"', source)
        self.assertIn('"/api/cost-explorer-search"', source)
        self.assertIn('"/api/cost-explorer-window"', source)

    def test_fixing_and_explorer_dashboards_include_new_sections(self) -> None:
        fixing_html = (REPO_ROOT / "docs" / "fixing-cost-dashboard.html").read_text()
        self.assertIn("Fixing / CI issues", fixing_html)
        self.assertIn("Task categories", fixing_html)
        self.assertIn("Request patterns", fixing_html)
        self.assertIn("Tool hotspots", fixing_html)
        self.assertIn("Token composition", fixing_html)
        self.assertIn("Legacy warehouse charts", fixing_html)
        explorer_html = (REPO_ROOT / "docs" / "cost-explorer.html").read_text()
        self.assertIn('id=\"explorerFilters\"', explorer_html)
        self.assertIn('id=\"explorerResults\"', explorer_html)
        self.assertIn('id=\"explorerDetail\"', explorer_html)
        self.assertIn('id=\"chunkTimeline\"', explorer_html)
        self.assertIn('#chunkTimeline { display:flex; flex-direction:column; gap:.8rem; width:100%; margin:.75rem 0 1rem; overflow:visible;', explorer_html)
        self.assertIn('.chunk-card { width:100%; border:1px solid var(--line); border-radius:12px; background:var(--bg); }', explorer_html)
        self.assertIn('.chunk-summary { padding:.75rem .8rem; cursor:pointer; }', explorer_html)
        self.assertIn('.chunk-summary-text { margin:.35rem 0 0; font-size:.9rem; }', explorer_html)
        self.assertIn('.chunk-detail-list { margin:.45rem 0 0; padding-left:1.1rem; color:var(--sub); font-size:.82rem; }', explorer_html)
        self.assertIn('.chunk-body[hidden] { display:none; }', explorer_html)
        self.assertIn('class=\"chunk-body\" ${isActive ? \'\' : \'hidden\'}', explorer_html)
        self.assertIn('function summarizeChunk(payload, chunk){', explorer_html)
        self.assertIn('Session steps (cost + summary)', explorer_html)
        self.assertIn('User asked: ${escapeHtml(chunkSummary.userAsked)}', explorer_html)
        self.assertIn('Agent activity:', explorer_html)
        self.assertIn('Work type:', explorer_html)
        self.assertIn('Example command:', explorer_html)
        self.assertIn('function conversationLabel(entry){', explorer_html)
        self.assertIn('function renderConversationBlock(chunk){', explorer_html)
        self.assertIn('chunk.conversation_entries || []', explorer_html)
        self.assertIn('const transcript = entries.map(entry => `${conversationLabel(entry)}\\n${entry.text || ""}`).join("\\n\\n");', explorer_html)
        self.assertIn('class=\"conversation-transcript\"', explorer_html)
        self.assertNotIn('class=\"conversation-list\"', explorer_html)
        self.assertIn('No conversation captured for this bucket.', explorer_html)
        self.assertIn('data-chunk-index="${index}" aria-expanded="${isActive ? \'true\' : \'false\'}"', explorer_html)
        self.assertIn("selectedChunkIndex = selectedChunkIndex === chunkIndex ? -1 : chunkIndex;", explorer_html)
        self.assertIn("Expand", explorer_html)
        self.assertIn("Collapse", explorer_html)
        self.assertIn('function showRequestPatternAcrossSessions(requestPattern){', explorer_html)
        self.assertIn('data-request-pattern-jump="${escapeHtml(payload.request_pattern)}"', explorer_html)
        self.assertIn('state.issue_kind = "request_pattern";', explorer_html)
        self.assertIn('state.issue_value = requestPattern;', explorer_html)
        self.assertIn('state.request_pattern = requestPattern;', explorer_html)
        self.assertIn('document.getElementById("issueValue").value = state.issue_value;', explorer_html)
        self.assertIn('function loadStaticBootstrap(){', explorer_html)
        self.assertIn('window.__COST_EXPLORER_STATIC_SUMMARY__', explorer_html)
        self.assertIn('window.__COST_EXPLORER_STATIC_WINDOW_ROWS__', explorer_html)
        self.assertIn('window.__COST_EXPLORER_STATIC_COMMAND_ROWS__', explorer_html)
        self.assertIn('await loadScriptOnce("./summary.js");', explorer_html)
        self.assertIn('await loadScriptOnce(`./windows-js/${windowFile}.js`);', explorer_html)
        self.assertIn('Open the generated explorer.html file from a cost-explorer output folder to load local data.', explorer_html)
        self.assertNotIn('Failed to load explorer summary: ${error.message}', explorer_html)

    def test_normalize_rows_keeps_score_and_weights(self) -> None:
        rows = app.normalize_rows(
            [
                {
                    "collected_at": "2026-06-05T14:35:08Z",
                    "run_id": "20260605T143508Z-00bc82e8b929",
                    "branch": "main",
                    "head_sha": "00bc82e8b929c7abeda87e65e66dfa769d5d3213",
                    "variant": "hinted",
                    "root_metric_id": "planToResponseGraphScore",
                    "metric_path": "planToResponseGraphScore.workflowRealizationScore",
                    "parent_metric_path": "planToResponseGraphScore",
                    "parent_metric_id": "planToResponseGraphScore",
                    "metric_id": "workflowRealizationScore",
                    "kind": "composite",
                    "depth": 1,
                    "relative_depth": 1,
                    "score": 0.9522,
                    "display_value": 0.9522,
                    "display_unit": "score",
                    "is_score": True,
                    "local_weight_pct": 45.0,
                    "effective_weight_pct": 45.0,
                    "description": "Measures whether intended plan nodes became response workflows.",
                    "why": "Low values mean workflow generation changed the planned work shape.",
                }
            ]
        )

        self.assertEqual(rows[0]["score"], 0.9522)
        self.assertEqual(rows[0]["display_value"], 0.9522)
        self.assertTrue(rows[0]["is_score"])
        self.assertEqual(rows[0]["tree_path"], rows[0]["metric_path"])
        self.assertEqual(rows[0]["local_weight_pct"], 45.0)
        self.assertEqual(rows[0]["effective_weight_pct"], 45.0)
        self.assertEqual(rows[0]["short_sha"], "00bc82e8b929")
        self.assertNotIn("weighted_value", rows[0])

    def test_normalize_rows_nests_diagnostics_under_explained_score(self) -> None:
        rows = app.normalize_rows(
            [
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore",
                    "parent_metric_path": "root",
                    "metric_id": "responseStackLinkCorrectnessScore",
                    "kind": "composite",
                    "relative_depth": 0,
                    "is_score": True,
                    "score": 0.63,
                    "display_value": 0.63,
                    "display_unit": "score",
                },
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore.responseCorrectStackLinksScore",
                    "parent_metric_path": "root.responseStackLinkCorrectnessScore",
                    "metric_id": "responseCorrectStackLinksScore",
                    "kind": "leaf",
                    "relative_depth": 1,
                    "is_score": True,
                    "score": 0.63,
                    "display_value": 0.63,
                    "display_unit": "score",
                },
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore.responseCorrectStackLinkCount",
                    "parent_metric_path": "root.responseStackLinkCorrectnessScore",
                    "metric_id": "responseCorrectStackLinkCount",
                    "kind": "diagnostic",
                    "relative_depth": 1,
                    "is_score": False,
                    "score": None,
                    "diagnostic_value": 2.55,
                    "display_value": 2.55,
                    "display_unit": "avg_count",
                },
            ]
        )

        count = next(row for row in rows if row["metric_id"] == "responseCorrectStackLinkCount")
        self.assertEqual(
            count["tree_parent_path"],
            "root.responseStackLinkCorrectnessScore.responseCorrectStackLinksScore",
        )
        self.assertEqual(
            count["tree_path"],
            "root.responseStackLinkCorrectnessScore.responseCorrectStackLinksScore.responseCorrectStackLinkCount",
        )
        self.assertEqual(count["tree_relative_depth"], 2)
        self.assertEqual(count["display_value"], 2.55)
        self.assertEqual(count["display_unit"], "avg_count")

    def test_normalize_rows_adds_stack_link_formula_explanations(self) -> None:
        rows = app.normalize_rows(
            [
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore.responseCorrectStackLinksScore",
                    "parent_metric_path": "root.responseStackLinkCorrectnessScore",
                    "metric_id": "responseCorrectStackLinksScore",
                    "kind": "leaf",
                    "relative_depth": 1,
                    "is_score": True,
                    "score": 0.63,
                    "display_value": 0.63,
                    "display_unit": "score",
                },
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore.responseCorrectStackLinkCount",
                    "parent_metric_path": "root.responseStackLinkCorrectnessScore",
                    "metric_id": "responseCorrectStackLinkCount",
                    "kind": "diagnostic",
                    "relative_depth": 1,
                    "is_score": False,
                    "score": None,
                    "diagnostic_value": 2.55,
                    "display_value": 2.55,
                    "display_unit": "avg_count",
                },
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore.responseExtraStackLinkCount",
                    "parent_metric_path": "root.responseStackLinkCorrectnessScore",
                    "metric_id": "responseExtraStackLinkCount",
                    "kind": "diagnostic",
                    "relative_depth": 1,
                    "is_score": False,
                    "diagnostic_value": 1.95,
                    "display_value": 1.95,
                    "display_unit": "avg_count",
                },
                {
                    "metric_path": "root.responseStackLinkCorrectnessScore.responseMissingStackLinkCount",
                    "parent_metric_path": "root.responseStackLinkCorrectnessScore",
                    "metric_id": "responseMissingStackLinkCount",
                    "kind": "diagnostic",
                    "relative_depth": 1,
                    "is_score": False,
                    "diagnostic_value": 1.95,
                    "display_value": 1.95,
                    "display_unit": "avg_count",
                },
            ]
        )

        score = next(row for row in rows if row["metric_id"] == "responseCorrectStackLinksScore")
        inputs = {item["label"]: item["value"] for item in score["explanation"]["inputs"]}
        self.assertEqual(score["explanation"]["formula"]["numerator"], "correct links")
        self.assertEqual(
            score["explanation"]["formula"]["denominator"],
            "max(expected links, actual links, 1)",
        )
        self.assertEqual(inputs["correct links"], 2.55)
        self.assertEqual(inputs["expected links"], 4.5)
        self.assertEqual(inputs["actual links"], 4.5)
        self.assertIn("per replay case", score["explanation"]["note"])

        count = next(row for row in rows if row["metric_id"] == "responseCorrectStackLinkCount")
        self.assertEqual(count["explanation"]["kind"], "diagnostic")
        self.assertEqual(count["explanation"]["result"]["value"], 2.55)
        self.assertIn("not normalized", count["explanation"]["note"])

    def test_normalize_history_groups_by_metric_path(self) -> None:
        history = app.normalize_history(
            [
                {
                    "metric_path": "root.a",
                    "variant": "hinted",
                    "collected_at": "2026-06-01T00:00:00Z",
                    "run_id": "20260601T000000Z-abcdef123456",
                    "branch": "main",
                    "head_sha": "abcdef1234567890",
                    "score": 0.5,
                    "display_value": 0.5,
                    "display_unit": "score",
                    "is_score": True,
                    "effective_weight_pct": 15.0,
                },
                {
                    "metric_path": "root.a",
                    "variant": "hinted",
                    "collected_at": "2026-06-02T00:00:00Z",
                    "run_id": "20260602T000000Z-fedcba654321",
                    "branch": "main",
                    "head_sha": "fedcba6543217890",
                    "score": 0.75,
                    "display_value": 0.75,
                    "display_unit": "score",
                    "is_score": True,
                    "effective_weight_pct": 30.0,
                },
            ]
        )

        self.assertEqual(len(history["root.a"]), 2)
        self.assertEqual(history["root.a"][1]["score"], 0.75)
        self.assertEqual(history["root.a"][1]["display_value"], 0.75)
        self.assertTrue(history["root.a"][1]["is_score"])
        self.assertEqual(history["root.a"][1]["short_sha"], "fedcba654321")
        self.assertEqual(history["root.a"][1]["effective_weight_pct"], 30.0)

    def test_ready_response_reports_missing_bigquery_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            static_path = root / "index.html"
            rules_static_path = root / "rules.html"
            static_path.write_text("<html></html>")
            rules_static_path.write_text("<html></html>")
            run_dir = root / "target" / "stack-learning" / "runs" / "20260609T000000Z"
            run_dir.mkdir(parents=True)
            (run_dir / "pipeline-run.json").write_text(
                json.dumps(
                    {
                        "runId": "20260609T000000Z",
                        "headSha": "62197da994421e1e5c3ae3014e777ba46862c2b5",
                        "artifacts": {},
                    }
                )
            )

            original_credentials = os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            try:
                payload, status = app.ready_response(
                    static_path=static_path,
                    rules_static_path=rules_static_path,
                    steps_static_path=app.DEFAULT_STEPS_STATIC_PATH,
                    workflow_analysis_root=root,
                    table="project.dataset.table",
                    project_id="project",
                    backend="bigquery",
                    metabase_url="",
                    metabase_api_key="",
                    bigquery_location="US",
                )
            finally:
                if original_credentials is not None:
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = original_credentials

        checks = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(status, app.HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertFalse(payload["ok"])
        self.assertTrue(checks["static_page"]["ok"])
        self.assertTrue(checks["rules_page"]["ok"])
        self.assertTrue(checks["rules_artifacts"]["ok"])
        self.assertFalse(checks["bigquery_credentials"]["ok"])
        self.assertFalse(checks["bigquery_import"]["ok"])
        self.assertIn("GOOGLE_APPLICATION_CREDENTIALS", checks["bigquery_credentials"]["message"])

    def test_static_history_chart_uses_time_axis_and_metadata_popup(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertIn("function formatAxisTime", html)
        self.assertIn("formatAxisTime(item.point.collected_at)", html)
        self.assertIn("className = \"point-popup\"", html)
        self.assertIn("pointer-events: none", html)
        self.assertIn("activePopupIndex === pointIndex", html)
        self.assertIn("data-point-index", html)
        self.assertIn("data-point-key", html)
        self.assertIn("<dt>SHA</dt>", html)
        self.assertIn("<dt>Effective</dt>", html)
        self.assertIn("point.head_sha", html)
        self.assertIn("Effective Weight Over Time", html)
        self.assertIn("Avg Count Over Time", html)
        self.assertIn("display_value", html)
        self.assertIn("effective_weight_pct", html)
        self.assertIn("effective-line", html)
        self.assertIn("effective-point", html)
        self.assertIn("historyLineChart(points", html)
        self.assertNotIn("text-anchor=\"middle\">${escapeHtml(point.short_sha", html)

    def test_static_metric_tree_table_omits_kind_column(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertNotIn("<th class=\"kind-col\">Kind</th>", html)
        self.assertNotIn("<td class=\"kind-col\">", html)
        self.assertNotIn(".kind-col", html)
        self.assertIn(".node-col { width: 360px; }", html)
        self.assertIn(".desc-col { width: 320px; }", html)
        self.assertIn(".metric-col { width: 150px; }", html)
        self.assertIn("tr[data-depth=\"0\"] .name", html)
        self.assertIn("white-space: nowrap", html)
        self.assertIn("tr[data-depth=\"0\"] .node", html)
        self.assertIn("grid-template-columns: 24px minmax(0, 1fr)", html)

    def test_static_metric_tree_table_ends_at_score_trend(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertIn("<th class=\"spark-col\">Score Trend</th>", html)
        self.assertIn("<th class=\"metric-col\">Score / Diagnostic</th>", html)
        self.assertIn("data-label=\"Score / Diagnostic\"", html)
        self.assertIn("diagnostic-value", html)
        self.assertIn("Avg Count", html)
        self.assertIn("info-button", html)
        self.assertIn("metric-info-popup", html)
        self.assertIn("formula-fraction", html)
        self.assertIn("formula.numerator", html)
        self.assertIn("formula.denominator", html)
        self.assertIn("renderMetricInfo", html)
        self.assertIn("row.tree_path", html)
        self.assertIn("row.tree_parent_path", html)
        self.assertNotIn("<th>Metric Path</th>", html)
        self.assertNotIn("<td><div class=\"path\" title=\"${row.metric_path}\">", html)
        self.assertIn(".spark-col { width: 190px; }", html)
        self.assertIn("const width = 170;", html)
        self.assertIn("colspan=\"6\"", html)

    def test_static_metric_tree_table_has_stable_min_width(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertIn("overflow-x: auto", html)
        self.assertIn("overflow-y: hidden", html)
        self.assertIn("min-width: 1320px", html)
        self.assertNotIn("min-width: 1560px", html)
        self.assertNotIn("min-width: 1120px", html)

    def test_static_metric_tree_has_mobile_row_layout(self) -> None:
        html = (REPO_ROOT / "docs" / "splitter-metric-tree-mvp.html").read_text()

        self.assertIn("@media (max-width: 760px)", html)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", html)
        self.assertIn("td[data-label]::before", html)
        self.assertIn("data-label=\"Metric Tree\"", html)
        self.assertIn("data-label=\"Score Trend\"", html)
        self.assertIn("table,\n      tbody", html)
        self.assertIn("min-width: 0", html)

    def test_canonical_tags_parses_both_vocabularies(self) -> None:
        new_style = app.canonical_tags(["change-type:behavior", "layer:core-domain", "qualifier:risky"])
        self.assertEqual(new_style["changeType"], "behavior")
        self.assertEqual(new_style["architectureLayer"], "core-domain")
        self.assertEqual(new_style["qualifiers"], ["risky"])

        legacy = app.canonical_tags(["task-kind:foundation", "phase:foundation"])
        self.assertEqual(legacy["changeType"], "foundation")
        self.assertEqual(legacy["taskKind"], "foundation")
        self.assertEqual(legacy["phase"], "foundation")

        # Explicit change-type wins over legacy fields.
        mixed = app.canonical_tags(["change-type:surface", "task-kind:behavior"])
        self.assertEqual(mixed["changeType"], "surface")

        # Legacy phase "change" maps to the behavior change type.
        self.assertEqual(app.canonical_tags(["phase:change"])["changeType"], "behavior")
        self.assertEqual(app.canonical_tags(["phase:surface"])["changeType"], "surface")

    def test_normalize_task_passes_gates_and_change_type_mix(self) -> None:
        new_shape = app.normalize_task(
            {
                "stackId": "stack-1",
                "changeTypeMix": {"foundation": 1, "behavior": 1},
                "actions": [
                    {
                        "prNumber": 1,
                        "nodeId": "add-config-schema",
                        "changeType": "foundation",
                        "gate": {"hasTests": True, "scope": "local", "verifiedBy": [3]},
                    },
                    {
                        "prNumber": 2,
                        "nodeId": "wire-behavior",
                        "changeType": "behavior",
                        "gate": {"hasTests": False, "scope": "none", "verifiedBy": []},
                    },
                ],
            }
        )
        self.assertEqual(new_shape["changeTypeMix"], {"foundation": 1, "behavior": 1})
        self.assertEqual(new_shape["actions"][0]["gate"]["state"], "passed")
        self.assertEqual(new_shape["actions"][0]["gate"]["raw"]["scope"], "local")
        self.assertEqual(new_shape["actions"][1]["gate"]["state"], "open")

        old_shape = app.normalize_task(
            {
                "stackId": "stack-2",
                "phaseMix": {"foundation": 1, "change": 1},
                "actions": [{"prNumber": 1, "nodeId": "n1", "phase": "change"}],
            }
        )
        self.assertEqual(old_shape["changeTypeMix"], {"foundation": 1, "behavior": 1})
        self.assertEqual(old_shape["actions"][0]["changeType"], "behavior")
        self.assertIsNone(old_shape["actions"][0]["gate"])
        # The old run stays renderable: phaseMix is preserved alongside.
        self.assertEqual(old_shape["phaseMix"], {"foundation": 1, "change": 1})

    def test_rules_response_attaches_parent_scoped_subrules(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "target" / "stack-learning" / "runs" / "20260608T000000Z"
            guidance_dir = run_dir / "guidance"
            reports_dir = run_dir / "reports"
            guidance_dir.mkdir(parents=True)
            reports_dir.mkdir(parents=True)
            experiments_dir = run_dir / "experiments"
            corpus_dir = run_dir / "corpus"
            experiments_dir.mkdir(parents=True)
            corpus_dir.mkdir(parents=True)
            (run_dir / "pipeline-run.json").write_text(
                json.dumps(
                    {
                        "runId": "20260608T000000Z",
                        "artifacts": {
                            "generatedRuleCatalog": "target/stack-learning/runs/20260608T000000Z/guidance/rule-catalog.json",
                            "effectiveRuleCatalog": "rules/rule-catalog.json",
                            "subrulesModel": "target/stack-learning/runs/20260608T000000Z/guidance/subrules.json",
                            "subruleCandidates": "target/stack-learning/runs/20260608T000000Z/guidance/subrule-candidates.json",
                            "subruleInvestigationReport": "target/stack-learning/runs/20260608T000000Z/reports/subrule-investigation.md",
                            "roleCatalog": "target/stack-learning/runs/20260608T000000Z/guidance/role-catalog.json",
                            "experimentLeaderboard": "target/stack-learning/runs/20260608T000000Z/experiments/leaderboard.json",
                            "tasksCorpus": "target/stack-learning/runs/20260608T000000Z/corpus/tasks.jsonl",
                        },
                    }
                )
            )
            catalog = {
                "version": 1,
                "reasons": [{"id": "foundation-before-behavior", "message": "Foundation before behavior."}],
                "actions": [
                    {
                        "id": "learned-node-add-config-reader",
                        "title": "Add config reader",
                        "tags": ["task-kind:foundation", "behavior-type:config-input", "layer:input-config-reader", "phase:foundation"],
                        "phase": "foundation",
                        "metadata": {"nodeId": "add-config-reader", "taskKeys": {"foundation/config-input/input-config-reader": 4}},
                    },
                    {
                        "id": "learned-node-implement-config-behavior",
                        "title": "Implement config behavior",
                        "tags": ["task-kind:behavior", "behavior-type:config-input", "layer:core-domain", "phase:change"],
                        "phase": "change",
                        "metadata": {"nodeId": "implement-config-behavior", "taskKeys": {"behavior/config-input/core-domain": 4}},
                    },
                ],
                "rules": [
                    {
                        "id": "foundation-before-behavior",
                        "priority": 100,
                        "relation": "precedes",
                        "reasonId": "foundation-before-behavior",
                        "before": [{"type": "tag", "id": "foundation"}],
                        "after": [{"type": "tag", "id": "behavior"}],
                    },
                    {
                        "id": "learned-action-add-config-reader-before-implement-config-behavior",
                        "priority": 55,
                        "relation": "precedes",
                        "reasonId": "foundation-before-behavior",
                        "before": [{"type": "action", "id": "learned-node-add-config-reader"}],
                        "after": [{"type": "action", "id": "learned-node-implement-config-behavior"}],
                        "metadata": {"source": "learned-action-pairs", "backoffLevel": 0},
                    },
                    {
                        "id": "learned-foundation-before-change",
                        "priority": 40,
                        "relation": "precedes",
                        "reasonId": "foundation-before-behavior",
                        "before": [{"type": "tag", "id": "foundation"}],
                        "after": [{"type": "tag", "id": "behavior"}],
                        "metadata": {"source": "role-labeling", "kind": "phase-backoff-prior", "backoffLevel": 1},
                    },
                ],
            }
            (guidance_dir / "role-catalog.json").write_text(
                json.dumps(
                    {
                        "nodes": [
                            {
                                "id": "add-config-reader",
                                "title": "Add config reader",
                                "tags": ["task-kind:foundation", "phase:foundation"],
                                "phase": "foundation",
                                "taskKeys": {"foundation/config-input/input-config-reader": 4},
                            }
                        ]
                    }
                )
            )
            (experiments_dir / "leaderboard.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "artifactType": "experiment-leaderboard",
                        "stages": [
                            {
                                "stage": "action-extraction",
                                "winner": "sim-070",
                                "promotions": ["sim-070"],
                                "experiments": [
                                    {"id": "baseline", "decision": "baseline"},
                                    {"id": "sim-070", "decision": "promoted"},
                                ],
                            }
                        ],
                    }
                )
            )
            (corpus_dir / "tasks.jsonl").write_text(
                json.dumps(
                    {
                        "stackId": "stack-1",
                        "split": "train",
                        "title": "Add config-driven behavior",
                        "actionIds": ["add-config-reader", "implement-config-behavior"],
                        "phaseMix": {"foundation": 1, "change": 1},
                    }
                )
                + "\n"
            )
            (guidance_dir / "rule-catalog.json").write_text(json.dumps(catalog))
            rules_dir = root / "rules"
            rules_dir.mkdir()
            (rules_dir / "rule-catalog.json").write_text(json.dumps(catalog))
            (guidance_dir / "subrules.json").write_text(
                json.dumps(
                    {
                        "subrules": [
                            {
                                "ruleId": "learned-subrule-foundation-config-reader-before-behavior-config-behavior",
                                "parentRuleId": "foundation-before-behavior",
                                "before": "foundation/config-reader",
                                "after": "behavior/config-behavior",
                                "support": 4,
                                "repoSupport": 2,
                                "repos": ["bazelbuild/bazel"],
                                "confidence": 0.7,
                                "promoted": False,
                            }
                        ]
                    }
                )
            )
            (guidance_dir / "subrule-candidates.json").write_text(
                json.dumps(
                    {
                        "splitSummary": {
                            "train": {"stackCount": 21, "prCount": 64, "repoCount": 9, "repos": ["bazelbuild/bazel"]},
                            "validation": {"stackCount": 5, "prCount": 12, "repoCount": 2, "repos": ["apache/flink"]},
                            "test": {"stackCount": 0, "prCount": 0, "repoCount": 0, "repos": []},
                        },
                        "subrules": [
                            {
                                "candidateId": "foundation-before-behavior__foundation-config-reader__before__behavior-config-behavior",
                                "ruleId": "learned-subrule-foundation-config-reader-before-behavior-config-behavior",
                                "parentRuleId": "foundation-before-behavior",
                                "before": "add-config-reader",
                                "after": "implement-config-behavior",
                                "beforeTitle": "Add config reader",
                                "afterTitle": "Implement config behavior",
                                "decision": "promoted",
                                "failureBucket": "",
                                "metadata": {"backoffLevel": 0},
                                "validationSupport": {"support": 2, "weightedSupport": 1.4, "backoffLevel": 1},
                                "validationSummary": {
                                    "subruleProof": {
                                        "hintsRulesVsHints": {
                                            "pairedCount": 3,
                                            "averagePlannedStackLinkCorrectnessDelta": 0.12,
                                            "regressionCount": 0,
                                        }
                                    }
                                },
                            },
                            {
                                "candidateId": "learned-role-transition-behavior-feature-behavior-before-release",
                                "parentRuleId": "learned-role-transition",
                                "before": "behavior/feature-behavior",
                                "after": "release",
                                "support": 7,
                                "repoSupport": 2,
                                "confidence": 0.59,
                                "decision": "skipped",
                                "failureBucket": "unsupported_selector",
                            },
                        ],
                    }
                )
            )
            (reports_dir / "subrule-investigation.json").write_text(json.dumps({"status": "ok"}))

            response = app.rules_response(root)

        self.assertEqual(response["subrules"]["count"], 2)
        generated = next(catalog for catalog in response["catalogs"] if catalog["kind"] == "generated")
        self.assertEqual(generated["counts"]["rules"], 3)
        rule = next(rule for rule in generated["rules"] if rule["id"] == "foundation-before-behavior")
        self.assertEqual(rule["subrules"][0]["parentRuleId"], "foundation-before-behavior")
        self.assertEqual(rule["subrules"][0]["decision"], "promoted")
        self.assertEqual(rule["subrules"][0]["validation"]["stackLinkDelta"], 0.12)
        self.assertEqual(rule["subrules"][0]["validation"]["weightedSupport"], 1.4)
        self.assertEqual(rule["subrules"][0]["backoffLevel"], 1)
        self.assertEqual(rule["subrules"][0]["beforeTitle"], "Add config reader")
        # Hand-written base rules keep label fallbacks; selectors resolve to objects.
        self.assertEqual(rule["before"][0]["label"], "tag:foundation")
        self.assertFalse(rule["isBackoffPrior"])

        action_rule = next(
            rule for rule in generated["rules"]
            if rule["id"] == "learned-action-add-config-reader-before-implement-config-behavior"
        )
        self.assertEqual(action_rule["backoffLevel"], 0)
        self.assertFalse(action_rule["isBackoffPrior"])
        self.assertEqual(action_rule["before"][0]["title"], "Add config reader")
        self.assertEqual(action_rule["before"][0]["tags"]["taskKind"], "foundation")
        # Old-vocabulary tags still resolve a changeType (backward compatibility).
        self.assertEqual(action_rule["before"][0]["changeType"], "foundation")
        self.assertEqual(action_rule["before"][0]["taskKey"], "foundation/config-input/input-config-reader")

        backoff_rule = next(
            rule for rule in generated["rules"] if rule["id"] == "learned-foundation-before-change"
        )
        self.assertTrue(backoff_rule["isBackoffPrior"])
        self.assertEqual(backoff_rule["backoffLevel"], 1)

        self.assertEqual(response["splitSummary"]["validation"]["stackCount"], 5)
        self.assertEqual(response["subrules"]["candidatesByBackoffLevel"], {"1": 1, "none": 1})
        self.assertEqual(response["leaderboard"]["stages"][0]["winner"], "sim-070")
        self.assertEqual(len(response["tasks"]), 1)
        self.assertEqual(response["tasks"][0]["actionIds"], ["add-config-reader", "implement-config-behavior"])

        self.assertFalse(any(rule["id"] == "uncategorized-subrules" for rule in generated["rules"]))
        uncategorized = response["subrules"]["uncategorizedParents"]
        self.assertEqual(len(uncategorized), 1)
        self.assertEqual(uncategorized[0]["sourceParentRuleId"], "learned-role-transition")
        self.assertEqual(uncategorized[0]["candidateCount"], 1)
        self.assertEqual(uncategorized[0]["subrules"][0]["sourceParentRuleId"], "learned-role-transition")
        self.assertEqual(uncategorized[0]["subrules"][0]["before"], "behavior/feature-behavior")

    def test_rules_html_uses_interactive_d3_model_view(self) -> None:
        html = app.DEFAULT_RULES_STATIC_PATH.read_text()
        app_source = (REPO_ROOT / "scripts" / "splitter_metric_tree_app.py").read_text()

        self.assertEqual(app.DEFAULT_RULES_STATIC_PATH.name, "rules-d3-poc.html")
        self.assertIn("<title>Rule Graph</title>", html)
        self.assertIn("<h1>Rule Graph</h1>", html)
        self.assertIn("https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js", html)
        self.assertIn("d3.forceSimulation", html)
        self.assertIn("d3.forceManyBody().strength(-520)", html)
        self.assertIn("d3.forceCollide(node => nodeCollisionRadius(node))", html)
        self.assertIn("d3.drag", html)
        self.assertIn("function releaseNode", html)
        self.assertIn("function renderNestedGraph", html)
        self.assertIn("function graphFromRulesPayload", html)
        self.assertIn("function nestedGraphFromSubrules", html)
        self.assertIn("function diagnosticParents", html)
        self.assertIn("function graphFromPlanningDag", html)
        self.assertIn("if (!(catalog.rules || []).length && planningGraph) return planningGraph;", html)
        self.assertNotIn("if (planningGraph) return planningGraph;", html)
        self.assertIn("fetch(\"/api/splitter-rules\"", html)
        self.assertIn("id=\"toggle-uncategorized\"", html)
        self.assertIn("Show uncategorized subrules", html)
        self.assertIn("showUncategorizedSubrules", html)
        self.assertIn("uncategorizedParents", html)
        self.assertIn("id=\"toggle-backoff\"", html)
        self.assertIn("Show backoff priors", html)
        self.assertIn("function tagChips", html)
        self.assertIn("backoff-prior", html)
        self.assertIn("function renderTasksPanel", html)
        self.assertIn("function renderLeaderboardPanel", html)
        self.assertIn("function stageForPhase", html)
        self.assertIn("diagnostic-parent:", html)
        self.assertIn("Drag nodes to pin them", html)
        self.assertIn("nested nodes also repel and drag", html)
        self.assertIn("function stageForChangeType", html)
        # Nine changeType lanes plus diagnostics; the evidence lane is retired.
        for lane in ["foundation", "dependency", "compatibility", "refactor", "behavior", "surface", "verification", "docs", "cleanup", "diagnostics"]:
            self.assertIn(f'id: "{lane}", label: "{lane.title()}"', html)
        self.assertNotIn('id: "evidence", label: "Evidence"', html)
        self.assertNotIn('stage: "evidence"', html)
        # "implementation" never renders: only machine-id keys may contain it.
        for line in html.splitlines():
            if "implementation" in line.lower():
                self.assertTrue(
                    "terminal-verification-after-implementation" in line or '"tag:implementation"' in line,
                    f"unexpected implementation wording: {line.strip()[:120]}",
                )
        self.assertIn("stageForChangeType(item.changeType || tags.changeType)", html)
        self.assertIn("change: ${escapeHtml(changeType)}", html)
        self.assertIn("href=\"/steps.html\"", html)
        self.assertIn("href=\"/steps.html?view=graph\"", html)
        self.assertIn("DEFAULT_RULES_STATIC_PATH = REPO_ROOT / \"docs\" / \"rules-d3-poc.html\"", app_source)

    def test_static_rules_steps_page_is_step_sequence_view(self) -> None:
        html = (REPO_ROOT / "docs" / "rules-steps.html").read_text()
        app_source = (REPO_ROOT / "scripts" / "splitter_metric_tree_app.py").read_text()

        self.assertIn("<title>Step Sequences</title>", html)
        self.assertIn("href=\"/rules.html\"", html)
        self.assertIn("requestedView === \"graph\"", html)
        self.assertIn("DEFAULT_STEPS_STATIC_PATH = REPO_ROOT / \"docs\" / \"rules-steps.html\"", app_source)
        self.assertIn("if parsed.path == \"/steps.html\":", app_source)
        self.assertIn("https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js", html)

        # The single changeType vocabulary drives lanes, chips, and ordering.
        self.assertIn("const CHANGE_TYPE_ORDER", html)
        self.assertIn("const LEGACY_PHASE_TO_CHANGE_TYPE", html)
        self.assertIn("function changeTypeForPhase", html)
        self.assertIn("function keywordChangeTypeForLabel", html)
        self.assertIn("function changeTypeForNode", html)
        self.assertIn("change: ${", html)
        self.assertIn("changeTypeMix", html)

        # Left-to-right deterministic step layout replaces lanes + force simulation.
        self.assertIn("function assignTopoColumns", html)
        self.assertIn("function columnLayout", html)
        self.assertIn("column-band", html)
        self.assertIn("Step ${band.column + 1}", html)
        self.assertNotIn("const stages = [", html)
        self.assertNotIn("d3.forceSimulation", html)
        self.assertNotIn("function stageForPhase", html)
        self.assertNotIn("function stageForRoleLabel", html)
        self.assertNotIn("function layoutStageColumns", html)

        # Rules and subrules are edges between steps; the nested subsystem is gone.
        self.assertIn("function sequenceFromRulesPayload", html)
        self.assertNotIn("function nestedGraphFromSubrules", html)
        self.assertNotIn("function renderNestedGraph", html)
        self.assertNotIn("nestedSimulations", html)
        self.assertNotIn("function zoomNestedNode", html)
        self.assertIn("function selectEdge", html)
        self.assertIn("function renderEdgeEvidence", html)
        self.assertIn("function renderSubruleProof", html)
        self.assertIn("function proofSummary", html)
        self.assertIn("function exampleLink", html)
        self.assertIn("fromCommitUrl", html)
        self.assertIn("target=\"_blank\"", html)
        self.assertIn("backoff L", html)

        # Verification gates render on steps and between task-strip steps.
        self.assertIn("function gateStateForNode", html)
        self.assertIn("function renderGateGlyph", html)
        self.assertIn("function gateGlyphHtml", html)
        self.assertIn("gate-badge", html)
        self.assertIn("gate-glyph", html)
        self.assertIn("no verification gate recorded", html)

        # Task strips are the default view.
        self.assertIn("function renderTaskStrips", html)
        self.assertIn("function taskStepTitle", html)
        self.assertIn("function selectTaskStep", html)
        self.assertIn("let activeView = \"tasks\"", html)
        self.assertIn("function setView", html)
        self.assertIn("task-strip-row", html)

        # Change-type order strip for tag-level priors.
        self.assertIn("function renderOrderStrip", html)
        self.assertIn("Change-type order priors", html)
        self.assertIn("showBackoffPriors", html)

        # Demo fallback exercises the new model offline.
        self.assertIn("const demoTasks", html)
        self.assertIn("const demoNodes", html)
        self.assertNotIn("nestedGraph:", html)
        self.assertIn("Add config-driven behavior", html)

        self.assertIn("function selectNode", html)
        self.assertIn("Prompt instruction", html)
        self.assertIn("production planning model", html.lower())
        self.assertIn("\"roleCatalog\": role_catalog", app_source)
        self.assertIn("\"planningDag\": planning_dag", app_source)

    def test_action_rule_graph_poc_route_redirects_to_nested_rule_graph(self) -> None:
        app_source = (REPO_ROOT / "scripts" / "splitter_metric_tree_app.py").read_text()

        self.assertIn("action-rule-graph-poc.html", app_source)
        self.assertIn("send_redirect(\"/rules.html?demo=nested\")", app_source)
        self.assertIn("self.send_redirect(f\"/rules.html{suffix}\")", app_source)
        self.assertIn("HTTPStatus.FOUND", app_source)
        self.assertNotIn("DEFAULT_ACTION_RULE_GRAPH_POC_STATIC_PATH", app_source)
        self.assertFalse((REPO_ROOT / "docs" / "action-rule-graph-poc.html").exists())

    def test_splitter_metric_tree_launcher_uses_bigquery_venv(self) -> None:
        launcher = (REPO_ROOT / "scripts" / "run-splitter-metric-tree-app.sh").read_text()
        installer = (REPO_ROOT / "scripts" / "install-splitter-metric-tree-do1.sh").read_text()

        self.assertIn("bigquery.env", launcher)
        self.assertIn("bigquery-venv", launcher)
        self.assertIn("google-cloud-bigquery", launcher)
        self.assertIn("WORKFLOW_ANALYSIS_SERVICE_ROOT", launcher)
        self.assertIn("scripts/splitter_metric_tree_app.py", launcher)
        self.assertIn("--rules-static-path docs/rules-d3-poc.html", launcher)
        self.assertNotIn("--rules-static-path docs/splitter-rules.html", launcher)
        self.assertIn("pgrep -f \"[s]plitter_metric_tree_app.py\"", installer)
        self.assertIn("pkill -9 -f \"[s]plitter_metric_tree_app.py\"", installer)
        self.assertIn("/healthz", installer)


if __name__ == "__main__":
    unittest.main()
