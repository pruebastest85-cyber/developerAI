from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field
from types import MappingProxyType

from brain.model_errors import (
    DuplicateJsonKeyError, GeneratedContentTooLargeError, InvalidJsonError,
    JsonDepthExceededError, NonFiniteNumberError, SchemaValidationError,
    StructuredOutputError, TrailingContentError,
)


SCHEMA_KEYWORDS = frozenset(
    {"type", "properties", "required", "additionalProperties", "items", "enum",
     "minLength", "maxLength", "minItems", "maxItems"}
)
SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


def _raw_json_value_is_valid(value, active=None):
    active = set() if active is None else active
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            return False
        value_id = id(value)
        if value_id in active:
            return False
        active.add(value_id)
        try:
            return all(_raw_json_value_is_valid(item, active)
                       for item in value.values())
        finally:
            active.remove(value_id)
    if isinstance(value, list):
        value_id = id(value)
        if value_id in active:
            return False
        active.add(value_id)
        try:
            return all(_raw_json_value_is_valid(item, active) for item in value)
        finally:
            active.remove(value_id)
    return False


def _normalized_schema_value(value):
    if not _raw_json_value_is_valid(value):
        return None, True
    failed = False
    try:
        normalized = thaw_json(freeze_json(value))
    except (StructuredOutputError, RecursionError):
        failed = True
        normalized = None
    return normalized, failed


def freeze_json(value, active=None):
    active = set() if active is None else active
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NonFiniteNumberError()
        return value
    if isinstance(value, Mapping):
        value_id = id(value)
        if value_id in active:
            raise SchemaValidationError(code="recursive_value")
        active.add(value_id)
        try:
            if any(not isinstance(key, str) for key in value):
                raise SchemaValidationError(code="non_string_json_key")
            return MappingProxyType({
                key: freeze_json(item, active) for key, item in value.items()
            })
        finally:
            active.remove(value_id)
    if isinstance(value, (list, tuple)):
        value_id = id(value)
        if value_id in active:
            raise SchemaValidationError(code="recursive_value")
        active.add(value_id)
        try:
            return tuple(freeze_json(item, active) for item in value)
        finally:
            active.remove(value_id)
    raise SchemaValidationError(code="unsupported_json_value")


def thaw_json(value):
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def _depth(value):
    deepest = 0
    stack = [(value, 1)]
    while stack:
        current, level = stack.pop()
        deepest = max(deepest, level)
        if isinstance(current, Mapping):
            stack.extend((item, level + 1) for item in current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend((item, level + 1) for item in current)
    return deepest


def strict_json_loads(text, *, max_bytes, max_depth):
    if not isinstance(text, str):
        raise InvalidJsonError()
    if len(text.encode("utf-8")) > max_bytes:
        raise GeneratedContentTooLargeError()

    def pairs_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateJsonKeyError()
            result[key] = value
        return result

    def reject_constant(_value):
        raise NonFiniteNumberError()

    json_whitespace = " \t\r\n"
    leading = len(text) - len(text.lstrip(json_whitespace))
    decoder = json.JSONDecoder(
        object_pairs_hook=pairs_hook,
        parse_constant=reject_constant,
    )
    invalid_json = False
    depth_failed = False
    try:
        value, end = decoder.raw_decode(text, leading)
    except (DuplicateJsonKeyError, NonFiniteNumberError):
        raise
    except RecursionError:
        depth_failed = True
    except (json.JSONDecodeError, UnicodeError):
        invalid_json = True
    if depth_failed:
        raise JsonDepthExceededError() from None
    if invalid_json:
        raise InvalidJsonError() from None
    if text[end:].strip(json_whitespace):
        raise TrailingContentError()
    if _depth(value) > max_depth:
        raise JsonDepthExceededError()
    return value


@dataclass(frozen=True)
class StructuredOutputSchema:
    name: str
    schema: Mapping = dataclass_field(repr=False)

    def __post_init__(self):
        if (
            not isinstance(self.name, str)
            or not 1 <= len(self.name) <= 64
            or any(
                not (character.isascii()
                     and (character.isalnum() or character in "-_."))
                for character in self.name
            )
        ):
            raise SchemaValidationError(code="invalid_schema_name")
        if not isinstance(self.schema, Mapping):
            raise SchemaValidationError(code="invalid_schema")
        recursion_failed = False
        try:
            self._validate_schema(self.schema, set())
        except RecursionError:
            recursion_failed = True
        if recursion_failed:
            raise SchemaValidationError(code="schema_too_deep") from None
        object.__setattr__(self, "schema", freeze_json(self.schema))

    @classmethod
    def _validate_schema(cls, schema, active):
        if not isinstance(schema, Mapping):
            raise SchemaValidationError(code="invalid_schema")
        schema_id = id(schema)
        if schema_id in active:
            raise SchemaValidationError(code="recursive_schema")
        active.add(schema_id)
        try:
            unknown = set(schema).difference(SCHEMA_KEYWORDS)
            if unknown:
                raise SchemaValidationError(code="unknown_schema_keyword")
            kind = schema.get("type")
            union = isinstance(kind, (list, tuple))
            if union:
                if (
                    tuple(kind) != ("string", "null")
                    or len(set(kind)) != len(kind)
                ):
                    raise SchemaValidationError(code="unsupported_schema_type")
                effective_kind = "string_or_null"
            else:
                if not isinstance(kind, str) or kind not in SCHEMA_TYPES:
                    raise SchemaValidationError(code="unsupported_schema_type")
                effective_kind = kind
            allowed_for_type = {
                "object": {"type", "properties", "required", "additionalProperties", "enum"},
                "array": {"type", "items", "minItems", "maxItems", "enum"},
                "string": {"type", "minLength", "maxLength", "enum"},
                "integer": {"type", "enum"},
                "number": {"type", "enum"},
                "boolean": {"type", "enum"},
                "null": {"type", "enum"},
                "string_or_null": {"type", "minLength", "maxLength", "enum"},
            }[effective_kind]
            if set(schema).difference(allowed_for_type):
                raise SchemaValidationError(code="keyword_not_allowed_for_type")
            if effective_kind == "object":
                properties = schema.get("properties", {})
                if not isinstance(properties, Mapping):
                    raise SchemaValidationError(code="invalid_properties")
                if schema.get("additionalProperties") is not False:
                    raise SchemaValidationError(code="additional_properties_must_be_false")
                required = schema.get("required", ())
                if not isinstance(required, (list, tuple)) or any(
                    not isinstance(item, str) for item in required
                ):
                    raise SchemaValidationError(code="invalid_required")
                if len(set(required)) != len(required) or not set(required).issubset(properties):
                    raise SchemaValidationError(code="invalid_required")
                for key, nested in properties.items():
                    if not isinstance(key, str):
                        raise SchemaValidationError(code="invalid_property_name")
                    cls._validate_schema(nested, active)
            elif effective_kind == "array":
                if "items" not in schema:
                    raise SchemaValidationError(code="missing_items")
                cls._validate_schema(schema["items"], active)
            for lower, upper in (("minLength", "maxLength"), ("minItems", "maxItems")):
                for name in (lower, upper):
                    if name in schema and (
                        isinstance(schema[name], bool)
                        or not isinstance(schema[name], int)
                        or schema[name] < 0
                    ):
                        raise SchemaValidationError(code=f"invalid_{name}")
                if lower in schema and upper in schema and schema[lower] > schema[upper]:
                    raise SchemaValidationError(code="invalid_schema_limits")
            if "enum" in schema:
                enum = schema["enum"]
                if not isinstance(enum, (list, tuple)) or not enum:
                    raise SchemaValidationError(code="invalid_enum")
                sibling_schema = {
                    key: value for key, value in schema.items() if key != "enum"
                }
                for item in enum:
                    normalized, failed = _normalized_schema_value(item)
                    if failed:
                        raise SchemaValidationError(code="invalid_enum") from None
                    cls._validate_value(normalized, sibling_schema)
        finally:
            active.remove(schema_id)

    def to_openai_schema(self):
        return thaw_json(self.schema)

    def validate(self, value):
        self._validate_value(value, self.schema)

    @classmethod
    def _validate_value(cls, value, schema):
        kind = schema["type"]
        if isinstance(kind, (list, tuple)):
            if value is None and "null" in kind:
                if "enum" in schema and freeze_json(value) not in schema["enum"]:
                    raise SchemaValidationError()
                return
            kind = "string" if "string" in kind else kind[0]
        valid = {
            "null": value is None,
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "string": isinstance(value, str),
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
        }[kind]
        if kind == "number" and isinstance(value, float) and not math.isfinite(value):
            valid = False
        if not valid:
            raise SchemaValidationError()
        if "enum" in schema:
            frozen_value = freeze_json(value)
            if frozen_value not in schema["enum"]:
                raise SchemaValidationError()
        if kind == "string":
            if len(value) < schema.get("minLength", 0):
                raise SchemaValidationError()
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                raise SchemaValidationError()
        elif kind == "array":
            if len(value) < schema.get("minItems", 0):
                raise SchemaValidationError()
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                raise SchemaValidationError()
            for item in value:
                cls._validate_value(item, schema["items"])
        elif kind == "object":
            properties = schema.get("properties", {})
            if not set(schema.get("required", ())).issubset(value):
                raise SchemaValidationError()
            if set(value).difference(properties):
                raise SchemaValidationError()
            for key, item in value.items():
                cls._validate_value(item, properties[key])
