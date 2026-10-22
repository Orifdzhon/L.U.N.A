"""Minimal JSON-schema validator for LLM-supplied tool arguments.

Supports the subset tools actually use: ``type`` (single or list), ``enum``, ``const``,
``properties``, ``required``, ``additionalProperties`` (bool or schema), ``items``,
``minLength``/``maxLength``, ``minimum``/``maximum``, ``minItems``/``maxItems``.
Unknown keywords are ignored. Returns human-readable error strings, never raises on bad data.
"""

from __future__ import annotations

from typing import Any

_TYPE_CHECKS: dict[str, Any] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list | tuple),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_args(value: Any, schema: dict[str, Any], path: str = "arguments") -> list[str]:
    """Return a list of violations of ``schema`` by ``value`` (empty when valid)."""
    errors: list[str] = []

    expected = schema.get("type")
    if expected is not None:
        names = [expected] if isinstance(expected, str) else list(expected)
        if not any(_TYPE_CHECKS.get(n, lambda _v: True)(value) for n in names):
            errors.append(f"{path}: expected {' or '.join(names)}, got {_type_name(value)}")
            return errors

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']!r}")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")

    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: less than {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: greater than {schema['maximum']}")

    if isinstance(value, dict):
        errors.extend(_check_object(value, schema, path))
    elif isinstance(value, list | tuple):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                errors.extend(validate_args(item, item_schema, f"{path}[{i}]"))
    return errors


def _check_object(value: dict[str, Any], schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    props: dict[str, Any] = schema.get("properties", {})
    for name in schema.get("required", ()):
        if name not in value:
            errors.append(f"{path}: missing required field {name!r}")
    extra = schema.get("additionalProperties", True)
    for key, item in value.items():
        if key in props:
            errors.extend(validate_args(item, props[key], f"{path}.{key}"))
        elif extra is False:
            errors.append(f"{path}: unexpected field {key!r}")
        elif isinstance(extra, dict):
            errors.extend(validate_args(item, extra, f"{path}.{key}"))
    return errors


def _type_name(value: Any) -> str:
    return "null" if value is None else type(value).__name__
