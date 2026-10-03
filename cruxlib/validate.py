"""Dependency-free validator for the subset of JSON Schema used in schemas/analysis.schema.json."""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict,
          "null": type(None)}


def _check(v, s: dict, path: str, errs: list) -> None:
    if len(errs) > 200:
        return
    t = s.get("type")
    if t:
        ts = t if isinstance(t, list) else [t]
        ok = any(isinstance(v, _TYPES[x]) and not (x in ("integer", "number") and isinstance(v, bool)) for x in ts)
        if not ok:
            errs.append(f"{path}: expected {t}, got {type(v).__name__}")
            return
    if "enum" in s and v not in s["enum"]:
        errs.append(f"{path}: {v!r} not in {s['enum']}")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in s and v < s["minimum"]:
            errs.append(f"{path}: {v} < {s['minimum']}")
        if "maximum" in s and v > s["maximum"]:
            errs.append(f"{path}: {v} > {s['maximum']}")
    if isinstance(v, dict):
        for k in s.get("required", []):
            if k not in v:
                errs.append(f"{path}: missing '{k}'")
        props = s.get("properties", {})
        for k, sub in props.items():
            if k in v:
                _check(v[k], sub, f"{path}.{k}", errs)
        if s.get("additionalProperties") is False:
            for k in v:
                if k not in props:
                    errs.append(f"{path}: unexpected '{k}'")
    if isinstance(v, list) and "items" in s:
        for i, item in enumerate(v):
            _check(item, s["items"], f"{path}[{i}]", errs)


def validate(doc, schema=None) -> list[str]:
    if schema is None:
        with open(os.path.join(HERE, "schemas", "analysis.schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
    errs: list[str] = []
    _check(doc, schema, "$", errs)
    return errs


def validate_file(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return validate(json.load(f))
