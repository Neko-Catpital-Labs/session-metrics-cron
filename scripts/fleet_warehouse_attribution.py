#!/usr/bin/env python3
"""Build the FLEET-WIDE v4.5 command-attribution CSV with correct fleet costs.

The stock planning_vs_execution_report anchors per-command cost to THIS machine's
ccusage bill (`cost_per_eff = local_ccusage_total / eff_corpus`), so it is
local-scoped by design. Feeding it fleet sessions would mis-scale every dollar.

This orchestrator instead:
  - collects codex / claude / omp sessions across local + all SSH hosts (reusing
    fleet_cost_report's host discovery + staging), de-duplicated by file content
    hash so a session synced to two hosts is never double-counted, and
  - anchors each family's cost to the pricing-table total derived from the fleet
    sessions themselves (ccusage-free, fleet-correct),
then reuses the report's own row builders + v4.5 intent classifier + CSV writer,
so the output is column-compatible with reports/usage-command-attribution-v4_5.csv
and loads into the warehouse unchanged. omp commands keep their exact per-turn cost.

Usage:
  python3 scripts/fleet_warehouse_attribution.py --out-dir reports
  python3 scripts/fleet_warehouse_attribution.py --no-collect   # reuse existing stage
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import planning_vs_execution_report as pve  # noqa: E402
import fleet_cost_report as fleet  # noqa: E402
from usage_costing import derive_cost, load_pricing_table  # noqa: E402


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        if not rows:
            handle.write("empty\n")
            return
        fieldnames = list(rows[0].keys())
        seen = set(fieldnames)
        for row in rows[1:]:
            for key in row:
                if key not in seen:
                    fieldnames.append(key)
                    seen.add(key)
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def collect_fleet_sessions(stage: Path, local_only: bool, no_collect: bool) -> dict[str, list[Any]]:
    """Parse codex/claude/omp sessions across local + SSH hosts, deduped by content."""
    hosts = fleet.load_hosts(include_local=True)
    if local_only:
        hosts = [h for h in hosts if h.get("local")]
    parsers = {
        "codex": pve.parse_codex_session,
        "claude": pve.parse_claude_session,
        "omp": pve.parse_omp_session,
    }
    seen: set[str] = set()
    out: dict[str, list[Any]] = {"codex": [], "claude": [], "omp": []}
    for host in hosts:
        name = host["name"]
        if host.get("local") or no_collect:
            dirs = (
                fleet.host_dirs(host, stage)
                if host.get("local")
                else {k: (stage / name / k if (stage / name / k).exists() else None) for k in parsers}
            )
        else:
            print(f"collecting {name} ...", flush=True)
            dirs = fleet.host_dirs(host, stage)
        for kind, parser in parsers.items():
            directory = dirs.get(kind)
            if not directory or not Path(directory).exists():
                continue
            for path in sorted(Path(directory).rglob("*.jsonl")):
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
                if digest in seen:
                    continue
                seen.add(digest)
                session = parser(path)
                if session:
                    out[kind].append(session)
        print(
            f"  {name}: cumulative codex={len(out['codex'])} "
            f"claude={len(out['claude'])} omp={len(out['omp'])}",
            flush=True,
        )
    return out


def family_pricing_total(sessions: list[Any], pricing_table: dict[str, Any]) -> float:
    """Pricing-table total cost for a family (the cost anchor for allocation)."""
    total = 0.0
    for session in sessions:
        cost = derive_cost(
            pricing_table,
            session.billable_model,
            input_tokens=session.final_input,
            cache_read_tokens=session.final_cached,
            cache_creation_tokens=session.final_cache_creation,
            output_tokens=session.final_output,
            input_includes_cache=session.input_includes_cache,
        )
        total += float(cost.get("derived_total_cost_usd") or 0.0)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-dir", default="/tmp/fleet-sessions")
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "reports"))
    parser.add_argument("--local-only", action="store_true", help="skip SSH hosts (debug)")
    parser.add_argument("--no-collect", action="store_true", help="reuse an existing stage (skip rsync)")
    args = parser.parse_args(argv)

    pricing_table = load_pricing_table(None)
    fam = collect_fleet_sessions(Path(args.stage_dir), args.local_only, args.no_collect)

    codex_total = family_pricing_total(fam["codex"], pricing_table)
    claude_total = family_pricing_total(fam["claude"], pricing_table)
    print(
        f"fleet pricing totals: codex=${codex_total:,.2f} claude=${claude_total:,.2f} "
        f"(omp uses exact per-turn cost)",
        flush=True,
    )

    codex_command_rows = pve.build_rows_for_model(fam["codex"], {"costUSD": codex_total}, pricing_table)[3]
    claude_command_rows = pve.build_rows_for_model(fam["claude"], {"costUSD": claude_total}, pricing_table)[3]
    omp_command_rows = pve.build_omp_rows(fam["omp"], pricing_table)[3]

    all_command_rows = codex_command_rows + claude_command_rows + omp_command_rows
    v4_5_rows, v4_5_review_rows = pve.build_command_attribution_v4_5_rows(all_command_rows)

    out_dir = Path(args.out_dir)
    write_csv(out_dir / "usage-command-attribution-v4_5.csv", v4_5_rows)
    write_csv(out_dir / "usage-command-attribution-v4_5-review.csv", v4_5_review_rows)

    total_cost = sum(float(r.get("allocated_total_cost_usd") or 0.0) for r in v4_5_rows)
    print(
        f"wrote {out_dir}/usage-command-attribution-v4_5.csv "
        f"rows={len(v4_5_rows)} total_cost=${total_cost:,.2f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
