import unittest
import math

from brain.model_errors import (
    DuplicateJsonKeyError, InvalidJsonError, JsonDepthExceededError,
    NonFiniteNumberError, SchemaValidationError, TrailingContentError,
)
from brain.structured_json import StructuredOutputSchema, strict_json_loads


def object_schema():
    return StructuredOutputSchema("answer", {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "minLength": 1},
            "scores": {
                "type": "array", "items": {"type": "integer"},
                "maxItems": 2,
            },
        },
        "required": ["answer"],
        "additionalProperties": False,
    })


class StrictJsonTests(unittest.TestCase):
    def parse(self, text, depth=8):
        return strict_json_loads(text, max_bytes=1000, max_depth=depth)

    def test_accepts_one_json_document_with_whitespace(self):
        self.assertEqual(self.parse(' \n {"a":[1,true,null]} \t'), {"a": [1, True, None]})

    def test_accepts_only_the_four_json_whitespace_characters(self):
        document = " \t\r\n{}\n\r\t "
        self.assertEqual(self.parse(document), {})
        for character in ("\u00a0", "\u2003", "\u1680",
                          "\u2028", "\u2029", "\u3000"):
            with self.subTest(character=ascii(character), position="before"):
                with self.assertRaises(InvalidJsonError):
                    self.parse(character + "{}")
            with self.subTest(character=ascii(character), position="after"):
                with self.assertRaises(TrailingContentError):
                    self.parse("{}" + character)

    def test_non_json_whitespace_errors_do_not_retain_marker(self):
        marker = "\u00a0whitespace-super-secret"
        with self.assertRaises(InvalidJsonError) as caught:
            self.parse(marker + "{}")
        exposed = " ".join([
            str(caught.exception), repr(caught.exception),
            repr(caught.exception.args), repr(caught.exception.__dict__),
            repr(caught.exception.__cause__), repr(caught.exception.__context__),
        ])
        self.assertNotIn(marker, exposed)

    def test_rejects_invalid_json_markdown_and_trailing_text(self):
        with self.assertRaises(InvalidJsonError):
            self.parse("```json\n{}\n```")
        with self.assertRaises(InvalidJsonError):
            self.parse("prefix {}")
        with self.assertRaises(TrailingContentError):
            self.parse("{} extra")

    def test_rejects_duplicate_keys_and_non_finite_numbers(self):
        with self.assertRaises(DuplicateJsonKeyError):
            self.parse('{"a":1,"a":2}')
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaises(NonFiniteNumberError):
                    self.parse(value)

    def test_enforces_depth_and_utf8_size(self):
        self.assertEqual(self.parse("[[1]]", depth=3), [[1]])
        with self.assertRaises(JsonDepthExceededError):
            self.parse("[[1]]", depth=2)
        with self.assertRaises(Exception) as caught:
            strict_json_loads('"á"', max_bytes=3, max_depth=2)
        self.assertEqual(caught.exception.code, "generated_content_too_large")

    def test_extreme_depth_is_always_a_typed_safe_error(self):
        document = "[" * 5000 + "0" + "]" * 5000
        with self.assertRaises(JsonDepthExceededError) as caught:
            strict_json_loads(document, max_bytes=20000, max_depth=32)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_invalid_json_error_does_not_retain_document(self):
        marker = "generated-super-secret"
        with self.assertRaises(InvalidJsonError) as caught:
            self.parse(marker)
        error = caught.exception
        exposed = " ".join([
            str(error), repr(error), repr(error.args), repr(error.__dict__),
            repr(error.__cause__), repr(error.__context__),
        ])
        self.assertNotIn(marker, exposed)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)


class StructuredOutputSchemaTests(unittest.TestCase):
    def test_validates_required_types_limits_and_extra_properties(self):
        schema = object_schema()
        schema.validate({"answer": "yes", "scores": [1, 2]})
        for value in [
            {}, {"answer": ""}, {"answer": "yes", "extra": 1},
            {"answer": "yes", "scores": [1, 2, 3]},
            {"answer": "yes", "scores": [True]},
        ]:
            with self.subTest(value=value):
                with self.assertRaises(SchemaValidationError):
                    schema.validate(value)

    def test_schema_is_deeply_copied_and_serialization_is_independent(self):
        source = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }
        schema = StructuredOutputSchema("x", source)
        source["properties"]["value"]["type"] = "integer"
        exported = schema.to_openai_schema()
        exported["properties"]["value"]["type"] = "boolean"
        self.assertEqual(schema.to_openai_schema()["properties"]["value"]["type"], "string")

    def test_rejects_open_or_unsupported_schema_features(self):
        invalid = [
            {"type": "object", "properties": {}},
            {"type": "object", "properties": {}, "additionalProperties": True},
            {"type": "string", "$ref": "#"},
            {"type": "string", "items": {"type": "string"}},
            {"type": ["string", "null"]},
            {"type": "array"},
        ]
        for schema in invalid:
            with self.subTest(schema=schema):
                with self.assertRaises(SchemaValidationError):
                    StructuredOutputSchema("x", schema)

    def test_rejects_recursive_schema(self):
        schema = {"type": "array"}
        schema["items"] = schema
        with self.assertRaises(SchemaValidationError):
            StructuredOutputSchema("x", schema)

    def test_enum_values_must_match_declared_type(self):
        with self.assertRaises(SchemaValidationError):
            StructuredOutputSchema("x", {"type": "integer", "enum": [True]})

    def test_enum_supports_every_declared_json_type(self):
        schemas_and_values = [
            ({"type": "null", "enum": [None]}, None),
            ({"type": "boolean", "enum": [True]}, True),
            ({"type": "integer", "enum": [2]}, 2),
            ({"type": "number", "enum": [2.5]}, 2.5),
            ({"type": "string", "enum": ["yes"], "minLength": 2}, "yes"),
            ({"type": "array", "items": {"type": "integer"},
              "enum": [[1, 2]], "maxItems": 2}, [1, 2]),
            ({
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
                "enum": [{"value": 1}],
            }, {"value": 1}),
        ]
        for definition, value in schemas_and_values:
            with self.subTest(definition=definition):
                contract = StructuredOutputSchema("enum_test", definition)
                contract.validate(value)

    def test_enum_values_must_be_json_typed_finite_and_non_recursive(self):
        circular = []
        circular.append(circular)
        invalid = [
            {"type": "integer", "enum": [True]},
            {"type": "number", "enum": [True]},
            {"type": "number", "enum": [math.nan]},
            {"type": "number", "enum": [math.inf]},
            {"type": "array", "items": {"type": "integer"}, "enum": [[True]]},
            {"type": "array", "items": {"type": "integer"}, "enum": [(1,)]},
            {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
                "enum": [{1: "bad"}],
            },
            {"type": "array", "items": {"type": "array",
                                        "items": {"type": "integer"}},
             "enum": [circular]},
        ]
        for definition in invalid:
            with self.subTest(definition=repr(definition)[:100]):
                with self.assertRaises(SchemaValidationError):
                    StructuredOutputSchema("enum_test", definition)

    def test_enum_values_obey_sibling_constraints(self):
        with self.assertRaises(SchemaValidationError):
            StructuredOutputSchema(
                "x", {"type": "string", "minLength": 3, "enum": ["no"]}
            )
        with self.assertRaises(SchemaValidationError):
            StructuredOutputSchema(
                "x", {"type": "array", "items": {"type": "integer"},
                      "maxItems": 1, "enum": [[1, 2]]}
            )

    def test_shared_schema_is_allowed_but_cycle_is_rejected(self):
        shared = {"type": "string"}
        schema = StructuredOutputSchema("shared", {
            "type": "object",
            "properties": {"first": shared, "second": shared},
            "required": ["first", "second"],
            "additionalProperties": False,
        })
        schema.validate({"first": "a", "second": "b"})
        recursive = {"type": "array"}
        recursive["items"] = recursive
        with self.assertRaises(SchemaValidationError):
            StructuredOutputSchema("recursive", recursive)

    def test_schema_repr_redacts_complete_schema(self):
        marker = "schema-super-secret"
        contract = StructuredOutputSchema("redacted", {
            "type": "string", "enum": [marker],
        })
        self.assertNotIn(marker, repr(contract))


if __name__ == "__main__":
    unittest.main()
