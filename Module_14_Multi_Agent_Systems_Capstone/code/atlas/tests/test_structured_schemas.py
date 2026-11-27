"""Structured-output schemas use only what the API accepts.

On 2026-10-23 the worker log showed a 400 on every executive tick:
"output_config.format.schema: For 'integer' type, properties maximum,
minimum are not supported". Signal triage had failed on each run. Numeric,
string-length and array-size constraints are refused; the code clamps
instead. This walks every module-level *_SCHEMA dict in core and agents.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any

import agents
import core

REFUSED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
           "minLength", "maxLength", "maxItems", "pattern"}


def _schemas() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for pkg in (core, agents):
        for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            try:
                m = importlib.import_module(mod.name)
            except Exception:  # noqa: BLE001 - an optional dependency (playwright) is not this test's concern
                continue
            for name, value in vars(m).items():
                if name.endswith("_SCHEMA") and isinstance(value, dict) and value.get("type") == "object":
                    out[f"{mod.name}.{name}"] = value
    return out


def _keys(node: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            # "properties" maps field names to schemas; a field may be called "pattern".
            if k in REFUSED and not path.endswith(".properties"):
                found.append(f"{path}.{k}")
            found += _keys(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _keys(v, f"{path}[{i}]")
    return found


def test_triage_and_discovery_schemas_are_found():
    names = _schemas()
    assert "core.loop.triage.VERDICT_SCHEMA" in names and "core.loop.discovery_scan.VET_SCHEMA" in names


def test_no_schema_uses_a_refused_constraint():
    bad = {name: _keys(schema) for name, schema in _schemas().items()}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"structured outputs refuse these keywords; clamp in code instead: {bad}"
