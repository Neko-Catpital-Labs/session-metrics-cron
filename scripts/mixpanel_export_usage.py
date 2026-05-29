#!/usr/bin/env python3
"""Export usage report artifacts to Mixpanel."""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_TASK_CATEGORIZATION_CONFIG: dict[str, Any] = {
    "version": "builtin_regex_v1",
    "defaults": {
        "id": "uncategorized",
        "label": "Uncategorized",
        "confidence": "low",
        "reason": "no_rule_matched",
    },
    "context": {
        "max_chars": 4000,
        "fields": [
            {"name": "prompt_preview", "weight": 1.0},
            {"name": "previous_prompt_preview", "weight": 0.9},
            {"name": "first_prompt_preview", "weight": 0.8},
            {"name": "final_answer_preview", "weight": 0.4},
            {"name": "session_cwd", "weight": 0.3},
        ],
    },
    "categories": [
        {"id": "pr_review", "label": "PR Review"},
        {"id": "invoker_plan_submission", "label": "Invoker Plan Submission"},
        {"id": "git_branch_stack", "label": "Git Branch / Stack"},
        {"id": "debug_repro", "label": "Debug / Repro"},
        {"id": "ui_terminal_visual", "label": "UI / Terminal / Visual Proof"},
        {"id": "dependency_setup", "label": "Dependency / Setup"},
        {"id": "test_ci_failure", "label": "Test / CI Failure"},
        {"id": "release_packaging", "label": "Release / Packaging"},
        {"id": "workflow_repair", "label": "Workflow Repair"},
        {"id": "uncategorized", "label": "Uncategorized"},
    ],
    "classifiers": [
        {
            "id": "regex_v1",
            "type": "regex",
            "enabled": True,
            "rules": [
                {"id": "pr_review", "priority": 900, "confidence": "high", "regex": [r"\bpr\b", r"\bpull request\b", r"\breview\b", "pr summary", "pr body", "auto-stamp", "landed", "merged"]},
                {"id": "invoker_plan_submission", "priority": 850, "confidence": "high", "regex": [r"\binvoker\b", "plan-to-invoker", "submit to invoker", "workflow chain", "workflow submission"]},
                {"id": "git_branch_stack", "priority": 800, "confidence": "high", "regex": [r"\brebase\b", r"\bmerge\b", r"\bstack\b", "upstream/master", "origin/master", "branch stack", "recreate all workflows"]},
                {"id": "debug_repro", "priority": 750, "confidence": "high", "regex": [r"\brepro\b", "root cause", r"\bdebug\b", r"\binvestigate\b", "failure analysis"]},
                {"id": "ui_terminal_visual", "priority": 700, "confidence": "high", "regex": [r"\bui\b", "terminal", "screenshot", "visual proof", "playwright", "embedded pty", "graph"]},
                {"id": "dependency_setup", "priority": 650, "confidence": "medium", "regex": [r"\binstall\b", r"\bdependency\b", r"\bdependencies\b", r"\bpnpm\b", r"\bnpm\b", r"\bpip\b", r"\bbundler\b"]},
                {"id": "test_ci_failure", "priority": 600, "confidence": "high", "regex": [r"\bci\b", "test failure", "failing test", r"\bpytest\b", "make test", "build failed"]},
                {"id": "release_packaging", "priority": 500, "confidence": "medium", "regex": [r"\brelease\b", r"\bpackage\b", r"\bversion\b", "changelog"]},
                {"id": "workflow_repair", "priority": 450, "confidence": "medium", "regex": ["task failed", "workflow failed", "fix with agent", "autofix", "review gate"]},
            ],
        }
    ],
}


DEFAULT_REQUEST_PATTERN_CONFIG: dict[str, Any] = {
    "version": "request_pattern_layers_v1",
    "context": {
        "fields": [
            {"name": "prompt_preview", "weight": 1.0},
            {"name": "previous_prompt_preview", "weight": 0.9},
            {"name": "first_prompt_preview", "weight": 0.8},
            {"name": "final_answer_preview", "weight": 0.4},
            {"name": "session_cwd", "weight": 0.3},
        ],
    },
    "layers": [
        {
            "id": "request_origin",
            "default": "other",
            "rules": [
                {"id": "previous_agent_plan", "confidence": "high", "regex": ["a previous agent produced the plan below", "previous agent.*plan"]},
                {"id": "implement_plan", "confidence": "high", "regex": ["^implement the plan", "implement.*plan"]},
                {"id": "upstream_task_handoff", "confidence": "high", "regex": ["\\[upstream task:", "upstream task"]},
                {"id": "auto_stamp_ci_loop", "confidence": "high", "regex": ["auto-stamp", "\\bmergify\\b", "\\bci\\b.*\\b(rebase|workflow|merge)\\b"]},
                {"id": "worktree_ssh_delegation", "confidence": "high", "regex": ["another worktree", "\\bworktree\\b", "ssh machine", "ssh machines"]},
                {"id": "run_fix_repro_loop", "confidence": "medium", "regex": ["keep going", "continue fixing", "fix ci", "run ./run.sh", "root cause", "repro script"]},
            ],
        },
        {
            "id": "request_domain",
            "default": "uncategorized",
            "continue_from": ["other"],
            "rules": [
                {"id": "pr_review", "confidence": "high", "regex": ["\\bpr\\b", "\\bpull request\\b", "\\breview\\b", "pr summary", "pr body"]},
                {"id": "invoker_plan_submission", "confidence": "high", "regex": ["\\binvoker\\b", "plan-to-invoker", "submit to invoker", "workflow chain", "workflow submission"]},
                {"id": "ui_terminal_visual", "confidence": "high", "regex": ["\\bui\\b", "terminal", "screenshot", "visual proof", "playwright", "embedded pty", "graph"]},
                {"id": "dependency_setup", "confidence": "medium", "regex": ["\\binstall\\b", "\\bdependency\\b", "\\bdependencies\\b", "\\bpnpm\\b", "\\bnpm\\b", "\\bpip\\b", "\\bbundler\\b"]},
                {"id": "git_branch_stack", "confidence": "high", "regex": ["\\bgit\\b", "\\bbranch\\b", "\\bstack\\b", "\\brebase\\b", "\\bmerge\\b", "upstream/master", "origin/master"]},
                {"id": "release_packaging", "confidence": "medium", "regex": ["\\brelease\\b", "\\bpackage\\b", "\\bversion\\b", "changelog"]},
                {"id": "test_ci_failure", "confidence": "high", "regex": ["\\bci\\b", "test failure", "failing test", "\\bpytest\\b", "make test", "build failed"]},
                {"id": "debug_repro", "confidence": "high", "regex": ["\\brepro\\b", "root cause", "\\bdebug\\b", "\\binvestigate\\b", "failure analysis"]},
            ],
        },
        {
            "id": "request_leaf",
            "default": "$current",
            "continue_from": [
                "previous_agent_plan",
                "auto_stamp_ci_loop",
                "upstream_task_handoff",
                "implement_plan",
                "worktree_ssh_delegation",
                "run_fix_repro_loop",
                "git_branch_stack",
                "debug_repro",
                "ui_terminal_visual",
                "uncategorized",
            ],
            "rules": [
                {"id": "previous_agent_plan_resume", "confidence": "high", "regex": ["fresh context", "carry the work through", "previous agent produced the plan below"]},
                {"id": "experiment_proof", "confidence": "high", "regex": ["experiment proof", "proof artifact", "capture.*proof"]},
                {"id": "workflow_recreate_rebase", "confidence": "high", "regex": ["recreate all workflows", "workflow.*rebase", "rebase.*workflow"]},
                {"id": "master_branch_sync", "confidence": "high", "regex": ["master branch sync", "sync.*master", "upstream/master", "origin/master"]},
                {"id": "failure_diagnosis", "confidence": "high", "regex": ["failure diagnosis", "root cause", "failure analysis", "diagnose.*fail"]},
                {"id": "fix_with_agent_conflict_resolution", "confidence": "high", "regex": ["fix with agent", "agent conflict", "conflict resolution", "merge conflict"]},
                {"id": "implementation_refactor", "confidence": "medium", "regex": ["\\bimplement\\b", "\\brefactor\\b", "code change", "make the change"]},
                {"id": "cost_usage_analysis", "confidence": "high", "regex": ["cost usage", "usage metrics", "mixpanel", "token cost", "cost analysis"]},
                {"id": "demo_video_visual_proof", "confidence": "high", "regex": ["demo video", "video proof", "visual proof", "playwright.*video"]},
            ],
        },
    ],
}


def to_int(value: Any, default: int = 0) -> int:
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_optional_float(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def normalize_preview(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text[:limit]


def normalize_label(text: str, limit: int = 72) -> str:
    text = " ".join(text.split())
    text = re.sub(r"^#+\s*", "", text).strip(" :-#`'\"")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text.lower()).strip("_")
    return text[:limit].strip("_") or "uncategorized"


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_report_date() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def report_epoch(report_date: str) -> int:
    parsed = datetime.strptime(report_date, "%Y-%m-%d").date()
    return int(datetime.combine(parsed, dt_time(12, 0), tzinfo=timezone.utc).timestamp())


def row_report_date(row: dict[str, str], fallback: str) -> str:
    value = (row.get("session_date") or "").strip()
    if not value:
        return fallback
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return fallback
    return value


def is_after_report_date(value: str, report_date: str) -> bool:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() > datetime.strptime(report_date, "%Y-%m-%d").date()
    except ValueError:
        return False


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON at {path}: {exc}") from exc


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def insert_id(report_date: str, family: str, key: str) -> str:
    return f"u3-{digest_text(f'{report_date}|{family}|{key}')[:32]}"


def insert_id_v4(report_date: str, family: str, key: str) -> str:
    return f"u4-{digest_text(f'{report_date}|{family}|{key}')[:32]}"


def session_identity(file_path: str) -> str:
    """Return a stable session identifier across different machine paths."""
    if not file_path:
        return "unknown-session"
    name = Path(file_path).name
    # Most session files are <id>.jsonl; stem gives stable ID.
    stem = Path(name).stem
    if stem:
        return stem
    return digest_text(file_path)[:24]


@dataclass
class ExportEvent:
    family: str
    event: str
    insert_id: str
    properties: dict[str, Any]


class StateStore:
    def __init__(self, path: Path, max_ids: int) -> None:
        self.path = path
        self.max_ids = max_ids
        self.sent: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return
        raw = payload.get("sent_insert_ids", {})
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            if isinstance(key, str):
                self.sent[key] = to_int(value, int(time.time()))

    def has(self, row_id: str) -> bool:
        return row_id in self.sent

    def add_many(self, row_ids: Iterable[str]) -> None:
        now = int(time.time())
        for row_id in row_ids:
            self.sent[row_id] = now
        if len(self.sent) <= self.max_ids:
            return
        ordered = sorted(self.sent.items(), key=lambda item: item[1], reverse=True)
        self.sent = dict(ordered[: self.max_ids])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"sent_insert_ids": self.sent}, indent=2, sort_keys=True))


@dataclass
class TaskClassification:
    task_type: str
    task_type_label: str
    task_type_confidence: str
    task_type_classifier: str
    task_type_reason: str
    task_type_source: str
    task_type_config_version: str


@dataclass
class RequestPatternClassification:
    request_pattern: str
    request_pattern_path: str
    request_pattern_depth: int
    request_pattern_rule_id: str
    request_pattern_confidence: str
    request_pattern_config_version: str


class TaskClassificationCache:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.values: dict[str, dict[str, Any]] = {}
        self.dirty = False
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            return
        values = payload.get("classifications", {})
        if isinstance(values, dict):
            self.values = {str(key): value for key, value in values.items() if isinstance(value, dict)}

    def get(self, key: str) -> dict[str, Any] | None:
        return self.values.get(key)

    def set(self, key: str, value: dict[str, Any]) -> None:
        self.values[key] = {**value, "cached_at": int(time.time())}
        self.dirty = True

    def save(self) -> None:
        if self.path is None or not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"classifications": self.values}, indent=2, sort_keys=True))


def read_config_file(path: Path) -> dict[str, Any]:
    text = path.read_text()
    if path.suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError:
            obj = parse_simple_yaml(text)
        else:
            obj = yaml.safe_load(text)
    else:
        obj = json.loads(text)
    if not isinstance(obj, dict):
        raise RuntimeError(f"Task categorization config must be a mapping: {path}")
    return obj


def yaml_scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "Null", "~"}:
        return None
    if value[0:1] in {"'", '"'}:
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value.strip("'\"")
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def parse_simple_yaml(text: str) -> dict[str, Any]:
    raw_lines = text.splitlines()
    lines: list[tuple[int, str]] = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((len(line) - len(line.lstrip(" ")), line.lstrip(" ")))

    def collect_block(index: int, parent_indent: int) -> tuple[str, int]:
        block_lines: list[str] = []
        while index < len(lines) and lines[index][0] > parent_indent:
            indent, content = lines[index]
            strip_count = min(indent, parent_indent + 2)
            block_lines.append((" " * max(0, indent - strip_count)) + content)
            index += 1
        return "\n".join(block_lines).rstrip() + "\n", index

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines):
            return {}, index
        if lines[index][1].startswith("- "):
            values: list[Any] = []
            while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
                item = lines[index][1][2:].strip()
                index += 1
                if not item:
                    child, index = parse_block(index, indent + 2)
                    values.append(child)
                elif ":" in item and not item.startswith(("'", '"')):
                    key, raw_value = item.split(":", 1)
                    mapping: dict[str, Any] = {}
                    if raw_value.strip():
                        mapping[key.strip()] = yaml_scalar(raw_value.strip())
                    else:
                        child, index = parse_block(index, indent + 2)
                        mapping[key.strip()] = child
                    while index < len(lines) and lines[index][0] == indent + 2 and not lines[index][1].startswith("- "):
                        key2, raw_value2 = lines[index][1].split(":", 1)
                        index += 1
                        raw_value2 = raw_value2.strip()
                        if raw_value2 == "|":
                            mapping[key2.strip()], index = collect_block(index, indent + 2)
                        elif raw_value2:
                            mapping[key2.strip()] = yaml_scalar(raw_value2)
                        else:
                            child, index = parse_block(index, indent + 4)
                            mapping[key2.strip()] = child
                    values.append(mapping)
                else:
                    values.append(yaml_scalar(item))
            return values, index

        mapping: dict[str, Any] = {}
        while index < len(lines) and lines[index][0] == indent and not lines[index][1].startswith("- "):
            key, raw_value = lines[index][1].split(":", 1)
            index += 1
            raw_value = raw_value.strip()
            if raw_value == "|":
                mapping[key.strip()], index = collect_block(index, indent)
            elif raw_value:
                mapping[key.strip()] = yaml_scalar(raw_value)
            else:
                child, index = parse_block(index, indent + 2)
                mapping[key.strip()] = child
        return mapping, index

    parsed, final_index = parse_block(0, lines[0][0] if lines else 0)
    if final_index != len(lines) or not isinstance(parsed, dict):
        raise RuntimeError("YAML config uses unsupported syntax; install PyYAML for full YAML support.")
    return parsed


def load_task_categorization_config(path_value: str = "") -> dict[str, Any]:
    if not path_value:
        return json.loads(json.dumps(DEFAULT_TASK_CATEGORIZATION_CONFIG))
    path = Path(path_value).expanduser()
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_TASK_CATEGORIZATION_CONFIG))
    return read_config_file(path)


def load_request_pattern_config(path_value: str = "") -> dict[str, Any]:
    if not path_value:
        return json.loads(json.dumps(DEFAULT_REQUEST_PATTERN_CONFIG))
    path = Path(path_value).expanduser()
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_REQUEST_PATTERN_CONFIG))
    return read_config_file(path)


def request_pattern_context_fields(config: dict[str, Any], row: dict[str, str]) -> list[tuple[str, float, str]]:
    field_specs = ((config.get("context", {}) or {}).get("fields", []) or [])
    fields: list[tuple[str, float, str]] = []
    for field in field_specs:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "")
        if name:
            fields.append((name, to_float(field.get("weight"), 1.0), str(row.get(name, ""))))
    if fields:
        return fields
    return [("prompt_preview", 1.0, str(row.get("prompt_preview", "")))]


def validate_request_pattern_config(config: dict[str, Any]) -> None:
    layers = config.get("layers", [])
    if not isinstance(layers, list) or not layers:
        raise RuntimeError("Request pattern config must define at least one layer.")

    layer_ids: set[str] = set()
    known_ids = {"$current"}
    rule_ids: set[str] = set()
    for layer in layers:
        if not isinstance(layer, dict):
            raise RuntimeError("Request pattern layers must be mappings.")
        layer_id = str(layer.get("id") or "")
        if not layer_id:
            raise RuntimeError("Request pattern layer is missing id.")
        if layer_id in layer_ids:
            raise RuntimeError(f"Duplicate request pattern layer id: {layer_id}")
        layer_ids.add(layer_id)
        default_id = str(layer.get("default") or "")
        if not default_id:
            raise RuntimeError(f"Request pattern layer {layer_id} is missing default.")
        known_ids.add(default_id)
        rules = layer.get("rules", [])
        if not isinstance(rules, list):
            raise RuntimeError(f"Request pattern layer {layer_id} rules must be a list.")
        for rule in rules:
            if not isinstance(rule, dict):
                raise RuntimeError("Request pattern rules must be mappings.")
            rule_id = str(rule.get("id") or "")
            if not rule_id:
                raise RuntimeError(f"Request pattern layer {layer_id} has a rule missing id.")
            if rule_id in rule_ids:
                raise RuntimeError(f"Duplicate request pattern rule id: {rule_id}")
            rule_ids.add(rule_id)
            known_ids.add(rule_id)
            regexes = rule.get("regex", [])
            if not isinstance(regexes, list) or not regexes:
                raise RuntimeError(f"Request pattern rule {rule_id} must define a non-empty regex list.")
            for expr in regexes:
                try:
                    re.compile(str(expr), re.IGNORECASE)
                except re.error as exc:
                    raise RuntimeError(f"Invalid request pattern regex for {rule_id}: {expr}: {exc}") from exc

    for layer in layers[1:]:
        refs = layer.get("continue_from", [])
        if refs is None:
            refs = []
        if not isinstance(refs, list):
            raise RuntimeError(f"Request pattern layer {layer.get('id')} continue_from must be a list.")
        for ref in refs:
            if str(ref) not in known_ids:
                raise RuntimeError(f"Request pattern layer {layer.get('id')} has unknown continue_from reference: {ref}")


class RequestPatternCategorizer:
    def __init__(self, config: dict[str, Any]) -> None:
        validate_request_pattern_config(config)
        self.config = config
        self.version = str(config.get("version") or "unknown")

    def classify_layer(self, layer: dict[str, Any], row: dict[str, str]) -> tuple[str, str, str]:
        winner: tuple[float, str, str, str] | None = None
        for rule_index, rule in enumerate(layer.get("rules", []) or []):
            priority = to_float(rule.get("priority"), float(len(layer.get("rules", [])) - rule_index))
            rule_id = str(rule.get("id") or "")
            for field_name, weight, value in request_pattern_context_fields(self.config, row):
                if not value:
                    continue
                for expr in rule.get("regex", []) or []:
                    if re.search(str(expr), value, re.IGNORECASE):
                        score = priority + weight
                        if winner is None or score > winner[0]:
                            winner = (score, rule_id, str(rule.get("confidence") or "medium"), f"{layer.get('id')}:{rule_id}:{field_name}")
        if winner is None:
            return str(layer.get("default") or "uncategorized"), "low", f"{layer.get('id')}:default"
        _score, rule_id, confidence, reason = winner
        return rule_id, confidence, reason

    def classify(self, row: dict[str, str]) -> RequestPatternClassification:
        path: list[str] = []
        current = ""
        rule_id = "default"
        confidence = "low"
        for index, layer in enumerate(self.config.get("layers", []) or []):
            if index > 0:
                refs = [str(item) for item in (layer.get("continue_from", []) or [])]
                if refs and current not in refs:
                    continue
            result, confidence, rule_id = self.classify_layer(layer, row)
            if result == "$current":
                result = current or "uncategorized"
            current = result
            if not path or path[-1] != current:
                path.append(current)
        if not current:
            current = "uncategorized"
            path = [current]
        return RequestPatternClassification(
            request_pattern=current,
            request_pattern_path="/".join(path),
            request_pattern_depth=len(path),
            request_pattern_rule_id=rule_id,
            request_pattern_confidence=confidence,
            request_pattern_config_version=self.version,
        )


def validate_task_categorization_config(config: dict[str, Any]) -> None:
    category_ids: set[str] = set()
    for category in config.get("categories", []) or []:
        if not isinstance(category, dict):
            raise RuntimeError("Task categorization categories must be mappings.")
        category_id = str(category.get("id") or "")
        if not category_id:
            raise RuntimeError("Task categorization category is missing id.")
        if category_id in category_ids:
            raise RuntimeError(f"Duplicate task categorization category id: {category_id}")
        category_ids.add(category_id)
    if not category_ids:
        raise RuntimeError("Task categorization config must define at least one category.")
    default_id = str((config.get("defaults", {}) or {}).get("id") or "uncategorized")
    if default_id not in category_ids:
        raise RuntimeError(f"Task categorization default id references unknown category: {default_id}")

    for classifier in config.get("classifiers", []) or []:
        if not isinstance(classifier, dict) or not to_bool(classifier.get("enabled", True)):
            continue
        if classifier.get("type") != "regex":
            continue
        for rule in classifier.get("rules", []) or []:
            if not isinstance(rule, dict):
                raise RuntimeError("Task categorization regex rules must be mappings.")
            rule_id = str(rule.get("id") or "")
            if rule_id not in category_ids:
                raise RuntimeError(f"Task categorization rule references unknown category id: {rule_id}")
            for expr in rule.get("regex", []) or []:
                try:
                    re.compile(str(expr), re.IGNORECASE)
                except re.error as exc:
                    raise RuntimeError(f"Invalid task categorization regex for {rule_id}: {expr}: {exc}") from exc


class TaskCategorizer:
    def __init__(self, config: dict[str, Any], cache: TaskClassificationCache | None = None) -> None:
        validate_task_categorization_config(config)
        self.config = config
        self.version = str(config.get("version") or "unknown")
        self.cache = cache or TaskClassificationCache(None)
        self.categories = {str(item["id"]): str(item.get("label") or item["id"]) for item in config.get("categories", [])}
        defaults = config.get("defaults", {}) or {}
        default_id = str(defaults.get("id") or "uncategorized")
        self.default = TaskClassification(
            task_type=default_id,
            task_type_label=self.categories.get(default_id, default_id),
            task_type_confidence=str(defaults.get("confidence") or "low"),
            task_type_classifier="default",
            task_type_reason=str(defaults.get("reason") or "no_rule_matched"),
            task_type_source="",
            task_type_config_version=self.version,
        )

    def configured_fields(self, row: dict[str, str]) -> list[tuple[str, float, str]]:
        field_specs = ((self.config.get("context", {}) or {}).get("fields", []) or [])
        fields: list[tuple[str, float, str]] = []
        for field in field_specs:
            if not isinstance(field, dict):
                continue
            name = str(field.get("name") or "")
            if not name:
                continue
            fields.append((name, to_float(field.get("weight"), 1.0), str(row.get(name, ""))))
        return fields

    def context_hash(self, row: dict[str, str]) -> str:
        payload = {name: value for name, _weight, value in self.configured_fields(row)}
        return digest_text(json.dumps(payload, sort_keys=True))

    def context_text(self, row: dict[str, str]) -> str:
        max_chars = to_int((self.config.get("context", {}) or {}).get("max_chars"), 4000)
        parts = []
        for name, _weight, value in self.configured_fields(row):
            if value:
                parts.append(f"{name}: {value}")
        return "\n".join(parts)[:max_chars]

    def classify_regex(self, classifier: dict[str, Any], row: dict[str, str]) -> TaskClassification | None:
        winner: tuple[float, dict[str, Any], str] | None = None
        for rule in classifier.get("rules", []) or []:
            priority = to_float(rule.get("priority"), 0.0)
            for name, weight, value in self.configured_fields(row):
                if not value:
                    continue
                for expr in rule.get("regex", []) or []:
                    if re.search(str(expr), value, re.IGNORECASE):
                        score = priority + weight
                        if winner is None or score > winner[0]:
                            winner = (score, rule, name)
        if winner is None:
            return None
        _score, rule, source = winner
        task_type = str(rule.get("id") or self.default.task_type)
        return TaskClassification(
            task_type=task_type,
            task_type_label=self.categories.get(task_type, task_type),
            task_type_confidence=str(rule.get("confidence") or "medium"),
            task_type_classifier=str(classifier.get("id") or "regex"),
            task_type_reason=f"regex_rule:{task_type}",
            task_type_source=source,
            task_type_config_version=self.version,
        )

    def classify_codex(self, classifier: dict[str, Any], row: dict[str, str], current: TaskClassification) -> TaskClassification:
        if classifier.get("mode") == "uncategorized_only" and current.task_type != self.default.task_type:
            return current
        cache_key = f"{self.version}:{classifier.get('id')}:{self.context_hash(row)}"
        cached = self.cache.get(cache_key)
        if cached:
            return self.result_from_payload(cached, str(classifier.get("id") or "codex"), "cache")

        result = self.run_codex_classifier(classifier, row)
        if result is None:
            return TaskClassification(**{**current.__dict__, "task_type_reason": "codex_failed_fallback"})
        self.cache.set(cache_key, result.__dict__)
        return result

    def result_from_payload(self, payload: dict[str, Any], classifier_id: str, source: str) -> TaskClassification:
        task_type = str(payload.get("task_type") or payload.get("id") or self.default.task_type)
        return TaskClassification(
            task_type=task_type,
            task_type_label=self.categories.get(task_type, task_type),
            task_type_confidence=str(payload.get("task_type_confidence") or payload.get("confidence") or "low"),
            task_type_classifier=str(payload.get("task_type_classifier") or classifier_id),
            task_type_reason=str(payload.get("task_type_reason") or payload.get("reason") or ""),
            task_type_source=str(payload.get("task_type_source") or source),
            task_type_config_version=self.version,
        )

    def run_codex_classifier(self, classifier: dict[str, Any], row: dict[str, str]) -> TaskClassification | None:
        import tempfile

        classifier_id = str(classifier.get("id") or "codex")
        categories_text = "\n".join(f"- {category_id}: {label}" for category_id, label in self.categories.items())
        prompt = (
            str(classifier.get("prompt") or "")
            .replace("{categories}", categories_text)
            .replace("{context}", self.context_text(row))
        )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "enum": sorted(self.categories)},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "reason": {"type": "string"},
            },
            "required": ["id", "confidence", "reason"],
        }
        try:
            with tempfile.TemporaryDirectory(prefix="task-classifier-") as tmpdir:
                schema_path = Path(tmpdir) / "schema.json"
                output_path = Path(tmpdir) / "output.json"
                schema_path.write_text(json.dumps(schema))
                command = [
                    str(part).format(schema_path=str(schema_path), output_path=str(output_path))
                    for part in (classifier.get("command") or [])
                ]
                if not command:
                    return None
                subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=to_int(classifier.get("timeout_seconds"), 90),
                    check=True,
                )
                payload = json.loads(output_path.read_text())
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TimeoutError):
            return None
        if not isinstance(payload, dict):
            return None
        task_type = str(payload.get("id") or "")
        if task_type not in self.categories:
            return None
        return TaskClassification(
            task_type=task_type,
            task_type_label=self.categories.get(task_type, task_type),
            task_type_confidence=str(payload.get("confidence") or "low"),
            task_type_classifier=classifier_id,
            task_type_reason=str(payload.get("reason") or ""),
            task_type_source="codex",
            task_type_config_version=self.version,
        )

    def classify(self, row: dict[str, str]) -> TaskClassification:
        result = self.default
        for classifier in self.config.get("classifiers", []) or []:
            if not isinstance(classifier, dict) or not to_bool(classifier.get("enabled", True)):
                continue
            classifier_type = classifier.get("type")
            if classifier_type == "regex":
                regex_result = self.classify_regex(classifier, row)
                if regex_result is not None:
                    result = regex_result
            elif classifier_type == "codex":
                result = self.classify_codex(classifier, row, result)
        return result


def with_common(
    token: str,
    distinct_id: str,
    epoch: int,
    row_id: str,
    report_date: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    base = {
        "token": token,
        "distinct_id": distinct_id,
        "time": epoch,
        "$insert_id": row_id,
        "report_date": report_date,
        "export_version": os.getenv("USAGE_EXPORT_VERSION", "session_date_v3"),
    }
    base.update(extra)
    return base


def build_daily_rollups(report: dict[str, Any], audit: dict[str, Any], token: str, distinct_id: str, epoch: int, report_date: str) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    for section_name in ("combined", "codex", "claude"):
        section = report.get(section_name, {})
        totals = section.get("totals", {})
        row_id = insert_id(report_date, "usage_daily_rollup", f"{section_name}:all")
        props = with_common(token, distinct_id, epoch, row_id, report_date, {
            "section": section_name,
            "bucket": "all",
            "estimated_cost_usd": to_float(totals.get("estimated_cost_usd")),
            "session_count": to_int(totals.get("session_count")),
            "planning_session_count": to_int(totals.get("planning_session_count")),
            "execution_session_count": to_int(totals.get("execution_session_count")),
            "effective_input_10pct": to_float(totals.get("effective_input_10pct")),
            "source": "planning_vs_execution_report",
        })
        events.append(ExportEvent("usage_daily_rollup", "usage_daily_rollup", row_id, props))

        for bucket_name in ("planning", "execution"):
            bucket = section.get(bucket_name, {}).get("totals", {})
            row_id = insert_id(report_date, "usage_daily_rollup", f"{section_name}:{bucket_name}")
            props = with_common(token, distinct_id, epoch, row_id, report_date, {
                "section": section_name,
                "bucket": bucket_name,
                "estimated_cost_usd": to_float(bucket.get("estimated_cost_usd")),
                "input_tokens": to_float(bucket.get("input_tokens")),
                "cached_input_tokens": to_float(bucket.get("cached_input_tokens")),
                "output_tokens": to_float(bucket.get("output_tokens")),
                "total_tokens": to_float(bucket.get("total_tokens")),
                "cache_hit_pct": to_float(bucket.get("cache_hit_pct")),
                "source": "planning_vs_execution_report",
            })
            events.append(ExportEvent("usage_daily_rollup", "usage_daily_rollup", row_id, props))

    for provider in ("codex", "claude"):
        dedup_daily = ((audit.get("dedup", {}) or {}).get(provider, {}) or {}).get("ccusageDaily", {})
        row_id = insert_id(report_date, "usage_daily_rollup", f"audit:{provider}:dedup")
        props = with_common(token, distinct_id, epoch, row_id, report_date, {
            "section": provider,
            "bucket": "dedup_daily",
            "input_tokens": to_float(dedup_daily.get("inputTokens")),
            "cached_input_tokens": to_float(dedup_daily.get("cachedInputTokens") or dedup_daily.get("cacheReadTokens")),
            "output_tokens": to_float(dedup_daily.get("outputTokens")),
            "total_tokens": to_float(dedup_daily.get("totalTokens")),
            "estimated_cost_usd": to_float(dedup_daily.get("costUSD") or dedup_daily.get("totalCost")),
            "cache_hit_pct": to_float(dedup_daily.get("cacheHitPct")),
            "source": "cache_hit_audit_report",
        })
        events.append(ExportEvent("usage_daily_rollup", "usage_daily_rollup", row_id, props))
    return events


def build_session_events(rows: list[dict[str, str]], token: str, distinct_id: str, epoch: int, report_date: str) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    for row in rows:
        session_file = row.get("file", "")
        session_id = session_identity(session_file)
        model = row.get("model", "")
        bucket = row.get("bucket", "")
        canonical_key = f"{model}:{bucket}:{session_id}"
        event_date = row_report_date(row, report_date)
        if is_after_report_date(event_date, report_date):
            continue
        row_id = insert_id(event_date, "usage_session", canonical_key)
        props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
            "model": model,
            "provider": row.get("provider", ""),
            "billable_model": row.get("billable_model", ""),
            "billable_model_source": row.get("billable_model_source", ""),
            "usage_source": row.get("usage_source", ""),
            "bucket": bucket,
            "batch_report_date": report_date,
            "session_id": session_id,
            "session_file": session_file,
            "canonical_key": canonical_key,
            "user_prompts": to_int(row.get("user_prompts")),
            "agent_messages": to_int(row.get("agent_messages")),
            "tool_calls": to_int(row.get("tool_calls")),
            "function_outputs": to_int(row.get("function_outputs")),
            "input_tokens": to_float(row.get("input_tokens")),
            "cache_read_input_tokens": to_float(row.get("cache_read_input_tokens") or row.get("cached_input_tokens")),
            "cached_input_tokens": to_float(row.get("cached_input_tokens")),
            "cache_creation_input_tokens": to_float(row.get("cache_creation_input_tokens")),
            "output_tokens": to_float(row.get("output_tokens")),
            "reasoning_output_tokens": to_float(row.get("reasoning_output_tokens")),
            "total_tokens": to_float(row.get("total_tokens")),
            "cache_hit_pct": to_float(row.get("cache_hit_pct")),
            "estimated_cost_usd": to_float(row.get("estimated_cost_usd")),
            "derived_input_cost_usd": to_optional_float(row.get("derived_input_cost_usd")),
            "derived_non_cache_input_cost_usd": to_optional_float(row.get("derived_non_cache_input_cost_usd")),
            "derived_cache_read_cost_usd": to_optional_float(row.get("derived_cache_read_cost_usd")),
            "derived_cache_creation_cost_usd": to_optional_float(row.get("derived_cache_creation_cost_usd")),
            "derived_output_cost_usd": to_optional_float(row.get("derived_output_cost_usd")),
            "derived_total_cost_usd": to_optional_float(row.get("derived_total_cost_usd")),
            "pricing_missing": to_bool(row.get("pricing_missing")),
            "pricing_source": row.get("pricing_source", ""),
            "session_cwd": row.get("session_cwd", ""),
            "first_prompt_preview": normalize_preview(row.get("first_prompt_preview", ""), 120),
        })
        events.append(ExportEvent("usage_session", "usage_session", row_id, props))
    return events


def build_prompt_events(
    rows: list[dict[str, str]],
    token: str,
    distinct_id: str,
    epoch: int,
    report_date: str,
    max_unique_prompt_hashes: int,
) -> tuple[list[ExportEvent], int]:
    events: list[ExportEvent] = []
    hashes: set[str] = set()
    skipped = 0
    for row in rows:
        preview = row.get("prompt_preview", "")
        preview_hash = digest_text(preview)
        if preview_hash not in hashes and len(hashes) >= max_unique_prompt_hashes:
            skipped += 1
            continue
        hashes.add(preview_hash)

        session_id = session_identity(row.get("file", ""))
        prompt_index = to_int(row.get("prompt_index"))
        canonical_key = f"{row.get('model','')}:{row.get('bucket','')}:{session_id}:{prompt_index}"
        event_date = row_report_date(row, report_date)
        if is_after_report_date(event_date, report_date):
            continue
        row_id = insert_id(
            event_date,
            "usage_prompt",
            canonical_key,
        )
        props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
            "model": row.get("model", ""),
            "provider": row.get("provider", ""),
            "billable_model": row.get("billable_model", ""),
            "billable_model_source": row.get("billable_model_source", ""),
            "usage_source": row.get("usage_source", ""),
            "bucket": row.get("bucket", ""),
            "batch_report_date": report_date,
            "session_id": session_id,
            "session_file": row.get("file", ""),
            "prompt_index": prompt_index,
            "prompt_hash": preview_hash,
            "prompt_preview": normalize_preview(preview),
            "session_cwd": row.get("session_cwd", ""),
            "previous_prompt_preview": normalize_preview(row.get("previous_prompt_preview", ""), 160),
            "first_prompt_preview": normalize_preview(row.get("first_prompt_preview", ""), 160),
            "final_answer_preview": normalize_preview(row.get("final_answer_preview", ""), 160),
            "canonical_key": canonical_key,
            "tool_calls": to_int(row.get("tool_calls")),
            "agent_messages": to_int(row.get("agent_messages")),
            "response_messages": to_int(row.get("response_messages")),
            "function_outputs": to_int(row.get("function_outputs")),
            "input_tokens_delta": to_float(row.get("input_tokens_delta")),
            "cache_read_tokens_delta": to_float(row.get("cache_read_tokens_delta") or row.get("cached_tokens_delta")),
            "cached_tokens_delta": to_float(row.get("cached_tokens_delta")),
            "cache_creation_tokens_delta": to_float(row.get("cache_creation_tokens_delta")),
            "output_tokens_delta": to_float(row.get("output_tokens_delta")),
            "reasoning_tokens_delta": to_float(row.get("reasoning_tokens_delta")),
            "total_tokens_delta": to_float(row.get("total_tokens_delta")),
            "cache_hit_pct": to_float(row.get("cache_hit_pct")),
            "estimated_cost_usd": to_float(row.get("estimated_cost_usd")),
            "derived_input_cost_usd": to_optional_float(row.get("derived_input_cost_usd")),
            "derived_non_cache_input_cost_usd": to_optional_float(row.get("derived_non_cache_input_cost_usd")),
            "derived_cache_read_cost_usd": to_optional_float(row.get("derived_cache_read_cost_usd")),
            "derived_cache_creation_cost_usd": to_optional_float(row.get("derived_cache_creation_cost_usd")),
            "derived_output_cost_usd": to_optional_float(row.get("derived_output_cost_usd")),
            "derived_total_cost_usd": to_optional_float(row.get("derived_total_cost_usd")),
            "pricing_missing": to_bool(row.get("pricing_missing")),
            "pricing_source": row.get("pricing_source", ""),
        })
        events.append(ExportEvent("usage_prompt", "usage_prompt", row_id, props))
    return events, skipped


def first_markdown_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            candidate = stripped.lstrip("#").strip()
            if candidate:
                return candidate
    return ""


def first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        candidate = line.strip(" -#`:\t")
        if not candidate:
            continue
        lower = candidate.lower()
        if lower.startswith(("<environment_context>", "summary", "key changes", "tests and validation", "assumptions")):
            continue
        if len(candidate) > 3:
            return candidate
    return ""


def cwd_label(cwd: str) -> str:
    if not cwd:
        return ""
    path = Path(cwd)
    parts = [part for part in path.parts if part and part not in {"/", "Users", "edbertchan", ".invoker", "worktrees", "merge-clones"}]
    if not parts:
        return path.name
    return parts[-1]


def derive_task_label(row: dict[str, str], pattern: str) -> tuple[str, str, str]:
    preview = row.get("prompt_preview", "")
    previous = row.get("previous_prompt_preview", "")
    first = row.get("first_prompt_preview", "")
    final = row.get("final_answer_preview", "")
    cwd = row.get("session_cwd", "")

    candidates: list[tuple[str, str, str]] = []
    if pattern == "implement_plan" and previous:
        candidates.append((previous, "previous_prompt", "high"))
    if first:
        candidates.append((first, "first_prompt", "high" if pattern != "other" else "medium"))
    if previous:
        candidates.append((previous, "previous_prompt", "medium"))
    if cwd:
        candidates.append((cwd_label(cwd), "session_cwd", "medium"))
    if final:
        candidates.append((final, "final_answer", "low"))
    candidates.append((preview, "prompt_preview", "low"))

    for text, source, confidence in candidates:
        label_text = first_markdown_heading(text) or first_meaningful_line(text) or text
        label = normalize_label(label_text)
        if label and label not in {"implement_the_plan", "other", "continue", "keep_going"}:
            return label, source, confidence
    return "uncategorized", "prompt_preview", "low"


def repeated_value_rows(audit: dict[str, Any], model: str) -> list[dict[str, Any]]:
    key = "topCodexRepeatedValues" if model == "codex" else "topClaudeRepeatedValues" if model == "claude" else ""
    if not key:
        return []
    rows = ((audit.get("repeatBreakdown", {}) or {}).get(key, []) or [])
    return sorted(rows, key=lambda row: to_float(row.get("tokenEstimateTotal")), reverse=True)


def pattern_source(model: str, pattern: str) -> str:
    prompt_context_patterns = {
        "previous_agent_plan",
        "previous_agent_plan_resume",
        "implement_plan",
        "implementation_refactor",
        "upstream_task_handoff",
        "worktree_ssh_delegation",
        "run_fix_repro_loop",
        "failure_diagnosis",
        "fix_with_agent_conflict_resolution",
        "workflow_recreate_rebase",
        "master_branch_sync",
    }
    if pattern not in prompt_context_patterns:
        return ""
    if model == "codex":
        return "codex.user_message_prefix180"
    if model == "claude":
        return "claude.enqueue_content"
    return ""


def source_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw_value = str(row.get("value", ""))
    return {
        "source": str(row.get("source", "")),
        "value_hash": digest_text(raw_value),
        "source_preview": normalize_preview(raw_value, 120),
        "source_occurrence_count": to_int(row.get("count")),
        "source_chars": to_int(row.get("chars")),
        "estimated_source_tokens_per_request": to_float(row.get("tokenEstimatePerValue")),
        "source_token_estimate_total": to_float(row.get("tokenEstimateTotal")),
        "source_shrinkability_score": to_float(row.get("shrinkabilityScore")),
    }


def choose_primary_cache_source(audit: dict[str, Any], model: str, pattern: str) -> tuple[dict[str, Any], str, str]:
    rows = repeated_value_rows(audit, model)
    preferred_source = pattern_source(model, pattern)
    if preferred_source:
        for row in rows:
            if str(row.get("source", "")) == preferred_source:
                return source_payload(row), "prompt_pattern", "medium"
    if rows:
        return source_payload(rows[0]), "global_repeated_context", "low"
    return {}, "none", "low"


def request_base_payload(
    row: dict[str, str],
    report_date: str,
    task_categorizer: TaskCategorizer,
    request_pattern_categorizer: RequestPatternCategorizer,
    include_task_type: bool = True,
) -> tuple[str, str, int, str, dict[str, Any]]:
    preview = row.get("prompt_preview", "")
    session_id = session_identity(row.get("file", ""))
    prompt_index = to_int(row.get("prompt_index"))
    canonical_key = f"{row.get('model','')}:{row.get('bucket','')}:{session_id}:{prompt_index}"
    event_date = row_report_date(row, report_date)
    pattern_result = request_pattern_categorizer.classify(row)
    pattern = pattern_result.request_pattern
    task_label, task_label_source, task_label_confidence = derive_task_label(row, pattern)
    payload = {
        "model": row.get("model", ""),
        "provider": row.get("provider", ""),
        "billable_model": row.get("billable_model", ""),
        "billable_model_source": row.get("billable_model_source", ""),
        "usage_source": row.get("usage_source", ""),
        "bucket": row.get("bucket", ""),
        "batch_report_date": report_date,
        "session_id": session_id,
        "session_file": row.get("file", ""),
        "prompt_index": prompt_index,
        "prompt_hash": digest_text(preview),
        "prompt_preview": normalize_preview(preview),
        "session_cwd": row.get("session_cwd", ""),
        "previous_prompt_preview": normalize_preview(row.get("previous_prompt_preview", ""), 160),
        "first_prompt_preview": normalize_preview(row.get("first_prompt_preview", ""), 160),
        "final_answer_preview": normalize_preview(row.get("final_answer_preview", ""), 160),
        "canonical_key": canonical_key,
        **pattern_result.__dict__,
        "task_label": task_label,
        "task_label_source": task_label_source,
        "task_label_confidence": task_label_confidence,
        "tool_calls": to_int(row.get("tool_calls")),
        "agent_messages": to_int(row.get("agent_messages")),
        "response_messages": to_int(row.get("response_messages")),
        "function_outputs": to_int(row.get("function_outputs")),
        "input_tokens_delta": to_float(row.get("input_tokens_delta")),
        "cache_read_tokens_delta": to_float(row.get("cache_read_tokens_delta") or row.get("cached_tokens_delta")),
        "cached_tokens_delta": to_float(row.get("cached_tokens_delta")),
        "cache_creation_tokens_delta": to_float(row.get("cache_creation_tokens_delta")),
        "output_tokens_delta": to_float(row.get("output_tokens_delta")),
        "reasoning_tokens_delta": to_float(row.get("reasoning_tokens_delta")),
        "total_tokens_delta": to_float(row.get("total_tokens_delta")),
        "cache_hit_pct": to_float(row.get("cache_hit_pct")),
        "estimated_cost_usd": to_float(row.get("estimated_cost_usd")),
        "derived_input_cost_usd": to_optional_float(row.get("derived_input_cost_usd")),
        "derived_non_cache_input_cost_usd": to_optional_float(row.get("derived_non_cache_input_cost_usd")),
        "derived_cache_read_cost_usd": to_optional_float(row.get("derived_cache_read_cost_usd")),
        "derived_cache_creation_cost_usd": to_optional_float(row.get("derived_cache_creation_cost_usd")),
        "derived_output_cost_usd": to_optional_float(row.get("derived_output_cost_usd")),
        "derived_total_cost_usd": to_optional_float(row.get("derived_total_cost_usd")),
        "pricing_missing": to_bool(row.get("pricing_missing")),
        "pricing_source": row.get("pricing_source", ""),
        "diagnosis_version": os.getenv("USAGE_DIAGNOSIS_VERSION", "request_pattern_layers_v1"),
        "source_attribution_method": "provider_metric_exact_source_estimated",
    }
    if include_task_type:
        payload.update(task_categorizer.classify(row).__dict__)
    return canonical_key, event_date, prompt_index, pattern, payload


def build_request_cache_diagnosis_events(
    rows: list[dict[str, str]],
    audit: dict[str, Any],
    token: str,
    distinct_id: str,
    report_date: str,
    max_unique_prompt_hashes: int,
    task_categorizer: TaskCategorizer,
    request_pattern_categorizer: RequestPatternCategorizer,
) -> tuple[list[ExportEvent], int]:
    events: list[ExportEvent] = []
    hashes: set[str] = set()
    skipped = 0
    for row in rows:
        preview_hash = digest_text(row.get("prompt_preview", ""))
        if preview_hash not in hashes and len(hashes) >= max_unique_prompt_hashes:
            skipped += 1
            continue
        hashes.add(preview_hash)

        canonical_key, event_date, _prompt_index, pattern, payload = request_base_payload(
            row, report_date, task_categorizer, request_pattern_categorizer
        )
        if is_after_report_date(event_date, report_date):
            continue
        primary, kind, confidence = choose_primary_cache_source(audit, row.get("model", ""), pattern)
        row_id = insert_id(event_date, "usage_request_cache_diagnosis", f"{canonical_key}:{payload['diagnosis_version']}")
        props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
            **payload,
            "primary_cache_driver_source": primary.get("source", ""),
            "primary_cache_driver_kind": kind,
            "source_attribution_confidence": confidence,
            "estimated_source_tokens_per_request": primary.get("estimated_source_tokens_per_request", 0.0),
            "source_token_estimate_total": primary.get("source_token_estimate_total", 0.0),
            "source_occurrence_count": primary.get("source_occurrence_count", 0),
            "source_shrinkability_score": primary.get("source_shrinkability_score", 0.0),
            "source_value_hash": primary.get("value_hash", ""),
            "source_preview": primary.get("source_preview", ""),
        })
        events.append(ExportEvent("usage_request_cache_diagnosis", "usage_request_cache_diagnosis", row_id, props))
    return events, skipped


def build_request_cache_source_events(
    rows: list[dict[str, str]],
    audit: dict[str, Any],
    token: str,
    distinct_id: str,
    report_date: str,
    max_unique_prompt_hashes: int,
    max_sources_per_request: int,
    task_categorizer: TaskCategorizer,
    request_pattern_categorizer: RequestPatternCategorizer,
) -> tuple[list[ExportEvent], int]:
    events: list[ExportEvent] = []
    hashes: set[str] = set()
    skipped = 0
    for row in rows:
        preview_hash = digest_text(row.get("prompt_preview", ""))
        if preview_hash not in hashes and len(hashes) >= max_unique_prompt_hashes:
            skipped += 1
            continue
        hashes.add(preview_hash)

        canonical_key, event_date, _prompt_index, pattern, payload = request_base_payload(
            row, report_date, task_categorizer, request_pattern_categorizer, include_task_type=False
        )
        if is_after_report_date(event_date, report_date):
            continue
        candidates = repeated_value_rows(audit, row.get("model", ""))
        preferred_source = pattern_source(row.get("model", ""), pattern)
        selected: list[dict[str, Any]] = []
        if preferred_source:
            selected.extend(row for row in candidates if str(row.get("source", "")) == preferred_source)
        for candidate in candidates:
            if len(selected) >= max_sources_per_request:
                break
            candidate_hash = digest_text(str(candidate.get("value", "")))
            if any(digest_text(str(existing.get("value", ""))) == candidate_hash for existing in selected):
                continue
            selected.append(candidate)

        for source_rank, candidate in enumerate(selected[:max_sources_per_request], start=1):
            source = source_payload(candidate)
            kind = "prompt_pattern" if preferred_source and source["source"] == preferred_source else "global_repeated_context"
            confidence = "medium" if kind == "prompt_pattern" else "low"
            source_key = f"{canonical_key}:{payload['diagnosis_version']}:{source['source']}:{source['value_hash']}"
            row_id = insert_id(event_date, "usage_request_cache_source", source_key)
            props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
                **payload,
                "source_rank": source_rank,
                "cache_driver_source": source["source"],
                "source_attribution_kind": kind,
                "source_attribution_confidence": confidence,
                "estimated_source_tokens_per_request": source["estimated_source_tokens_per_request"],
                "source_token_estimate_total": source["source_token_estimate_total"],
                "source_occurrence_count": source["source_occurrence_count"],
                "source_chars": source["source_chars"],
                "source_shrinkability_score": source["source_shrinkability_score"],
                "source_value_hash": source["value_hash"],
                "source_preview": source["source_preview"],
            })
            events.append(ExportEvent("usage_request_cache_source", "usage_request_cache_source", row_id, props))
    return events, skipped


def build_tool_events(rows: list[dict[str, str]], token: str, distinct_id: str, epoch: int, report_date: str) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    for row in rows:
        section = row.get("section", "")
        bucket = row.get("bucket", "")
        dimension = row.get("dimension", "")
        name = row.get("name", "")
        canonical_key = f"{section}:{bucket}:{dimension}:{name}"
        row_id = insert_id(report_date, "usage_tool_breakdown", canonical_key)
        props = with_common(token, distinct_id, epoch, row_id, report_date, {
            "section": section,
            "bucket": bucket,
            "dimension": dimension,
            "name": name,
            "canonical_key": canonical_key,
            "calls": to_int(row.get("calls")),
            "calls_share_pct": to_float(row.get("calls_share_pct")),
            "sessions_with_tool": to_int(row.get("sessions_with_tool")),
            "avg_calls_per_using_session": to_float(row.get("avg_calls_per_using_session")),
            "projected_cost_usd": to_float(row.get("projected_cost_usd")),
            "projected_cost_share_pct": to_float(row.get("projected_cost_share_pct")),
        })
        events.append(ExportEvent("usage_tool_breakdown", "usage_tool_breakdown", row_id, props))
    return events


def build_tool_attribution_events(rows: list[dict[str, str]], token: str, distinct_id: str, epoch: int, report_date: str) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    for row in rows:
        session_id = session_identity(row.get("file", ""))
        prompt_index = to_int(row.get("prompt_index"))
        dimension = row.get("dimension", "")
        name = row.get("name", "")
        canonical_key = f"{row.get('model','')}:{row.get('bucket','')}:{session_id}:{prompt_index}:{dimension}:{name}"
        event_date = row_report_date(row, report_date)
        if is_after_report_date(event_date, report_date):
            continue
        row_id = insert_id(event_date, "usage_tool_attribution", canonical_key)
        props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
            "model": row.get("model", ""),
            "provider": row.get("provider", ""),
            "billable_model": row.get("billable_model", ""),
            "billable_model_source": row.get("billable_model_source", ""),
            "usage_source": row.get("usage_source", ""),
            "bucket": row.get("bucket", ""),
            "batch_report_date": report_date,
            "session_id": session_id,
            "session_file": row.get("file", ""),
            "prompt_index": prompt_index,
            "dimension": dimension,
            "name": name,
            "canonical_key": canonical_key,
            "calls": to_int(row.get("calls")),
            "prompt_input_tokens": to_float(row.get("prompt_input_tokens")),
            "prompt_cache_read_tokens": to_float(row.get("prompt_cache_read_tokens")),
            "prompt_cache_creation_tokens": to_float(row.get("prompt_cache_creation_tokens")),
            "prompt_output_tokens": to_float(row.get("prompt_output_tokens")),
            "prompt_reasoning_tokens": to_float(row.get("prompt_reasoning_tokens")),
            "prompt_total_tokens": to_float(row.get("prompt_total_tokens")),
            "session_input_tokens": to_float(row.get("session_input_tokens")),
            "session_cache_read_tokens": to_float(row.get("session_cache_read_tokens")),
            "session_cache_creation_tokens": to_float(row.get("session_cache_creation_tokens")),
            "session_output_tokens": to_float(row.get("session_output_tokens")),
            "session_reasoning_tokens": to_float(row.get("session_reasoning_tokens")),
            "session_total_tokens": to_float(row.get("session_total_tokens")),
            "prompt_derived_total_cost_usd": to_optional_float(row.get("prompt_derived_total_cost_usd")),
            "session_derived_total_cost_usd": to_optional_float(row.get("session_derived_total_cost_usd")),
            "allocated_input_tokens": to_float(row.get("allocated_input_tokens")),
            "allocated_cache_read_tokens": to_float(row.get("allocated_cache_read_tokens")),
            "allocated_cache_creation_tokens": to_float(row.get("allocated_cache_creation_tokens")),
            "allocated_output_tokens": to_float(row.get("allocated_output_tokens")),
            "allocated_reasoning_tokens": to_float(row.get("allocated_reasoning_tokens")),
            "allocated_total_tokens": to_float(row.get("allocated_total_tokens")),
            "allocated_total_cost_usd": to_optional_float(row.get("allocated_total_cost_usd")),
            "call_share_pct": to_float(row.get("call_share_pct")),
            "allocation_method": row.get("allocation_method", "prompt_window_even_split"),
            "pricing_missing": to_bool(row.get("pricing_missing")),
        })
        events.append(ExportEvent("usage_tool_attribution", "usage_tool_attribution", row_id, props))
    return events


def build_request_tool_attribution_events(
    rows: list[dict[str, str]],
    prompt_rows: list[dict[str, str]],
    token: str,
    distinct_id: str,
    report_date: str,
    task_categorizer: TaskCategorizer,
    request_pattern_categorizer: RequestPatternCategorizer,
) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    prompt_context: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for prompt in prompt_rows:
        canonical_key, event_date, prompt_index, _pattern, payload = request_base_payload(
            prompt, report_date, task_categorizer, request_pattern_categorizer
        )
        if is_after_report_date(event_date, report_date):
            continue
        prompt_context[(prompt.get("model", ""), prompt.get("bucket", ""), session_identity(prompt.get("file", "")), prompt_index)] = {
            **payload,
            "request_canonical_key": canonical_key,
        }

    for row in rows:
        session_id = session_identity(row.get("file", ""))
        prompt_index = to_int(row.get("prompt_index"))
        dimension = row.get("dimension", "")
        name = row.get("name", "")
        event_date = row_report_date(row, report_date)
        if is_after_report_date(event_date, report_date):
            continue
        key = (row.get("model", ""), row.get("bucket", ""), session_id, prompt_index)
        context = prompt_context.get(key, {})
        canonical_key = f"{row.get('model','')}:{row.get('bucket','')}:{session_id}:{prompt_index}:{dimension}:{name}"
        diagnosis_version = context.get("diagnosis_version", os.getenv("USAGE_DIAGNOSIS_VERSION", "request_pattern_layers_v1"))
        row_id = insert_id(event_date, "usage_request_tool_attribution", f"{canonical_key}:{diagnosis_version}")
        props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
            "model": row.get("model", ""),
            "provider": row.get("provider", ""),
            "billable_model": row.get("billable_model", ""),
            "billable_model_source": row.get("billable_model_source", ""),
            "usage_source": row.get("usage_source", ""),
            "bucket": row.get("bucket", ""),
            "batch_report_date": report_date,
            "session_id": session_id,
            "session_file": row.get("file", ""),
            "prompt_index": prompt_index,
            "prompt_preview": context.get("prompt_preview", normalize_preview(row.get("prompt_preview", ""))),
            "request_pattern": context.get("request_pattern", "other"),
            "request_pattern_path": context.get("request_pattern_path", "other"),
            "request_pattern_depth": context.get("request_pattern_depth", 1),
            "request_pattern_rule_id": context.get("request_pattern_rule_id", "default"),
            "request_pattern_confidence": context.get("request_pattern_confidence", "low"),
            "request_pattern_config_version": context.get("request_pattern_config_version", ""),
            "task_label": context.get("task_label", "uncategorized"),
            "task_label_source": context.get("task_label_source", ""),
            "task_label_confidence": context.get("task_label_confidence", "low"),
            "task_type": context.get("task_type", "uncategorized"),
            "task_type_label": context.get("task_type_label", "Uncategorized"),
            "task_type_confidence": context.get("task_type_confidence", "low"),
            "task_type_classifier": context.get("task_type_classifier", "default"),
            "task_type_reason": context.get("task_type_reason", "no_rule_matched"),
            "task_type_source": context.get("task_type_source", ""),
            "task_type_config_version": context.get("task_type_config_version", ""),
            "diagnosis_version": diagnosis_version,
            "dimension": dimension,
            "name": name,
            "canonical_key": canonical_key,
            "request_canonical_key": context.get("request_canonical_key", ""),
            "calls": to_int(row.get("calls")),
            "prompt_input_tokens": to_float(row.get("prompt_input_tokens")),
            "prompt_cache_read_tokens": to_float(row.get("prompt_cache_read_tokens")),
            "prompt_cache_creation_tokens": to_float(row.get("prompt_cache_creation_tokens")),
            "prompt_output_tokens": to_float(row.get("prompt_output_tokens")),
            "prompt_reasoning_tokens": to_float(row.get("prompt_reasoning_tokens")),
            "prompt_total_tokens": to_float(row.get("prompt_total_tokens")),
            "prompt_derived_total_cost_usd": to_optional_float(row.get("prompt_derived_total_cost_usd")),
            "allocated_input_tokens": to_float(row.get("allocated_input_tokens")),
            "allocated_cache_read_tokens": to_float(row.get("allocated_cache_read_tokens")),
            "allocated_cache_creation_tokens": to_float(row.get("allocated_cache_creation_tokens")),
            "allocated_output_tokens": to_float(row.get("allocated_output_tokens")),
            "allocated_reasoning_tokens": to_float(row.get("allocated_reasoning_tokens")),
            "allocated_total_tokens": to_float(row.get("allocated_total_tokens")),
            "allocated_total_cost_usd": to_optional_float(row.get("allocated_total_cost_usd")),
            "allocation_method": row.get("allocation_method", "prompt_window_even_split"),
        })
        events.append(ExportEvent("usage_request_tool_attribution", "usage_request_tool_attribution", row_id, props))
    return events


def build_command_attribution_events(
    rows: list[dict[str, str]],
    prompt_rows: list[dict[str, str]],
    token: str,
    distinct_id: str,
    report_date: str,
    task_categorizer: TaskCategorizer,
    request_pattern_categorizer: RequestPatternCategorizer,
) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    prompt_context: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for prompt in prompt_rows:
        canonical_key, event_date, prompt_index, _pattern, payload = request_base_payload(
            prompt, report_date, task_categorizer, request_pattern_categorizer
        )
        if is_after_report_date(event_date, report_date):
            continue
        prompt_context[(prompt.get("model", ""), prompt.get("bucket", ""), session_identity(prompt.get("file", "")), prompt_index)] = {
            **payload,
            "request_canonical_key": canonical_key,
        }

    for row in rows:
        session_id = session_identity(row.get("file", ""))
        prompt_index = to_int(row.get("prompt_index"))
        command_index = to_int(row.get("command_index"))
        event_date = row_report_date(row, report_date)
        if is_after_report_date(event_date, report_date):
            continue
        key = (row.get("model", ""), row.get("bucket", ""), session_id, prompt_index)
        context = prompt_context.get(key, {})
        schema_version = row.get("schema_version") or "usage_command_attribution_v4"
        service_classifier_revision = row.get("service_classifier_revision", "")
        classification_revision = row.get("classification_revision", "")
        canonical_key = f"{row.get('model','')}:{row.get('bucket','')}:{session_id}:{prompt_index}:{command_index}:{row.get('command_hash','')}"
        revision_key = service_classifier_revision or classification_revision
        row_id_key = f"{schema_version}:{revision_key}:{canonical_key}" if revision_key else f"{schema_version}:{canonical_key}"
        row_id = insert_id_v4(event_date, "usage_command_attribution", row_id_key)
        props = with_common(token, distinct_id, report_epoch(event_date), row_id, event_date, {
            "schema_version": schema_version,
            "service_classifier_revision": service_classifier_revision,
            "model": row.get("model", ""),
            "provider": row.get("provider", ""),
            "billable_model": row.get("billable_model", ""),
            "billable_model_source": row.get("billable_model_source", ""),
            "usage_source": row.get("usage_source", ""),
            "bucket": row.get("bucket", ""),
            "batch_report_date": report_date,
            "session_id": session_id,
            "session_file": row.get("file", ""),
            "prompt_index": prompt_index,
            "command_index": command_index,
            "request_canonical_key": context.get("request_canonical_key", ""),
            "canonical_key": canonical_key,
            "prompt_preview": context.get("prompt_preview", normalize_preview(row.get("prompt_preview", ""))),
            "request_pattern": context.get("request_pattern", "other"),
            "request_pattern_path": context.get("request_pattern_path", "other"),
            "request_pattern_depth": context.get("request_pattern_depth", 1),
            "request_pattern_rule_id": context.get("request_pattern_rule_id", "default"),
            "request_pattern_confidence": context.get("request_pattern_confidence", "low"),
            "request_pattern_config_version": context.get("request_pattern_config_version", ""),
            "task_label": context.get("task_label", "uncategorized"),
            "task_label_source": context.get("task_label_source", ""),
            "task_label_confidence": context.get("task_label_confidence", "low"),
            "task_type": context.get("task_type", "uncategorized"),
            "task_type_label": context.get("task_type_label", "Uncategorized"),
            "task_type_confidence": context.get("task_type_confidence", "low"),
            "task_type_classifier": context.get("task_type_classifier", "default"),
            "task_type_reason": context.get("task_type_reason", "no_rule_matched"),
            "task_type_source": context.get("task_type_source", ""),
            "task_type_config_version": context.get("task_type_config_version", ""),
            "function_name": row.get("function_name", ""),
            "shell_verb": row.get("shell_verb", ""),
            "command_preview": row.get("command_preview", ""),
            "command_hash": row.get("command_hash", ""),
            "workdir": row.get("workdir", ""),
            "target_type": row.get("target_type", ""),
            "target": row.get("target", ""),
            "output_chars": to_int(row.get("output_chars")),
            "output_token_estimate": to_float(row.get("output_token_estimate")),
            "primary_why": row.get("primary_why", "uncategorized"),
            "why_tags": row.get("why_tags", row.get("primary_why", "uncategorized")),
            "why_classifier": row.get("why_classifier", "rules_v1"),
            "classification_revision": row.get("classification_revision", ""),
            "classification_cluster_key": row.get("classification_cluster_key", ""),
            "prompt_task_kind": row.get("prompt_task_kind", ""),
            "agent_tool_intention": row.get("agent_tool_intention", ""),
            "agent_tool_intention_source": row.get("agent_tool_intention_source", ""),
            "tool_execution_mode": row.get("tool_execution_mode", ""),
            "tool_execution_mode_source": row.get("tool_execution_mode_source", ""),
            "delegated_agent_action": row.get("delegated_agent_action", ""),
            "delegated_agent_id": row.get("delegated_agent_id", ""),
            "delegated_agent_type": row.get("delegated_agent_type", ""),
            "delegated_agent_nickname": row.get("delegated_agent_nickname", ""),
            "delegated_task_preview": row.get("delegated_task_preview", ""),
            "delegated_task_hash": row.get("delegated_task_hash", ""),
            "primary_why_confidence": row.get("primary_why_confidence", ""),
            "prompt_task_kind_confidence": row.get("prompt_task_kind_confidence", ""),
            "agent_tool_intention_confidence": row.get("agent_tool_intention_confidence", ""),
            "classification_agreement": row.get("classification_agreement", ""),
            "review_reason": row.get("review_reason", ""),
            "tool_action": row.get("tool_action", ""),
            "tool_action_source": row.get("tool_action_source", ""),
            "service_of_why": row.get("service_of_why", ""),
            "service_of_confidence": row.get("service_of_confidence", ""),
            "service_of_source": row.get("service_of_source", ""),
            "uncategorized_reason": row.get("uncategorized_reason", ""),
            "session_root_cause_summary": row.get("session_root_cause_summary", ""),
            "prompt_input_tokens": to_float(row.get("prompt_input_tokens")),
            "prompt_cache_read_tokens": to_float(row.get("prompt_cache_read_tokens")),
            "prompt_cache_creation_tokens": to_float(row.get("prompt_cache_creation_tokens")),
            "prompt_output_tokens": to_float(row.get("prompt_output_tokens")),
            "prompt_reasoning_tokens": to_float(row.get("prompt_reasoning_tokens")),
            "prompt_total_tokens": to_float(row.get("prompt_total_tokens")),
            "prompt_derived_total_cost_usd": to_optional_float(row.get("prompt_derived_total_cost_usd")),
            "allocated_input_tokens": to_float(row.get("allocated_input_tokens")),
            "allocated_cache_read_tokens": to_float(row.get("allocated_cache_read_tokens")),
            "allocated_cache_creation_tokens": to_float(row.get("allocated_cache_creation_tokens")),
            "allocated_output_tokens": to_float(row.get("allocated_output_tokens")),
            "allocated_reasoning_tokens": to_float(row.get("allocated_reasoning_tokens")),
            "allocated_total_tokens": to_float(row.get("allocated_total_tokens")),
            "allocated_total_cost_usd": to_optional_float(row.get("allocated_total_cost_usd")),
            "allocation_weight": to_float(row.get("allocation_weight")),
            "cost_is_estimated": to_bool(row.get("cost_is_estimated", True)),
            "cost_allocation_method": row.get("cost_allocation_method", "prompt_cost_output_weighted_v1"),
        })
        if schema_version in {"usage_command_attribution_v4_2", "usage_command_attribution_v4_3", "usage_command_attribution_v4_4"}:
            for legacy_key in (
                "why_tags",
                "why_classifier",
                "tool_action",
                "tool_action_source",
                "service_of_why",
                "service_of_confidence",
                "service_of_source",
                "uncategorized_reason",
                "session_root_cause_summary",
                "prompt_primary_why",
                "row_primary_why",
            ):
                props.pop(legacy_key, None)
        events.append(ExportEvent("usage_command_attribution", "usage_command_attribution", row_id, props))
    return events


def build_cache_driver_events(report: dict[str, Any], audit: dict[str, Any], token: str, distinct_id: str, epoch: int, report_date: str) -> list[ExportEvent]:
    events: list[ExportEvent] = []
    drivers = report.get("cache_hit_drivers", {}) or {}
    for section_name, detail in drivers.items():
        for row in detail.get("source_shares", []) or []:
            source = str(row.get("source", ""))
            canonical_key = f"{section_name}:source_share:{source}"
            row_id = insert_id(report_date, "usage_cache_driver", canonical_key)
            props = with_common(token, distinct_id, epoch, row_id, report_date, {
                "section": section_name,
                "driver_kind": "source_share",
                "source": source,
                "canonical_key": canonical_key,
                "estimated_repeated_tokens": to_float(row.get("estimated_repeated_tokens")),
                "share_pct": to_float(row.get("share_pct")),
            })
            events.append(ExportEvent("usage_cache_driver", "usage_cache_driver", row_id, props))

    for provider, key in (("codex", "topCodexRepeatedValues"), ("claude", "topClaudeRepeatedValues")):
        for row in ((audit.get("repeatBreakdown", {}) or {}).get(key, []) or []):
            raw_value = str(row.get("value", ""))
            source = str(row.get("source", ""))
            value_hash = digest_text(raw_value)
            canonical_key = f"{provider}:repeated_value:{source}:{value_hash}"
            row_id = insert_id(report_date, "usage_cache_driver", canonical_key)
            props = with_common(token, distinct_id, epoch, row_id, report_date, {
                "section": provider,
                "driver_kind": "repeated_value",
                "source": source,
                "canonical_key": canonical_key,
                "occurrence_count": to_int(row.get("count")),
                "chars": to_int(row.get("chars")),
                "token_estimate_per_value": to_float(row.get("tokenEstimatePerValue")),
                "token_estimate_total": to_float(row.get("tokenEstimateTotal")),
                "shrinkability_score": to_float(row.get("shrinkabilityScore")),
                "value_hash": value_hash,
                "value_preview": normalize_preview(raw_value, 120),
            })
            events.append(ExportEvent("usage_cache_driver", "usage_cache_driver", row_id, props))
    return events


def limit_family(events: list[ExportEvent], cap: int) -> tuple[list[ExportEvent], int]:
    if cap > 0 and len(events) > cap:
        return events[:cap], len(events) - cap
    return events, 0


def auth_header() -> str:
    user = os.getenv("MIXPANEL_SERVICE_ACCOUNT_USER", "")
    password = os.getenv("MIXPANEL_SERVICE_ACCOUNT_PASS", "")
    api_secret = os.getenv("MIXPANEL_API_SECRET", "")
    if user and password:
        raw = f"{user}:{password}".encode("utf-8")
    elif api_secret:
        raw = f"{api_secret}:".encode("utf-8")
    else:
        raise RuntimeError(
            "Missing Mixpanel auth. Set MIXPANEL_API_SECRET or MIXPANEL_SERVICE_ACCOUNT_USER + MIXPANEL_SERVICE_ACCOUNT_PASS"
        )
    return "Basic " + base64.b64encode(raw).decode("ascii")


def import_url(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query["strict"] = "1"
    project_id = os.getenv("MIXPANEL_PROJECT_ID", "")
    if project_id:
        query["project_id"] = project_id
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def send_import_batch(endpoint: str, headers: dict[str, str], batch: list[dict[str, Any]]) -> None:
    body = json.dumps(batch).encode("utf-8")
    request = urllib.request.Request(import_url(endpoint), data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        payload = response.read().decode("utf-8", errors="replace").strip()
        if response.status >= 400:
            raise RuntimeError(f"Mixpanel import failed status={response.status} body={payload}")


def emit_batches(
    events: list[ExportEvent],
    endpoint: str,
    batch_size: int,
    dry_run: bool,
) -> int:
    if dry_run:
        return len(events)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": auth_header(),
    }
    sent = 0
    rows = [{"event": event.event, "properties": event.properties} for event in events]
    for idx in range(0, len(rows), batch_size):
        batch = rows[idx : idx + batch_size]
        try:
            send_import_batch(endpoint, headers, batch)
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Mixpanel import failed status={exc.code} body={payload}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Network error while sending Mixpanel batch: {exc}") from exc
        sent += len(batch)
    return sent


def build_all_events(
    input_root: Path,
    report_date: str,
    token: str,
    distinct_id: str,
    max_events_per_family: int,
    max_unique_prompt_hashes: int,
    max_cache_sources_per_request: int,
    task_categorizer: TaskCategorizer,
    request_pattern_categorizer: RequestPatternCategorizer,
) -> tuple[dict[str, list[ExportEvent]], dict[str, int]]:
    audit = read_json(input_root / "cache-hit-audit-report.json")
    report = read_json(input_root / "reports/planning-vs-execution-report.json")
    sessions = read_csv(input_root / "reports/planning-vs-execution-sessions.csv")
    prompts = read_csv(input_root / "reports/planning-vs-execution-prompts.csv")
    tools = read_csv(input_root / "reports/planning-vs-execution-tool-breakdown.csv")
    attribution_path = input_root / "reports/planning-vs-execution-tool-attribution.csv"
    tool_attribution = read_csv(attribution_path) if attribution_path.exists() else []
    command_attribution_path = input_root / "reports/usage-command-attribution-v4.csv"
    command_attribution = read_csv(command_attribution_path) if command_attribution_path.exists() else []
    command_attribution_v4_1_path = input_root / "reports/usage-command-attribution-v4_1.csv"
    if command_attribution_v4_1_path.exists():
        command_attribution.extend(read_csv(command_attribution_v4_1_path))
    command_attribution_v4_2_path = input_root / "reports/usage-command-attribution-v4_2.csv"
    if command_attribution_v4_2_path.exists():
        command_attribution.extend(read_csv(command_attribution_v4_2_path))
    command_attribution_v4_3_path = input_root / "reports/usage-command-attribution-v4_3.csv"
    if command_attribution_v4_3_path.exists():
        command_attribution.extend(read_csv(command_attribution_v4_3_path))
    command_attribution_v4_4_path = input_root / "reports/usage-command-attribution-v4_4.csv"
    if command_attribution_v4_4_path.exists():
        command_attribution.extend(read_csv(command_attribution_v4_4_path))

    epoch = report_epoch(report_date)
    families: dict[str, list[ExportEvent]] = {}
    capped: dict[str, int] = {}

    families["usage_daily_rollup"] = build_daily_rollups(report, audit, token, distinct_id, epoch, report_date)
    families["usage_session"] = build_session_events(sessions, token, distinct_id, epoch, report_date)
    prompt_events, prompt_skipped = build_prompt_events(
        prompts, token, distinct_id, epoch, report_date, max_unique_prompt_hashes
    )
    families["usage_prompt"] = prompt_events
    diagnosis_events, diagnosis_skipped = build_request_cache_diagnosis_events(
        prompts, audit, token, distinct_id, report_date, max_unique_prompt_hashes, task_categorizer, request_pattern_categorizer
    )
    families["usage_request_cache_diagnosis"] = diagnosis_events
    source_events, source_skipped = build_request_cache_source_events(
        prompts,
        audit,
        token,
        distinct_id,
        report_date,
        max_unique_prompt_hashes,
        max_cache_sources_per_request,
        task_categorizer,
        request_pattern_categorizer,
    )
    families["usage_request_cache_source"] = source_events
    families["usage_tool_breakdown"] = build_tool_events(tools, token, distinct_id, epoch, report_date)
    families["usage_tool_attribution"] = build_tool_attribution_events(tool_attribution, token, distinct_id, epoch, report_date)
    families["usage_request_tool_attribution"] = build_request_tool_attribution_events(
        tool_attribution, prompts, token, distinct_id, report_date, task_categorizer, request_pattern_categorizer
    )
    families["usage_command_attribution"] = build_command_attribution_events(
        command_attribution, prompts, token, distinct_id, report_date, task_categorizer, request_pattern_categorizer
    )
    families["usage_cache_driver"] = build_cache_driver_events(report, audit, token, distinct_id, epoch, report_date)
    if prompt_skipped:
        capped["usage_prompt_prompt_hash_cap"] = prompt_skipped
    if diagnosis_skipped:
        capped["usage_request_cache_diagnosis_prompt_hash_cap"] = diagnosis_skipped
    if source_skipped:
        capped["usage_request_cache_source_prompt_hash_cap"] = source_skipped

    for name, rows in list(families.items()):
        if name == "usage_command_attribution":
            continue
        limited, dropped = limit_family(rows, max_events_per_family)
        families[name] = limited
        if dropped:
            capped[f"{name}_family_cap"] = dropped
    return families, capped


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export usage artifacts to Mixpanel.")
    parser.add_argument("--input-root", default=".", help="Repository root containing cache-hit report and reports/ outputs.")
    parser.add_argument("--date", default=default_report_date(), help="Report date (YYYY-MM-DD).")
    parser.add_argument("--state-file", default=os.path.expanduser("~/.session-metrics-cron/usage-metrics/send_state.json"))
    parser.add_argument("--ignore-local-state", action="store_true", help="Do not suppress events using local state file; rely on deterministic $insert_id for Mixpanel dedupe.")
    parser.add_argument("--summary-path", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-events-per-family", type=int, default=to_int(os.getenv("MAX_EVENTS_PER_FAMILY"), 100000))
    parser.add_argument(
        "--max-unique-prompt-hashes",
        type=int,
        default=to_int(os.getenv("MAX_UNIQUE_PROMPT_HASHES_PER_DAY"), 50000),
    )
    parser.add_argument(
        "--max-cache-sources-per-request",
        type=int,
        default=to_int(os.getenv("MAX_CACHE_SOURCES_PER_REQUEST"), 3),
    )
    parser.add_argument("--max-state-ids", type=int, default=to_int(os.getenv("MAX_STATE_IDS"), 500000))
    parser.add_argument("--batch-size", type=int, default=to_int(os.getenv("MIXPANEL_BATCH_SIZE"), 2000))
    parser.add_argument("--endpoint", default=os.getenv("MIXPANEL_ENDPOINT", "https://api.mixpanel.com/import"))
    parser.add_argument(
        "--task-categorization-config",
        default=os.getenv("USAGE_TASK_CATEGORIZATION_CONFIG", ""),
        help="YAML/JSON task categorization config. Missing path falls back to built-in regex defaults.",
    )
    parser.add_argument(
        "--request-pattern-config",
        default=os.getenv("USAGE_REQUEST_PATTERN_CONFIG", ""),
        help="YAML/JSON recursive request pattern config. Missing path falls back to built-in regex defaults.",
    )
    parser.add_argument(
        "--task-classification-cache",
        default=os.getenv(
            "USAGE_TASK_CLASSIFICATION_CACHE",
            os.path.expanduser("~/.session-metrics-cron/usage-metrics/task-classification-cache.json"),
        ),
        help="Path for Codex task classification cache.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = os.getenv("MIXPANEL_TOKEN", "")
    if not token:
        print("Missing required env: MIXPANEL_TOKEN", file=sys.stderr)
        return 1

    distinct_id = os.getenv("MIXPANEL_DISTINCT_ID", "session-metrics-cron")
    input_root = Path(args.input_root).resolve()
    try:
        task_config = load_task_categorization_config(args.task_categorization_config)
        task_cache = TaskClassificationCache(Path(args.task_classification_cache).expanduser())
        task_categorizer = TaskCategorizer(task_config, task_cache)
        request_pattern_config = load_request_pattern_config(args.request_pattern_config)
        request_pattern_categorizer = RequestPatternCategorizer(request_pattern_config)
    except RuntimeError as exc:
        print(f"Invalid categorization config: {exc}", file=sys.stderr)
        return 1
    for required in (
        input_root / "cache-hit-audit-report.json",
        input_root / "reports/planning-vs-execution-report.json",
        input_root / "reports/planning-vs-execution-sessions.csv",
        input_root / "reports/planning-vs-execution-prompts.csv",
        input_root / "reports/planning-vs-execution-tool-breakdown.csv",
    ):
        if not required.exists():
            print(f"Missing required input: {required}", file=sys.stderr)
            return 1

    families, capped = build_all_events(
        input_root=input_root,
        report_date=args.date,
        token=token,
        distinct_id=distinct_id,
        max_events_per_family=args.max_events_per_family,
        max_unique_prompt_hashes=args.max_unique_prompt_hashes,
        max_cache_sources_per_request=args.max_cache_sources_per_request,
        task_categorizer=task_categorizer,
        request_pattern_categorizer=request_pattern_categorizer,
    )
    task_cache.save()

    state = StateStore(Path(args.state_file).expanduser(), args.max_state_ids)
    to_send: dict[str, list[ExportEvent]] = {}
    duplicate_counts: dict[str, int] = {}
    for family, events in families.items():
        fresh: list[ExportEvent] = []
        dupes = 0
        if args.ignore_local_state:
            fresh = events
        else:
            for event in events:
                if state.has(event.insert_id):
                    dupes += 1
                    continue
                fresh.append(event)
        to_send[family] = fresh
        duplicate_counts[family] = dupes

    ordered = []
    for family in (
        "usage_daily_rollup",
        "usage_session",
        "usage_prompt",
        "usage_request_cache_diagnosis",
        "usage_request_cache_source",
        "usage_tool_breakdown",
        "usage_tool_attribution",
        "usage_request_tool_attribution",
        "usage_command_attribution",
        "usage_cache_driver",
    ):
        ordered.extend(to_send.get(family, []))

    try:
        sent_count = emit_batches(ordered, args.endpoint, args.batch_size, args.dry_run)
    except RuntimeError as exc:
        print(f"Failed to export Mixpanel events: {exc}", file=sys.stderr)
        return 1

    if not args.dry_run:
        state.add_many(event.insert_id for event in ordered)
        state.save()

    summary = {
        "report_date": args.date,
        "dry_run": args.dry_run,
        "total_events_after_dedupe": sent_count,
        "families": {family: len(rows) for family, rows in to_send.items()},
        "sample_event_properties": {
            family: sorted(rows[0].properties) if rows else []
            for family, rows in to_send.items()
        },
        "duplicates_suppressed": duplicate_counts,
        "capped": capped,
        "ignore_local_state": args.ignore_local_state,
        "state_file": str(Path(args.state_file).expanduser()),
        "task_type_config_version": task_categorizer.version,
        "request_pattern_config_version": request_pattern_categorizer.version,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.summary_path:
        Path(args.summary_path).write_text(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
