"""Turn a Pydantic JSON schema into one the provider accepts.

Pydantic is the real gate (every response is validated with the model in strict mode); this
only removes keywords providers ignore or reject and inlines `$ref`s, so constrained decoding
works across schema-support differences between model versions. A unit test checks every role
schema against the accepted subset.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

ALLOWED_KEYS = {
    "type",
    "properties",
    "required",
    "items",
    "enum",
    "anyOf",
    "description",
    "maxLength",
    "minLength",
    "maxItems",
    "minItems",
    "pattern",
    "format",
    "minimum",
    "maximum",
    "nullable",
    "propertyOrdering",
}


def _inline(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    if depth > 20:
        raise ValueError("schema nesting too deep (recursive model?)")
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return _inline(copy.deepcopy(defs[name]), defs, depth + 1)
        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            if key == "properties":
                cleaned[key] = {prop: _inline(sub, defs, depth + 1) for prop, sub in value.items()}
            elif key in ALLOWED_KEYS:
                cleaned[key] = _inline(value, defs, depth + 1)
            elif key == "const":
                cleaned["enum"] = [value]
        if "allOf" in node and len(node["allOf"]) == 1:
            cleaned.update(_inline(node["allOf"][0], defs, depth + 1))
        return cleaned
    if isinstance(node, list):
        return [_inline(item, defs, depth + 1) for item in node]
    return node


def provider_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    schema = _inline(raw, defs)
    schema.setdefault("type", "object")
    return schema


def check_supported(schema: Any) -> list[str]:
    """Return any keys outside the accepted subset (used by tests)."""
    problems: list[str] = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties":
                for sub in value.values():
                    problems += check_supported(sub)
            elif key not in ALLOWED_KEYS:
                problems.append(key)
            else:
                problems += check_supported(value)
    elif isinstance(schema, list):
        for item in schema:
            problems += check_supported(item)
    return problems
