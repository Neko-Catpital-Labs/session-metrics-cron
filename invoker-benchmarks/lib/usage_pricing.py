#!/usr/bin/env python3
"""Compatibility shim for the canonical usage_costing module."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_HERE = Path(__file__).resolve()
_CANDIDATES = (
    _HERE.with_name("usage_costing.py"),
    _HERE.parents[2] / "scripts" / "usage_costing.py",
)

for _path in _CANDIDATES:
    if _path.exists():
        _spec = importlib.util.spec_from_file_location("usage_costing", _path)
        if _spec and _spec.loader:
            _module = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_module)
            globals().update({name: getattr(_module, name) for name in dir(_module) if not name.startswith("_")})
            break
else:
    raise ImportError("Unable to locate canonical usage_costing.py")
