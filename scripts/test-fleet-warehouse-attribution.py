#!/usr/bin/env python3
"""Tests for the fleet warehouse attribution orchestrator."""
from __future__ import annotations

import csv
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPT_PATH = Path(__file__).resolve().parent / "fleet_warehouse_attribution.py"
spec = importlib.util.spec_from_file_location("fleet_warehouse_attribution", SCRIPT_PATH)
fwa = importlib.util.module_from_spec(spec)
sys.modules["fleet_warehouse_attribution"] = fwa
assert spec and spec.loader
spec.loader.exec_module(fwa)


class FleetWarehouseAttributionTests(unittest.TestCase):
    def test_write_csv_header_is_union_of_all_row_keys(self) -> None:
        rows = [
            {"a": 1, "b": 2},
            {"a": 3, "c": 4},  # introduces a new column not in row 0
        ]
        out = Path("/tmp/_fwa_test_union.csv")
        fwa.write_csv(out, rows)
        with out.open() as handle:
            header = next(csv.reader(handle))
        self.assertEqual(header, ["a", "b", "c"])

    def test_write_csv_empty_rows_writes_sentinel(self) -> None:
        out = Path("/tmp/_fwa_test_empty.csv")
        fwa.write_csv(out, [])
        self.assertEqual(out.read_text(), "empty\n")

    def test_family_pricing_total_sums_derived_total(self) -> None:
        original = fwa.derive_cost
        fwa.derive_cost = lambda *a, **k: {"derived_total_cost_usd": 2.5}
        try:
            sessions = [
                SimpleNamespace(
                    billable_model="m", final_input=1, final_cached=0,
                    final_cache_creation=0, final_output=1, input_includes_cache=False,
                )
                for _ in range(3)
            ]
            self.assertAlmostEqual(fwa.family_pricing_total(sessions, {}), 7.5)
        finally:
            fwa.derive_cost = original

    def test_family_pricing_total_tolerates_none_total(self) -> None:
        original = fwa.derive_cost
        fwa.derive_cost = lambda *a, **k: {"derived_total_cost_usd": None}
        try:
            session = SimpleNamespace(
                billable_model="m", final_input=1, final_cached=0,
                final_cache_creation=0, final_output=1, input_includes_cache=False,
            )
            self.assertEqual(fwa.family_pricing_total([session], {}), 0.0)
        finally:
            fwa.derive_cost = original


if __name__ == "__main__":
    unittest.main()
