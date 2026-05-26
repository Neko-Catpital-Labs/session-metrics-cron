#!/usr/bin/env python3
"""
Audit cache-hit behavior across local + SSH machines.

Features:
- Runs ccusage (generic/codex/claude) across all hosts.
- Stages codex/claude logs from all hosts.
- Computes cross-host log-overlap stats.
- Deduplicates logs by content hash, reruns ccusage on deduped data.
- Extracts repeated input blocks and exact repeated values.
- Writes a full JSON report.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES_CONFIG = REPO_ROOT / "config" / "sources.json"
LEGACY_INV_CONFIG = Path.home() / ".invoker" / "config.json"
DEFAULT_SSH_KEY = Path.home() / ".ssh" / "id_ed25519"


@dataclass(frozen=True)
class Host:
    name: str
    host: str | None
    user: str = "invoker"
    port: int = 22
    ssh_key: str = str(DEFAULT_SSH_KEY)
    codex_sessions_dir: str = str(Path.home() / ".codex" / "sessions")
    claude_root_dir: str = str(Path.home() / ".claude")
    codex_home: str | None = None
    home_dir: str | None = None


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if not allow_failure and cp.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{cp.stdout[:2000]}")
    return cp


def parse_first_json(text: str) -> Any:
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch not in "{[":
            continue
        try:
            obj, _ = dec.raw_decode(text[i:])
            return obj
        except Exception:
            continue
    raise ValueError("No JSON found in command output")


def to_int(v: Any) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def to_float(v: Any) -> float:
    try:
        return float(v or 0.0)
    except Exception:
        return 0.0


def token_estimate(chars: int) -> int:
    return max(1, round(chars / 4))


def expand_local_path(path: str, default: Path) -> str:
    value = (path or "").strip()
    if not value:
        return str(default)
    return str(Path(value).expanduser())


def parse_host_entry(name: str, raw: dict[str, Any], *, local_default: bool) -> Host | None:
    if raw.get("enabled") is False:
        return None
    host_value = raw.get("host")
    host = host_value.strip() if isinstance(host_value, str) and host_value.strip() else None
    if not local_default and host is None:
        return None

    user = raw.get("user") if isinstance(raw.get("user"), str) else "invoker"
    port = int(raw.get("port") or 22)
    ssh_key = raw.get("sshKeyPath") if isinstance(raw.get("sshKeyPath"), str) else str(DEFAULT_SSH_KEY)
    codex_sessions = expand_local_path(
        str(raw.get("codexSessionsDir") or raw.get("codex_sessions_dir") or ""),
        Path.home() / ".codex" / "sessions",
    )
    claude_root = expand_local_path(
        str(raw.get("claudeRootDir") or raw.get("claude_root_dir") or ""),
        Path.home() / ".claude",
    )
    codex_home = raw.get("codexHome") if isinstance(raw.get("codexHome"), str) else str(Path(codex_sessions).parent)
    home_dir = raw.get("homeDir") if isinstance(raw.get("homeDir"), str) else str(Path(claude_root).parent)
    return Host(
        name=name,
        host=host,
        user=user,
        port=port,
        ssh_key=ssh_key,
        codex_sessions_dir=codex_sessions,
        claude_root_dir=claude_root,
        codex_home=codex_home,
        home_dir=home_dir,
    )


def load_sources_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text()
    if path.suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                f"Found {path} but PyYAML is not installed. Install with `python3 -m pip install pyyaml`."
            ) from exc
        obj = yaml.safe_load(text)
        return obj if isinstance(obj, dict) else {}
    obj = json.loads(text)
    return obj if isinstance(obj, dict) else {}


def load_hosts(sources_path: Path) -> list[Host]:
    hosts: list[Host] = []
    cfg = load_sources_config(sources_path)
    if cfg:
        local_raw = cfg.get("local")
        if isinstance(local_raw, dict):
            local_name = str(local_raw.get("name") or "local_this_machine")
            local_host = parse_host_entry(local_name, local_raw, local_default=True)
            if local_host:
                local_host = Host(**{**local_host.__dict__, "host": None})
                hosts.append(local_host)
        else:
            hosts.append(Host("local_this_machine", None))

        remote_targets = cfg.get("remoteTargets") or cfg.get("remote_targets") or []
        if isinstance(remote_targets, dict):
            iterable = remote_targets.items()
        else:
            iterable = []
            if isinstance(remote_targets, list):
                for item in remote_targets:
                    if isinstance(item, dict):
                        name = str(item.get("name") or item.get("id") or f"remote_{len(iterable)+1}")
                        iterable.append((name, item))
        for name, raw in iterable:
            if not isinstance(raw, dict):
                continue
            host = parse_host_entry(str(name), raw, local_default=False)
            if host:
                hosts.append(host)
        return hosts or [Host("local_this_machine", None)]

    # Legacy fallback for existing Invoker installations.
    hosts = [Host("local_this_machine", None)]
    if LEGACY_INV_CONFIG.exists():
        legacy = json.loads(LEGACY_INV_CONFIG.read_text())
        remotes = legacy.get("remoteTargets") or {}
        for name, target in remotes.items():
            if not isinstance(target, dict):
                continue
            host = parse_host_entry(str(name), target, local_default=False)
            if host:
                hosts.append(host)
    return hosts


def ssh_cmd(h: Host, inner: str) -> list[str]:
    return [
        "ssh",
        "-i",
        h.ssh_key,
        "-p",
        str(h.port),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=20",
        f"{h.user}@{h.host}",
        inner,
    ]


def run_ccusage_generic(host: Host) -> dict[str, Any]:
    cmd = ["npx", "ccusage@latest", "daily", "--json"]
    cp = run_cmd(cmd, cwd=REPO_ROOT) if host.host is None else run_cmd(ssh_cmd(host, " ".join(cmd)))
    obj = parse_first_json(cp.stdout)
    totals = obj.get("totals", {}) if isinstance(obj, dict) else {}
    row = {
        "inputTokens": to_int(totals.get("inputTokens")),
        "outputTokens": to_int(totals.get("outputTokens")),
        "cacheCreationTokens": to_int(totals.get("cacheCreationTokens")),
        "cacheReadTokens": to_int(totals.get("cacheReadTokens")),
        "totalTokens": to_int(totals.get("totalTokens")),
        "totalCost": to_float(totals.get("totalCost")),
    }
    denom = row["inputTokens"] + row["cacheReadTokens"]
    row["cacheHitPct"] = round((row["cacheReadTokens"] / denom * 100) if denom else 0.0, 6)
    return row


def run_ccusage_codex(host: Host, codex_home: Path | None = None) -> dict[str, Any]:
    cmd = ["npx", "ccusage@latest", "codex", "daily", "--json"]
    if host.host is None:
        env = dict(os.environ)
        if codex_home:
            env["CODEX_HOME"] = str(codex_home)
        cp = run_cmd(cmd, cwd=REPO_ROOT, env=env)
    else:
        prefix = f"CODEX_HOME={shlex.quote(str(host.codex_home or Path(host.codex_sessions_dir).parent))} "
        cp = run_cmd(ssh_cmd(host, prefix + shlex.join(cmd)))
    obj = parse_first_json(cp.stdout)
    totals = obj.get("totals", {}) if isinstance(obj, dict) else {}
    row = {
        "inputTokens": to_int(totals.get("inputTokens")),
        "cachedInputTokens": to_int(totals.get("cachedInputTokens")),
        "outputTokens": to_int(totals.get("outputTokens")),
        "reasoningOutputTokens": to_int(totals.get("reasoningOutputTokens")),
        "totalTokens": to_int(totals.get("totalTokens")),
        "costUSD": to_float(totals.get("costUSD")),
    }
    denom = row["inputTokens"] + row["cachedInputTokens"]
    row["cacheHitPct"] = round((row["cachedInputTokens"] / denom * 100) if denom else 0.0, 6)
    return row


def run_ccusage_claude(host: Host, home_override: Path | None = None) -> dict[str, Any]:
    cmd = ["npx", "ccusage@latest", "claude", "daily", "--json"]
    if host.host is None:
        env = dict(os.environ)
        if home_override:
            env["HOME"] = str(home_override)
        cp = run_cmd(cmd, cwd=REPO_ROOT, env=env)
    else:
        prefix = f"HOME={shlex.quote(str(host.home_dir or Path(host.claude_root_dir).parent))} "
        cp = run_cmd(ssh_cmd(host, prefix + shlex.join(cmd)))
    obj = parse_first_json(cp.stdout)
    totals = obj.get("totals", {}) if isinstance(obj, dict) else {}
    row = {
        "inputTokens": to_int(totals.get("inputTokens")),
        "outputTokens": to_int(totals.get("outputTokens")),
        "cacheCreationTokens": to_int(totals.get("cacheCreationTokens")),
        "cacheReadTokens": to_int(totals.get("cacheReadTokens")),
        "totalTokens": to_int(totals.get("totalTokens")),
        "totalCost": to_float(totals.get("totalCost")),
    }
    denom = row["inputTokens"] + row["cacheReadTokens"]
    row["cacheHitPct"] = round((row["cacheReadTokens"] / denom * 100) if denom else 0.0, 6)
    return row


def sum_rows(rows: list[dict[str, Any]], keys: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in keys:
        if "Cost" in k or "cost" in k:
            out[k] = float(sum(to_float(r.get(k)) for r in rows))
        else:
            out[k] = int(sum(to_int(r.get(k)) for r in rows))
    return out


def rsync_pull(src: str, dst: Path, jsonl_only: bool = False) -> None:
    cmd = ["rsync", "-a"]
    if jsonl_only:
        cmd.extend(["--prune-empty-dirs", "--include=*/", "--include=*.jsonl", "--exclude=*"])
    cmd.extend([src, str(dst) + "/"])
    run_cmd(cmd)


def stage_logs(hosts: list[Host], stage_root: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    codex_roots: dict[str, Path] = {}
    claude_roots: dict[str, Path] = {}
    for h in hosts:
        codex_dst = stage_root / h.name / "codex_sessions"
        claude_dst = stage_root / h.name / "claude_root"
        codex_dst.mkdir(parents=True, exist_ok=True)
        claude_dst.mkdir(parents=True, exist_ok=True)
        if h.host is None:
            local_codex = Path(h.codex_sessions_dir).expanduser()
            local_claude = Path(h.claude_root_dir).expanduser()
            if local_codex.exists():
                rsync_pull(str(local_codex) + "/", codex_dst)
            if local_claude.exists():
                rsync_pull(str(local_claude) + "/", claude_dst, jsonl_only=True)
        else:
            ssh_transport = (
                "ssh -i "
                + h.ssh_key
                + " -p "
                + str(h.port)
                + " -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
            )
            run_cmd(
                [
                    "rsync",
                    "-a",
                    "-e",
                    ssh_transport,
                    f"{h.user}@{h.host}:{h.codex_sessions_dir.rstrip('/')}/",
                    str(codex_dst) + "/",
                ]
            )
            run_cmd(
                [
                    "rsync",
                    "-a",
                    "--prune-empty-dirs",
                    "--include=*/",
                    "--include=*.jsonl",
                    "--exclude=*",
                    "-e",
                    ssh_transport,
                    f"{h.user}@{h.host}:{h.claude_root_dir.rstrip('/')}/",
                    str(claude_dst) + "/",
                ]
            )
        codex_roots[h.name] = codex_dst
        claude_roots[h.name] = claude_dst
    return codex_roots, claude_roots


def overlap_stats(roots: dict[str, Path], pattern: str = "*.jsonl") -> dict[str, Any]:
    per_host_sets: dict[str, set[str]] = {}
    for name, root in roots.items():
        per_host_sets[name] = {str(p.relative_to(root)) for p in root.rglob(pattern) if p.is_file()}
    pairwise = []
    for a, b in itertools.combinations(per_host_sets.keys(), 2):
        inter = len(per_host_sets[a] & per_host_sets[b])
        if inter:
            union = len(per_host_sets[a] | per_host_sets[b]) or 1
            pairwise.append({"a": a, "b": b, "overlapCount": inter, "jaccard": round(inter / union, 6)})
    pairwise.sort(key=lambda x: x["overlapCount"], reverse=True)
    return {"countsByHost": {k: len(v) for k, v in per_host_sets.items()}, "pairOverlap": pairwise}


def dedupe_by_hash(host_order: list[str], roots: dict[str, Path], merged_root: Path) -> dict[str, Any]:
    merged_root.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    kept = dropped = collisions = 0
    kept_by_host: dict[str, int] = {}
    dropped_by_host: dict[str, int] = {}
    for host in host_order:
        root = roots[host]
        kept_by_host[host] = 0
        dropped_by_host[host] = 0
        for fp in root.rglob("*.jsonl"):
            rel = fp.relative_to(root)
            blob = fp.read_bytes()
            h = hashlib.sha256(blob).hexdigest()
            if h in seen:
                dropped += 1
                dropped_by_host[host] += 1
                continue
            seen.add(h)
            dst = merged_root / rel
            if dst.exists():
                collisions += 1
                dst = merged_root / rel.parent / f"{rel.stem}__{host}.jsonl"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(blob)
            kept += 1
            kept_by_host[host] += 1
    return {
        "uniqueJsonlKept": kept,
        "duplicateJsonlDropped": dropped,
        "pathCollisions": collisions,
        "keptByHost": kept_by_host,
        "droppedByHost": dropped_by_host,
    }


def top_repeated_entries(counter: Counter[str], source: str, top_n: int) -> list[dict[str, Any]]:
    rows = []
    for value, count in counter.items():
        chars = len(value)
        tok = token_estimate(chars)
        total_tok = tok * count
        shrinkability = round((chars / 20000) * 100, 2)
        rows.append(
            {
                "source": source,
                "count": count,
                "chars": chars,
                "tokenEstimatePerValue": tok,
                "tokenEstimateTotal": total_tok,
                "shrinkabilityScore": shrinkability,
                "value": value,
            }
        )
    rows.sort(key=lambda x: (x["tokenEstimateTotal"], x["count"]), reverse=True)
    return rows[:top_n]


def analyze_codex_repeats(codex_sessions_root: Path, top_n: int) -> list[dict[str, Any]]:
    base = Counter()
    developer = Counter()
    env_ctx = Counter()
    user_prefix = Counter()
    for fp in codex_sessions_root.rglob("*.jsonl"):
        for line in fp.read_text(errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = obj.get("type")
            if t == "session_meta":
                txt = ((obj.get("payload") or {}).get("base_instructions") or {}).get("text")
                if isinstance(txt, str) and txt.strip():
                    base[txt] += 1
            elif t == "response_item":
                payload = obj.get("payload") or {}
                if payload.get("type") == "message" and payload.get("role") == "developer":
                    parts = []
                    for c in payload.get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "input_text" and isinstance(c.get("text"), str):
                            parts.append(c["text"])
                    if parts:
                        developer["\n".join(parts)] += 1
                if payload.get("type") == "message" and payload.get("role") == "user":
                    for c in payload.get("content") or []:
                        txt = c.get("text") if isinstance(c, dict) else None
                        if isinstance(txt, str) and "<environment_context>" in txt:
                            env_ctx[txt] += 1
            elif t == "event_msg":
                payload = obj.get("payload") or {}
                if payload.get("type") == "user_message" and isinstance(payload.get("message"), str):
                    msg = " ".join(payload["message"].split())
                    user_prefix[msg[:180]] += 1
    rows = []
    rows += top_repeated_entries(base, "codex.base_instructions", top_n)
    rows += top_repeated_entries(developer, "codex.developer_blob", top_n)
    rows += top_repeated_entries(env_ctx, "codex.environment_context", top_n)
    rows += top_repeated_entries(user_prefix, "codex.user_message_prefix180", top_n)
    rows.sort(key=lambda x: (x["tokenEstimateTotal"], x["count"]), reverse=True)
    return rows[:top_n]


def analyze_claude_repeats(claude_root: Path, top_n: int) -> list[dict[str, Any]]:
    enqueue = Counter()
    skills = Counter()
    tools_delta = Counter()
    for fp in claude_root.rglob("*.jsonl"):
        for line in fp.read_text(errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "queue-operation" and obj.get("operation") == "enqueue" and isinstance(obj.get("content"), str):
                enqueue[" ".join(obj["content"].split())] += 1
            attachment = obj.get("attachment")
            if isinstance(attachment, dict):
                if attachment.get("type") == "skill_listing" and isinstance(attachment.get("content"), str):
                    skills[attachment["content"]] += 1
                if attachment.get("type") == "deferred_tools_delta":
                    names = attachment.get("addedNames") or []
                    if isinstance(names, list) and names:
                        tools_delta["\n".join(str(x) for x in names)] += 1
    rows = []
    rows += top_repeated_entries(enqueue, "claude.enqueue_content", top_n)
    rows += top_repeated_entries(skills, "claude.attachment.skill_listing", top_n)
    rows += top_repeated_entries(tools_delta, "claude.attachment.deferred_tools_delta", top_n)
    rows.sort(key=lambda x: (x["tokenEstimateTotal"], x["count"]), reverse=True)
    return rows[:top_n]


def main() -> None:
    parser = argparse.ArgumentParser(description="Cache-hit + repeated-input audit across local and SSH session sources.")
    parser.add_argument("--output", default=None, help="Output JSON report path (default: ./cache-hit-audit-report.json)")
    parser.add_argument("--top", type=int, default=20, help="How many repeated entries to keep in each ranked output.")
    parser.add_argument(
        "--sources-config",
        default=str(DEFAULT_SOURCES_CONFIG),
        help="Path to sources config (YAML/JSON). Falls back to ~/.invoker/config.json if missing.",
    )
    parser.add_argument("--keep-temp", action="store_true", help="Keep staging/dedupe temp directory.")
    args = parser.parse_args()

    out_path = Path(args.output) if args.output else (REPO_ROOT / "cache-hit-audit-report.json")
    sources_path = Path(args.sources_config).expanduser()
    top_n = max(1, args.top)
    hosts = load_hosts(sources_path)
    host_order = [h.name for h in hosts]

    tmpdir = Path(tempfile.mkdtemp(prefix="cache-hit-audit-"))
    stage_root = tmpdir / "stage"
    merged = tmpdir / "merged"
    merged_codex_home = merged / "codex_home"
    merged_claude_home = merged / "claude_home"
    merged_claude_dot = merged_claude_home / ".claude"

    try:
        generic_rows = {}
        codex_rows = {}
        claude_rows = {}
        for h in hosts:
            generic_rows[h.name] = run_ccusage_generic(h)
            codex_rows[h.name] = run_ccusage_codex(h)
            claude_rows[h.name] = run_ccusage_claude(h)

        generic_total = sum_rows(list(generic_rows.values()), ["inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens", "totalTokens", "totalCost"])
        codex_total = sum_rows(list(codex_rows.values()), ["inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens", "costUSD"])
        claude_total = sum_rows(list(claude_rows.values()), ["inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens", "totalTokens", "totalCost"])

        generic_denom = generic_total["inputTokens"] + generic_total["cacheReadTokens"]
        codex_denom = codex_total["inputTokens"] + codex_total["cachedInputTokens"]
        claude_denom = claude_total["inputTokens"] + claude_total["cacheReadTokens"]
        generic_total["cacheHitPct"] = round((generic_total["cacheReadTokens"] / generic_denom * 100) if generic_denom else 0.0, 6)
        codex_total["cacheHitPct"] = round((codex_total["cachedInputTokens"] / codex_denom * 100) if codex_denom else 0.0, 6)
        claude_total["cacheHitPct"] = round((claude_total["cacheReadTokens"] / claude_denom * 100) if claude_denom else 0.0, 6)

        codex_roots, claude_roots = stage_logs(hosts, stage_root)
        codex_overlap = overlap_stats(codex_roots)
        claude_overlap = overlap_stats(claude_roots)

        dedup_codex_stats = dedupe_by_hash(host_order, codex_roots, merged_codex_home / "sessions")
        dedup_claude_stats = dedupe_by_hash(host_order, claude_roots, merged_claude_dot)

        dedup_codex_ccusage = run_ccusage_codex(Host("local", None), codex_home=merged_codex_home)
        dedup_claude_ccusage = run_ccusage_claude(Host("local", None), home_override=merged_claude_home)

        codex_repeats = analyze_codex_repeats(merged_codex_home / "sessions", top_n)
        claude_repeats = analyze_claude_repeats(merged_claude_dot, top_n)

        report = {
            "hosts": [h.__dict__ for h in hosts],
            "baselineCcusage": {
                "genericDailyByHost": generic_rows,
                "genericDailyTotal": generic_total,
                "codexDailyByHost": codex_rows,
                "codexDailyTotal": codex_total,
                "claudeDailyByHost": claude_rows,
                "claudeDailyTotal": claude_total,
            },
            "logOverlap": {
                "codexSessions": codex_overlap,
                "claudeJsonl": claude_overlap,
            },
            "dedup": {
                "tempWorkspace": str(tmpdir),
                "codex": {
                    "stats": dedup_codex_stats,
                    "ccusageDaily": dedup_codex_ccusage,
                },
                "claude": {
                    "stats": dedup_claude_stats,
                    "ccusageDaily": dedup_claude_ccusage,
                },
            },
            "repeatBreakdown": {
                "topCodexRepeatedValues": codex_repeats,
                "topClaudeRepeatedValues": claude_repeats,
            },
        }

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2))

        print(f"Report written: {out_path}")
        print(f"Hosts checked: {', '.join(host_order)}")
        print(f"Baseline cache hit % | generic={generic_total['cacheHitPct']:.2f}, codex={codex_total['cacheHitPct']:.2f}, claude={claude_total['cacheHitPct']:.2f}")
        print(f"Dedup cache hit %    | codex={dedup_codex_ccusage['cacheHitPct']:.2f}, claude={dedup_claude_ccusage['cacheHitPct']:.2f}")
        print(f"Top repeated rows    | codex={len(codex_repeats)}, claude={len(claude_repeats)}")

        if args.keep_temp:
            print(f"Temp workspace kept at: {tmpdir}")
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)

    except Exception:
        if args.keep_temp:
            print(f"Error occurred. Temp workspace retained: {tmpdir}")
        raise


if __name__ == "__main__":
    main()

