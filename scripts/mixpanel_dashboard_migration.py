#!/usr/bin/env python3
"""Migrate Mixpanel dashboard/report assets to request_pattern_layers_v1."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any


MIXPANEL_BASE = "https://mixpanel.com"
NEW_BOARD_TITLE = "Usage Cost Ops - request_pattern_layers_v1"
TOP_LEVEL_BOARD_TITLE = "Usage Why / Cost Ops Over Time"
SESSION_ROOT_CAUSE_BOARD_TITLE = "Usage Why / Session Root Cause"
DEPRECATED_PREFIX = "[Deprecated] "
OLD_TAXONOMY_MARKERS = ("request_subpattern", "request_cache_sources_v3")
NEW_DIAGNOSIS_VERSION = "request_pattern_layers_v1"


def auth_header() -> str:
    user = os.getenv("MIXPANEL_SERVICE_ACCOUNT_USER", "")
    password = os.getenv("MIXPANEL_SERVICE_ACCOUNT_PASS", "")
    api_secret = os.getenv("MIXPANEL_API_SECRET", "")
    if user and password:
        raw = f"{user}:{password}".encode("utf-8")
    elif api_secret:
        raw = f"{api_secret}:".encode("utf-8")
    else:
        raise RuntimeError("Missing Mixpanel auth env.")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class MixpanelAppClient:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.headers = {
            "Authorization": auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(MIXPANEL_BASE + path, data=data, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed status={exc.code} body={body[:500]}") from exc
        if not body:
            return {}
        parsed = json.loads(body)
        if isinstance(parsed, dict) and parsed.get("status") == "error":
            raise RuntimeError(f"{method} {path} failed body={body[:500]}")
        return parsed

    def collection(self, name: str) -> list[dict[str, Any]]:
        payload = self.request("GET", f"/api/app/projects/{self.project_id}/{name}")
        rows = payload.get("results", [])
        return rows if isinstance(rows, list) else []

    def get_one(self, collection: str, asset_id: int) -> dict[str, Any]:
        payload = self.request("GET", f"/api/app/projects/{self.project_id}/{collection}/{asset_id}")
        result = payload.get("results", {})
        return result if isinstance(result, dict) else {}

    def patch_one(self, collection: str, asset_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"/api/app/projects/{self.project_id}/{collection}/{asset_id}", payload)

    def create_dashboard(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/app/projects/{self.project_id}/dashboards", payload)

    def create_bookmark(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/api/app/projects/{self.project_id}/bookmarks", payload)


def asset_text(asset: dict[str, Any]) -> str:
    return json.dumps(asset, sort_keys=True, default=str)


def matched_markers(asset: dict[str, Any]) -> list[str]:
    text = asset_text(asset)
    return [marker for marker in OLD_TAXONOMY_MARKERS if marker in text]


def prefixed(value: str) -> str:
    if value.startswith(DEPRECATED_PREFIX):
        return value
    return DEPRECATED_PREFIX + value


def property_ref(name: str, prop_type: str = "string") -> dict[str, Any]:
    return {
        "propertyDefaultType": prop_type,
        "propertyName": name,
        "propertyType": prop_type,
        "resourceType": "events",
        "value": name,
    }


def filter_ref(name: str, value: str) -> dict[str, Any]:
    return {
        "resourceType": "events",
        "filterType": "string",
        "defaultType": "string",
        "value": name,
        "filterValue": [value],
        "filterOperator": "equals",
    }


def metric(event_name: str, math: str, prop_name: str | None = None, prop_type: str = "number") -> dict[str, Any]:
    measurement: dict[str, Any] = {"math": math}
    if prop_name:
        measurement["property"] = {
            "dataset": "mixpanel",
            "name": prop_name,
            "defaultType": prop_type,
            "type": prop_type,
            "resourceType": "events",
        }
    return {
        "type": "metric",
        "behavior": {
            "type": "event",
            "name": event_name,
            "filters": [],
            "filtersDeterminer": "all",
            "resourceType": "events",
        },
        "measurement": measurement,
        "isHidden": False,
    }


def insight_params(
    *,
    event_name: str,
    metrics: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    filters: list[dict[str, Any]],
    chart_type: str = "table",
) -> str:
    return json.dumps(
        {
            "displayOptions": {"chartType": chart_type, "plotStyle": "standard", "value": "absolute"},
            "sections": {
                "filter": filters,
                "group": groups,
                "show": metrics,
                "time": [{"dateRangeType": "in the last", "window": {"unit": "day", "value": 30}, "unit": "day"}],
            },
            "sorting": {},
        }
    )


def canonical_reports() -> list[dict[str, Any]]:
    diagnosis_filter = [filter_ref("diagnosis_version", NEW_DIAGNOSIS_VERSION)]
    reports = [
        {
            "name": "Final Request Pattern Cost",
            "board": "top_level",
            "description": "Derived request cost by final recursive request_pattern.",
            "params": insight_params(
                event_name="usage_request_cache_diagnosis",
                metrics=[metric("usage_request_cache_diagnosis", "total", "derived_total_cost_usd")],
                groups=[property_ref("request_pattern")],
                filters=diagnosis_filter,
            ),
        },
        {
            "name": "Request Pattern Hierarchy Cost",
            "board": "top_level",
            "description": "Derived request cost by slash-delimited request_pattern_path.",
            "params": insight_params(
                event_name="usage_request_cache_diagnosis",
                metrics=[metric("usage_request_cache_diagnosis", "total", "derived_total_cost_usd")],
                groups=[property_ref("request_pattern_path")],
                filters=diagnosis_filter,
            ),
        },
        {
            "name": "Request Pattern Hierarchy Calls",
            "board": "top_level",
            "description": "Request volume by request_pattern_path.",
            "params": insight_params(
                event_name="usage_request_cache_diagnosis",
                metrics=[metric("usage_request_cache_diagnosis", "total", "tool_calls")],
                groups=[property_ref("request_pattern_path")],
                filters=diagnosis_filter,
            ),
        },
        {
            "name": "Final Uncategorized Share",
            "board": "session_root_cause",
            "description": "Derived request cost for final uncategorized requests.",
            "params": insight_params(
                event_name="usage_request_cache_diagnosis",
                metrics=[metric("usage_request_cache_diagnosis", "total", "derived_total_cost_usd")],
                groups=[property_ref("request_pattern")],
                filters=[*diagnosis_filter, filter_ref("request_pattern", "uncategorized")],
            ),
        },
        {
            "name": "Request Command Cost by Pattern Path",
            "board": "session_root_cause",
            "description": "Allocated command/tool cost by request_pattern_path, dimension, and name.",
            "params": insight_params(
                event_name="usage_request_tool_attribution",
                metrics=[metric("usage_request_tool_attribution", "total", "allocated_total_cost_usd")],
                groups=[property_ref("request_pattern_path"), property_ref("dimension"), property_ref("name")],
                filters=diagnosis_filter,
            ),
        },
    ]
    command_filter = [
        filter_ref("schema_version", "usage_command_attribution_v4_1"),
        filter_ref("service_classifier_revision", "service_context_v2"),
    ]
    reports.extend(
        [
            {
                "name": "Cost by Service Reason",
                "board": "top_level",
                "description": "Estimated command cost by context-aware service_of_why. Methodology: exact prompt costs allocated to commands by output-token estimate.",
                "params": insight_params(
                    event_name="usage_command_attribution",
                    metrics=[metric("usage_command_attribution", "total", "allocated_total_cost_usd")],
                    groups=[property_ref("service_of_why")],
                    filters=command_filter,
                ),
            },
            {
                "name": "Cost by Tool Action",
                "board": "top_level",
                "description": "Estimated command cost by immediate tool_action.",
                "params": insight_params(
                    event_name="usage_command_attribution",
                    metrics=[metric("usage_command_attribution", "total", "allocated_total_cost_usd")],
                    groups=[property_ref("tool_action")],
                    filters=command_filter,
                ),
            },
            {
                "name": "Service Reason x Tool Action",
                "board": "top_level",
                "description": "Estimated command cost by higher-level service reason and immediate tool action.",
                "params": insight_params(
                    event_name="usage_command_attribution",
                    metrics=[metric("usage_command_attribution", "total", "allocated_total_cost_usd")],
                    groups=[property_ref("service_of_why"), property_ref("tool_action")],
                    filters=command_filter,
                ),
            },
            {
                "name": "Top Sessions by Command Cost",
                "board": "session_root_cause",
                "description": "Sessions ranked by estimated allocated command cost.",
                "params": insight_params(
                    event_name="usage_command_attribution",
                    metrics=[metric("usage_command_attribution", "total", "allocated_total_cost_usd")],
                    groups=[property_ref("session_id"), property_ref("task_label")],
                    filters=command_filter,
                ),
            },
            {
                "name": "Session Prompt Command Drilldown",
                "board": "session_root_cause",
                "description": "Session to task to service reason to tool action to target drilldown for command attribution.",
                "params": insight_params(
                    event_name="usage_command_attribution",
                    metrics=[metric("usage_command_attribution", "total", "allocated_total_cost_usd")],
                    groups=[property_ref("session_id"), property_ref("task_label"), property_ref("service_of_why"), property_ref("tool_action"), property_ref("target")],
                    filters=command_filter,
                ),
            },
            {
                "name": "Remaining Uncategorized Breakdown",
                "board": "session_root_cause",
                "description": "Remaining uncategorized command cost by deterministic reason.",
                "params": insight_params(
                    event_name="usage_command_attribution",
                    metrics=[metric("usage_command_attribution", "total", "allocated_total_cost_usd")],
                    groups=[property_ref("uncategorized_reason"), property_ref("function_name"), property_ref("tool_action")],
                    filters=[*command_filter, filter_ref("service_of_why", "uncategorized")],
                ),
            },
            {
                "name": "Autofix Failure Repair Drilldown",
                "board": "session_root_cause",
                "description": "Estimated command cost for autofix_or_failure_repair context by tool action and target.",
                "params": insight_params(
                    event_name="usage_command_attribution",
                    metrics=[metric("usage_command_attribution", "total", "allocated_total_cost_usd")],
                    groups=[property_ref("tool_action"), property_ref("function_name"), property_ref("target")],
                    filters=[*command_filter, filter_ref("service_of_why", "autofix_or_failure_repair")],
                ),
            },
            {
                "name": "Source Inspection Targets",
                "board": "session_root_cause",
                "description": "Source inspection command cost by target_type and target.",
                "params": insight_params(
                    event_name="usage_command_attribution",
                    metrics=[metric("usage_command_attribution", "total", "allocated_total_cost_usd")],
                    groups=[property_ref("target_type"), property_ref("target")],
                    filters=[*command_filter, filter_ref("primary_why", "source_inspection")],
                ),
            },
        ]
    )
    return reports


def backup_payload(
    output_dir: Path,
    dashboards: list[dict[str, Any]],
    bookmarks: list[dict[str, Any]],
    dashboard_candidates: list[dict[str, Any]],
    bookmark_candidates: list[dict[str, Any]],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"mixpanel-dashboard-migration-backup-{int(time.time())}.json"
    payload = {
        "created_at": int(time.time()),
        "candidate_dashboard_ids": [item["id"] for item in dashboard_candidates],
        "candidate_bookmark_ids": [item["id"] for item in bookmark_candidates],
        "dashboards": dashboards,
        "bookmarks": bookmarks,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate Mixpanel dashboards to request_pattern_layers_v1.")
    parser.add_argument("--execute", action="store_true", help="Apply live Mixpanel mutations. Default is dry-run.")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--new-board-title", default=NEW_BOARD_TITLE)
    parser.add_argument("--top-level-board-title", default=TOP_LEVEL_BOARD_TITLE)
    parser.add_argument("--session-root-cause-board-title", default=SESSION_ROOT_CAUSE_BOARD_TITLE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_id = os.getenv("MIXPANEL_PROJECT_ID", "")
    if not project_id:
        print("Missing MIXPANEL_PROJECT_ID", file=sys.stderr)
        return 1

    client = MixpanelAppClient(project_id)
    client.collection("service-accounts")
    dashboards = client.collection("dashboards")
    bookmarks = client.collection("bookmarks")
    bookmark_candidates = [item for item in bookmarks if matched_markers(item)]
    candidate_dashboard_ids = {item.get("dashboard_id") for item in bookmark_candidates if item.get("dashboard_id")}
    dashboard_candidates = [
        item for item in dashboards if matched_markers(item) or item.get("id") in candidate_dashboard_ids
    ]
    backup_path = backup_payload(Path(args.output_dir), dashboards, bookmarks, dashboard_candidates, bookmark_candidates)

    summary: dict[str, Any] = {
        "dry_run": not args.execute,
        "backup_path": str(backup_path),
        "deprecated_dashboards": [],
        "deprecated_bookmarks": [],
        "new_dashboards": {},
        "created_bookmarks": [],
        "errors": [],
    }

    for dashboard in dashboard_candidates:
        title = str(dashboard.get("title") or "")
        new_title = prefixed(title)
        summary["deprecated_dashboards"].append({"id": dashboard["id"], "old_title": title, "new_title": new_title})
        if args.execute and new_title != title:
            try:
                client.patch_one("dashboards", int(dashboard["id"]), {"title": new_title})
            except RuntimeError as exc:
                summary["errors"].append({"asset": "dashboard", "id": dashboard["id"], "error": str(exc)})

    for bookmark in bookmark_candidates:
        name = str(bookmark.get("name") or "")
        new_name = prefixed(name)
        summary["deprecated_bookmarks"].append(
            {"id": bookmark["id"], "dashboard_id": bookmark.get("dashboard_id"), "old_name": name, "new_name": new_name}
        )
        if args.execute and new_name != name:
            try:
                client.patch_one("bookmarks", int(bookmark["id"]), {"name": new_name})
            except RuntimeError as exc:
                summary["errors"].append({"asset": "bookmark", "id": bookmark["id"], "error": str(exc)})

    board_specs = {
        "top_level": {
            "title": args.top_level_board_title,
            "description": "Top-level usage cost ops over time by request pattern and command why.",
        },
        "session_root_cause": {
            "title": args.session_root_cause_board_title,
            "description": "Session breakdown and root-cause drilldowns for token and command spend.",
        },
    }
    new_dashboards: dict[str, dict[str, Any]] = {}
    for board_key, spec in board_specs.items():
        existing_new = next((item for item in dashboards if item.get("title") == spec["title"]), None)
        if existing_new:
            new_dashboard = deepcopy(existing_new)
        elif args.execute:
            try:
                created = client.create_dashboard(
                    {
                        "title": spec["title"],
                        "description": spec["description"],
                        "is_private": False,
                        "time_filter": {
                            "dateRange": {"type": "in the last", "window": {"unit": "day", "value": 30}},
                            "displayText": "Last 30 days",
                        },
                    }
                )
                new_dashboard = created.get("results", created)
            except RuntimeError as exc:
                summary["errors"].append({"asset": "dashboard", "id": None, "title": spec["title"], "error": str(exc)})
                new_dashboard = {"title": spec["title"], "id": None}
        else:
            new_dashboard = {"title": spec["title"], "id": None}
        new_dashboards[board_key] = new_dashboard
        summary["new_dashboards"][board_key] = {"id": new_dashboard.get("id"), "title": new_dashboard.get("title")}

    if args.execute:
        missing = [key for key, dashboard in new_dashboards.items() if not dashboard.get("id")]
        if missing:
            summary["errors"].append({"asset": "dashboard", "id": None, "error": f"New dashboard creation did not return ids for: {', '.join(missing)}"})
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 1

    existing_by_name = {
        item.get("name"): item
        for item in bookmarks
        if isinstance(item.get("name"), str) and not str(item.get("name")).startswith(DEPRECATED_PREFIX)
    }
    for report in canonical_reports():
        board_key = str(report.get("board") or "top_level")
        new_dashboard = new_dashboards[board_key]
        planned = {"name": report["name"], "board": board_key, "dashboard_id": new_dashboard.get("id")}
        existing_report = existing_by_name.get(report["name"])
        if args.execute:
            try:
                if existing_report:
                    result = client.patch_one(
                        "bookmarks",
                        int(existing_report["id"]),
                        {
                            "dashboard_id": new_dashboard["id"],
                            "name": report["name"],
                            "description": report["description"],
                            "params": report["params"],
                        },
                    ).get("results", {})
                    planned["id"] = result.get("id", existing_report["id"])
                    planned["action"] = "updated"
                else:
                    payload = {
                        "type": "insights",
                        "name": report["name"],
                        "dashboard_id": new_dashboard["id"],
                        "description": report["description"],
                        "params": report["params"],
                    }
                    created = client.create_bookmark(payload)
                    result = created.get("results", created)
                    bookmark_id = int(result["id"])
                    result = client.patch_one(
                        "bookmarks",
                        bookmark_id,
                        {"dashboard_id": new_dashboard["id"], "params": report["params"]},
                    ).get("results", {})
                    planned["id"] = result.get("id", bookmark_id)
                    planned["action"] = "created"
            except RuntimeError as exc:
                planned["error"] = str(exc)
                summary["errors"].append({"asset": "bookmark", "id": None, "name": report["name"], "error": str(exc)})
        summary["created_bookmarks"].append(planned)

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
