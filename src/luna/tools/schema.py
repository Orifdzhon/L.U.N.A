"""Minimal, fail-closed JSON-schema subset for tool arguments.

``check_schema`` rejects (at registration time) any schema that uses a keyword outside the
supported set, so a constraint is never silently ignored. ``validate_args`` checks LLM-supplied
values; it never raises on bad data and returns human-readable violations.

Supported keywords: ``type`` (one name or a list), ``enum``, ``const``, ``properties``,
``required``, ``additionalProperties`` (bool or schema; **defaults to False**, also for nested
objects), ``items``, ``minLength``/``maxLength``, ``minimum``/``maximum``,
``minItems``/``maxItems`` and the annotations ``description``/``title``.
"""

from __future__ import annotations

import math
from typing import Any

TYPE_NAMES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_ANNOTATIONS = frozenset({"description", "title"})
SUPPORTED_KEYWORDS = (
    frozenset(
        {
            "type",
            "enum",
            "const",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "minItems",
            "maxItems",
        }
    )
    | _ANNOTATIONS
)
MAX_SCHEMA_DEPTH = 16


class SchemaError(ValueError):
    """The schema is malformed or uses an unsupported keyword."""


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _is_count(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def check_schema(
    schema: Any, path: str = "parameters", *, root: bool = True, _depth: int = 0
) -> None:
    """Raise :class:`SchemaError` unless ``schema`` is well-formed and fully supported."""
    if _depth > MAX_SCHEMA_DEPTH:
        raise SchemaError(f"{path}: schema nested too deeply")
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: schema must be an object")
    unknown = sorted(str(k) for k in schema if k not in SUPPORTED_KEYWORDS)
    if unknown:
        raise SchemaError(f"{path}: unsupported keyword(s) {unknown}")
    if root and schema.get("type") != "object":
        raise SchemaError(f"{path}: root schema must have type 'object'")

    if "type" in schema:
        t = schema["type"]
        names = [t] if isinstance(t, str) else t
        if not isinstance(names, list) or not names:
            raise SchemaError(f"{path}: invalid 'type'")
        for n in names:
            if not isinstance(n, str) or n not in TYPE_NAMES:
                raise SchemaError(f"{path}: unknown type {n!r}")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise SchemaError(f"{path}: 'enum' must be a non-empty list")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and not _is_count(schema[key]):
            raise SchemaError(f"{path}: {key!r} must be a non-negative integer")
    for key in ("minimum", "maximum"):
        if key in schema and not (_is_num(schema[key]) and math.isfinite(schema[key])):
            raise SchemaError(f"{path}: {key!r} must be a finite number")
    for key in _ANNOTATIONS:
        if key in schema and not isinstance(schema[key], str):
            raise SchemaError(f"{path}: {key!r} must be a string")
    if "required" in schema:
        req = schema["required"]
        if not isinstance(req, list) or not all(isinstance(r, str) for r in req):
            raise SchemaError(f"{path}: 'required' must be a list of strings")
    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict):
            raise SchemaError(f"{path}: 'properties' must be an object")
        for name, sub in props.items():
            if not isinstance(name, str):
                raise SchemaError(f"{path}: property names must be strings")
            check_schema(sub, f"{path}.{name}", root=False, _depth=_depth + 1)
    extra = schema.get("additionalProperties", False)
    if isinstance(extra, dict):
        check_schema(extra, f"{path}.*", root=False, _depth=_depth + 1)
    elif not isinstance(extra, bool):
        raise SchemaError(f"{path}: 'additionalProperties' must be a bool or a schema")
    if "items" in schema:
        check_schema(schema["items"], f"{path}[]", root=False, _depth=_depth + 1)


def _strict_eq(a: Any, b: Any) -> bool:
    """Equality where ``True`` != ``1`` and ``1`` == ``1.0`` (JSON semantics)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_num(a) and _is_num(b):
        return a == b
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(_strict_eq(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_strict_eq(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _type_ok(name: str, v: Any) -> bool:
    match name:
        case "object":
            return isinstance(v, dict)
        case "array":
            return isinstance(v, list | tuple)
        case "string":
            return isinstance(v, str)
        case "integer":
            return isinstance(v, int) and not isinstance(v, bool)
        case "number":
            return _is_num(v) and math.isfinite(v)
        case "boolean":
            return isinstance(v, bool)
        case "null":
            return v is None
    return False


def validate_args(value: Any, schema: dict[str, Any], path: str = "arguments") -> list[str]:
    """Return violations of a schema accepted by :func:`check_schema` (empty when valid)."""
    errors: list[str] = []

    if "type" in schema:
        t = schema["type"]
        names = [t] if isinstance(t, str) else list(t)
        if not any(_type_ok(n, value) for n in names):
            errors.append(f"{path}: expected {' or '.join(names)}, got {_type_name(value)}")
            return errors

    if "const" in schema and not _strict_eq(value, schema["const"]):
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and not any(_strict_eq(value, e) for e in schema["enum"]):
        errors.append(f"{path}: must be one of {schema['enum']!r}")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")

    if _is_num(value):
        if "minimum" in schema and not value >= schema["minimum"]:
            errors.append(f"{path}: less than {schema['minimum']}")
        if "maximum" in schema and not value <= schema["maximum"]:
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
    extra = schema.get("additionalProperties", False)
    for key, item in value.items():
        if key in props:
            errors.extend(validate_args(item, props[key], f"{path}.{key}"))
        elif isinstance(extra, dict):
            errors.extend(validate_args(item, extra, f"{path}.{key}"))
        elif extra is not True:
            errors.append(f"{path}: unexpected field {key!r}")
    return errors


def _type_name(value: Any) -> str:
    return "null" if value is None else type(value).__name__
