"""Owned planner schemas and a deliberately bounded JSON Schema validator.

This validates the keywords used by the bundled schemas, not arbitrary JSON
Schema documents. Unknown schema keywords fail closed. Configuration-specific
semantics (known profiles/metrics, range ordering and clipping) live in the
planner engine. Wire maps use key/value arrays for strict structured outputs.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path


STEPS = ("targets", "campaign", "revise", "bug", "report")
_KEYWORDS = frozenset({
    "$schema", "title", "description", "type", "const", "enum", "properties",
    "required", "additionalProperties", "propertyNames", "minProperties",
    "items", "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum",
})
_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})


class SchemaError(ValueError):
    """An owned schema or planner result violates its structural contract."""


def _reject_constant(value):
    raise SchemaError(f"nonfinite JSON constant: {value}")


def _check_schema(schema: dict, path: str = "schema") -> None:
    if type(schema) is not dict or set(schema) - _KEYWORDS:
        raise SchemaError(f"{path}: unsupported schema keywords or schema type")
    kinds = schema.get("type")
    if type(kinds) is str:
        kinds = [kinds]
    if (type(kinds) is not list or not kinds
            or any(type(kind) is not str or kind not in _TYPES for kind in kinds)
            or len(set(kinds)) != len(kinds)):
        raise SchemaError(f"{path}: unsupported or missing schema type")
    if "properties" in schema:
        if type(schema["properties"]) is not dict or any(type(key) is not str for key in schema["properties"]):
            raise SchemaError(f"{path}: invalid properties")
        for key, child in schema["properties"].items():
            _check_schema(child, f"{path}.{key}")
    if "required" in schema:
        required = schema["required"]
        if (type(required) is not list or any(type(key) is not str for key in required)
                or len(set(required)) != len(required)
                or set(required) - schema.get("properties", {}).keys()):
            raise SchemaError(f"{path}: invalid required properties")
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if type(extra) is dict:
            _check_schema(extra, f"{path}.additionalProperties")
        elif type(extra) is not bool:
            raise SchemaError(f"{path}: invalid additionalProperties")
    for key in ("items", "propertyNames"):
        if key in schema:
            _check_schema(schema[key], f"{path}.{key}")
    for key in ("minProperties", "minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise SchemaError(f"{path}: invalid {key}")
    for key in ("minimum", "maximum"):
        if key in schema and not _number(schema[key]):
            raise SchemaError(f"{path}: invalid {key}")
    if "enum" in schema and (type(schema["enum"]) is not list or not schema["enum"]):
        raise SchemaError(f"{path}: invalid enum")


def load_schema(step: str, *, wire: bool = False) -> dict:
    if step not in STEPS or type(wire) is not bool:
        raise SchemaError("unknown planner step or schema variant")
    path = Path(__file__).with_name("schemas") / f"{step}{'.wire' if wire else ''}.json"
    try:
        schema = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
        _check_schema(schema)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise SchemaError(f"cannot load {step} schema: {error}") from error
    return schema


def _number(value) -> bool:
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _is_type(value, kind: str) -> bool:
    return {"object": type(value) is dict, "array": type(value) is list,
            "string": type(value) is str, "integer": type(value) is int,
            "number": _number(value), "boolean": type(value) is bool,
            "null": value is None}[kind]


def _equal(first, second) -> bool:
    # JSON booleans must never compare equal to integer enum/constant values.
    if type(first) is bool or type(second) is bool:
        return type(first) is type(second) and first == second
    return first == second


def _validate(value, schema: dict, path: str) -> None:
    kinds = schema["type"] if type(schema["type"]) is list else [schema["type"]]
    if not any(_is_type(value, kind) for kind in kinds):
        raise SchemaError(f"{path}: expected {' or '.join(kinds)}")
    if "const" in schema and not _equal(value, schema["const"]):
        raise SchemaError(f"{path}: incorrect constant")
    if "enum" in schema and not any(_equal(value, choice) for choice in schema["enum"]):
        raise SchemaError(f"{path}: value is not in enum")
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise SchemaError(f"{path}: object keys must be strings")
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise SchemaError(f"{path}: missing properties {', '.join(sorted(missing))}")
        if len(value) < schema.get("minProperties", 0):
            raise SchemaError(f"{path}: too few properties")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            if "propertyNames" in schema:
                _validate(key, schema["propertyNames"], f"{path}.<key>")
            if key in properties:
                _validate(child, properties[key], f"{path}.{key}")
            elif additional is False:
                raise SchemaError(f"{path}: unknown property {key}")
            elif type(additional) is dict:
                _validate(child, additional, f"{path}.{key}")
    elif type(value) is list:
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            raise SchemaError(f"{path}: invalid item count")
        if "items" in schema:
            for index, child in enumerate(value):
                _validate(child, schema["items"], f"{path}[{index}]")
    elif type(value) is str:
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            raise SchemaError(f"{path}: invalid string length")
    elif _number(value):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise SchemaError(f"{path}: number outside allowed bounds")


def validate_output(step: str, value, *, wire: bool = False) -> None:
    try:
        _validate(value, load_schema(step, wire=wire), "$")
    except RecursionError as error:
        raise SchemaError("planner output is too deeply nested") from error


def _to_pairs(value: dict) -> list[dict]:
    return [{"key": key, "value": value[key]} for key in sorted(value)]


def _from_pairs(value: list[dict], path: str) -> dict:
    output = {}
    for item in value:
        key = item["key"]
        if key in output:
            raise SchemaError(f"{path}: duplicate map key {key}")
        output[key] = item["value"]
    return output


def _map_fields(step: str, value: dict, transform) -> dict:
    result = deepcopy(value)
    if step == "campaign":
        for name in ("profile_instructions", "search_ranges"):
            result[name] = transform(result[name], name)
        for index, experiment in enumerate(result["experiments"]):
            experiment["tuning_ranges"] = transform(experiment["tuning_ranges"], f"experiments[{index}].tuning_ranges")
    elif step == "revise":
        result["search_ranges"] = transform(result["search_ranges"], "search_ranges")
    return result


def to_wire(step: str, canonical: dict) -> dict:
    validate_output(step, canonical)
    result = _map_fields(step, canonical, lambda value, _: _to_pairs(value))
    validate_output(step, result, wire=True)
    return result


def from_wire(step: str, wire_value: dict) -> dict:
    validate_output(step, wire_value, wire=True)
    result = _map_fields(step, wire_value, _from_pairs)
    validate_output(step, result)
    return result
