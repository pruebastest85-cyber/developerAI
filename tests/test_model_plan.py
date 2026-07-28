import math
import unittest
from types import MappingProxyType
from unittest import mock

from brain.model_plan import (
    AUTHORITY_FIELDS,
    ModelArgumentContract,
    ModelOperationContract,
    ModelPlanAdaptationError,
    ModelPlanAdapter,
    ModelPlanDecision,
    ModelPlanLimits,
    ModelPlanStep,
    MODEL_PLAN_OUTPUT_SCHEMA,
    SAFE_MODEL_OPERATION_CATALOG,
)
from brain.workflow_plan import StepSpec, WorkflowPlan


def step_payload(**changes):
    payload = {
        "id": "inspect_1",
        "tool": "code_reader",
        "action": "read_file",
        "args": {"path": "brain/agent.py", "max_lines": 200},
        "goal": "inspeccionar el agente",
        "depends_on": [],
        "justification": "archivo central",
    }
    payload.update(changes)
    return payload


def decision_payload(**changes):
    payload = {
        "schema_version": "1",
        "goal": "inspeccionar de forma segura",
        "completed": False,
        "steps": [step_payload()],
        "message": "",
    }
    payload.update(changes)
    return payload


class ModelPlanTests(unittest.TestCase):
    def adapt(self, payload=None, catalog=SAFE_MODEL_OPERATION_CATALOG):
        decision = ModelPlanDecision.from_mapping(payload or decision_payload())
        return ModelPlanAdapter(catalog).adapt(decision)

    def assert_code(self, code, callable_):
        with self.assertRaises(ModelPlanAdaptationError) as caught:
            callable_()
        error = caught.exception
        self.assertEqual(error.code, code)
        self.assertNotIn("SECRET_MARKER", str(error))
        self.assertNotIn("SECRET_MARKER", repr(error))
        self.assertNotIn("SECRET_MARKER", repr(error.args))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_minimal_plan_adapts_to_real_workflow_types(self):
        workflow = self.adapt()
        self.assertIsInstance(workflow, WorkflowPlan)
        self.assertIsInstance(workflow.steps[0], StepSpec)
        self.assertEqual(workflow.steps[0].approval, "policy")
        self.assertTrue(workflow.steps[0].required)
        self.assertFalse(workflow.steps[0].repeat_completed)
        self.assertEqual(dict(workflow.steps[0].bindings), {})

    def test_multiple_steps_preserve_dependencies_and_order(self):
        payload = decision_payload(
            steps=[
                step_payload(id="inspect_1"),
                step_payload(
                    id="analyze_1",
                    tool="code_analyzer",
                    action="summarize",
                    args={"path": "brain/agent.py"},
                    depends_on=["inspect_1"],
                ),
                step_payload(
                    id="tests_1",
                    tool="test_runner",
                    action="run_tests",
                    args={},
                    depends_on=["analyze_1"],
                ),
            ]
        )
        workflow = self.adapt(payload)
        self.assertEqual(
            tuple(step.id for step in workflow.execution_order()),
            ("inspect_1", "analyze_1", "tests_1"),
        )
        self.assertEqual(workflow.steps[2].depends_on, ("analyze_1",))

    def test_completed_decision_has_no_workflow_steps(self):
        decision = ModelPlanDecision.from_mapping(
            decision_payload(completed=True, steps=[], message="Trabajo terminado.")
        )
        workflow = ModelPlanAdapter(SAFE_MODEL_OPERATION_CATALOG).adapt(decision)
        self.assertTrue(decision.completed)
        self.assertEqual(workflow.steps, ())

    def test_decision_and_arguments_are_immutable_and_defensive(self):
        args = {"path": "brain/agent.py", "max_lines": 200}
        dependencies = []
        payload = decision_payload(steps=[step_payload(args=args, depends_on=dependencies)])
        decision = ModelPlanDecision.from_mapping(payload)
        workflow = ModelPlanAdapter(SAFE_MODEL_OPERATION_CATALOG).adapt(decision)
        args["path"] = "SECRET_MARKER"
        dependencies.append("later")
        payload["steps"].clear()
        self.assertEqual(decision.steps[0].args["path"], "brain/agent.py")
        self.assertEqual(workflow.steps[0].args["path"], "brain/agent.py")
        self.assertEqual(decision.steps[0].depends_on, ())
        with self.assertRaises(TypeError):
            decision.steps[0].args["path"] = "changed"
        with self.assertRaises((AttributeError, TypeError)):
            decision.steps += decision.steps

    def test_version_root_and_step_shape_are_closed(self):
        self.assert_code(
            "unsupported_plan_version",
            lambda: ModelPlanDecision.from_mapping(
                decision_payload(schema_version="2")
            ),
        )
        for field_name in ("extra", *AUTHORITY_FIELDS):
            with self.subTest(root_field=field_name):
                payload = decision_payload()
                payload[field_name] = "SECRET_MARKER"
                self.assert_code(
                    "invalid_model_plan",
                    lambda payload=payload: ModelPlanDecision.from_mapping(payload),
                )
            with self.subTest(step_field=field_name):
                bad_step = step_payload()
                bad_step[field_name] = "SECRET_MARKER"
                payload = decision_payload(steps=[bad_step])
                self.assert_code(
                    "invalid_model_plan",
                    lambda payload=payload: ModelPlanDecision.from_mapping(payload),
                )

    def test_completed_and_step_count_invariants(self):
        self.assert_code(
            "invalid_model_plan",
            lambda: ModelPlanDecision.from_mapping(decision_payload(steps=[])),
        )
        self.assert_code(
            "invalid_model_plan",
            lambda: ModelPlanDecision.from_mapping(
                decision_payload(completed=True, message="", steps=[])
            ),
        )
        self.assert_code(
            "invalid_model_plan",
            lambda: ModelPlanDecision.from_mapping(
                decision_payload(completed=True, message="done")
            ),
        )
        self.assert_code(
            "step_limit_exceeded",
            lambda: ModelPlanDecision.from_mapping(
                decision_payload(
                    steps=[step_payload(id=f"step_{index}") for index in range(9)]
                )
            ),
        )

    def test_ids_are_canonical_unique_and_bounded(self):
        for invalid in ("", "A", "two-words", "_start", "a" * 65):
            with self.subTest(invalid=invalid):
                self.assert_code(
                    "invalid_model_plan",
                    lambda invalid=invalid: ModelPlanDecision.from_mapping(
                        decision_payload(steps=[step_payload(id=invalid)])
                    ),
                )
        self.assert_code(
            "invalid_dependency",
            lambda: ModelPlanDecision.from_mapping(
                decision_payload(
                    steps=[step_payload(id="same"), step_payload(id="same")]
                )
            ),
        )

    def test_invalid_dependencies_and_cycles_are_distinct(self):
        cases = [
            [step_payload(depends_on=["missing"])],
            [
                step_payload(id="first"),
                step_payload(id="second", depends_on=["first", "first"]),
            ],
            [step_payload(id="self", depends_on=["self"])],
        ]
        for steps in cases:
            with self.subTest(steps=steps):
                self.assert_code(
                    "invalid_dependency",
                    lambda steps=steps: ModelPlanDecision.from_mapping(
                        decision_payload(steps=steps)
                    ),
                )
        self.assert_code(
            "cyclic_dependencies",
            lambda: ModelPlanDecision.from_mapping(
                decision_payload(
                    steps=[
                        step_payload(id="first", depends_on=["second"]),
                        step_payload(id="second", depends_on=["first"]),
                    ]
                )
            ),
        )
        self.assert_code(
            "cyclic_dependencies",
            lambda: ModelPlanDecision.from_mapping(
                decision_payload(
                    steps=[
                        step_payload(id="first", depends_on=["third"]),
                        step_payload(id="second", depends_on=["first"]),
                        step_payload(id="third", depends_on=["second"]),
                    ]
                )
            ),
        )

    def test_tool_action_and_pair_must_match_exact_catalog(self):
        cases = [
            ("unknown_tool", step_payload(tool="unknown")),
            ("unknown_action", step_payload(action="unknown")),
            (
                "unknown_action",
                step_payload(tool="code_analyzer", action="read_file"),
            ),
            ("unknown_tool", step_payload(tool="Code_Reader")),
            ("unknown_action", step_payload(action="Read_File")),
            ("unknown_tool", step_payload(tool="code")),
        ]
        for code, bad_step in cases:
            with self.subTest(code=code, bad_step=bad_step):
                self.assert_code(
                    code,
                    lambda bad_step=bad_step: self.adapt(
                        decision_payload(steps=[bad_step])
                    ),
                )

    def test_arguments_are_closed_required_and_strictly_typed(self):
        cases = [
            step_payload(args={"max_lines": 10}),
            step_payload(args={"path": ""}),
            step_payload(args={"path": "brain/agent.py", "extra": 1}),
            step_payload(args={"path": 7}),
            step_payload(args={"path": "brain/agent.py", "max_lines": True}),
            step_payload(args={"path": "brain/agent.py", "max_lines": 0}),
        ]
        for bad_step in cases:
            with self.subTest(bad_step=bad_step):
                self.assert_code(
                    "invalid_arguments",
                    lambda bad_step=bad_step: self.adapt(
                        decision_payload(steps=[bad_step])
                    ),
                )

    def test_forbidden_operations_cannot_be_injected(self):
        for action in (
            "commit",
            "Commit",
            "autocommit",
            "push",
            "forcepush",
            "rollback",
            "reset",
            "hardreset",
            "delete",
            "remove",
            "removeAll",
            "unlink",
            "rmdir",
        ):
            with self.subTest(action=action):
                catalog = {
                    ("git_tools", action): ModelOperationContract(),
                }
                self.assert_code(
                    "invalid_model_plan",
                    lambda catalog=catalog: ModelPlanAdapter(catalog),
                )

    def test_custom_catalog_can_only_narrow_base_authority(self):
        valid_subsets = [
            {},
            {
                ("code_reader", "read_file"): ModelOperationContract(
                    {
                        "path": ModelArgumentContract(
                            "string",
                            required=True,
                            min_length=2,
                            max_length=100,
                        )
                    }
                )
            },
            {
                ("code_reader", "read_file"): ModelOperationContract(
                    {
                        "path": ModelArgumentContract(
                            "string",
                            required=True,
                            min_length=1,
                            max_length=512,
                        ),
                        "max_lines": ModelArgumentContract(
                            "integer",
                            required=True,
                            min_value=10,
                            max_value=100,
                        ),
                    }
                )
            },
        ]
        for catalog in valid_subsets:
            with self.subTest(catalog=catalog):
                ModelPlanAdapter(catalog)

        broadened = [
            {
                ("unknown", "read"): ModelOperationContract(),
            },
            {
                ("test_runner", "run_tests"): ModelOperationContract(
                    {"command": ModelArgumentContract("string")}
                )
            },
            {
                ("test_runner", "run_tests"): ModelOperationContract(
                    {"approval_token": ModelArgumentContract("string")}
                )
            },
            {
                ("code_reader", "read_file"): ModelOperationContract(
                    {"max_lines": ModelArgumentContract("integer")}
                )
            },
            {
                ("code_reader", "read_file"): ModelOperationContract(
                    {
                        "path": ModelArgumentContract(
                            "string", required=False, min_length=1, max_length=512
                        )
                    }
                )
            },
            {
                ("code_reader", "read_file"): ModelOperationContract(
                    {
                        "path": ModelArgumentContract(
                            "string", required=True, min_length=0, max_length=512
                        )
                    }
                )
            },
            {
                ("code_reader", "read_file"): ModelOperationContract(
                    {
                        "path": ModelArgumentContract(
                            "string", required=True, min_length=1, max_length=513
                        )
                    }
                )
            },
            {
                ("code_reader", "read_file"): ModelOperationContract(
                    {
                        "path": ModelArgumentContract(
                            "string", required=True, min_length=1, max_length=512
                        ),
                        "max_lines": ModelArgumentContract(
                            "integer", min_value=0, max_value=10_000
                        ),
                    }
                )
            },
            {
                ("code_reader", "read_file"): ModelOperationContract(
                    {
                        "path": ModelArgumentContract(
                            "string", required=True, min_length=1, max_length=512
                        ),
                        "max_lines": ModelArgumentContract(
                            "number", min_value=1, max_value=10_000
                        ),
                    }
                )
            },
        ]
        for catalog in broadened:
            with self.subTest(catalog=catalog):
                self.assert_code(
                    "invalid_model_plan",
                    lambda catalog=catalog: ModelPlanAdapter(catalog),
                )

    def test_result_refs_bindings_and_runtime_authority_are_rejected(self):
        for field_name in ("bindings", "result", "status", "approval_token"):
            bad_step = step_payload()
            bad_step[field_name] = {"SECRET_MARKER": "value"}
            self.assert_code(
                "invalid_model_plan",
                lambda bad_step=bad_step: ModelPlanDecision.from_mapping(
                    decision_payload(steps=[bad_step])
                ),
            )
        self.assert_code(
            "invalid_arguments",
            lambda: self.adapt(
                decision_payload(
                    steps=[
                        step_payload(
                            args={
                                "path": "brain/agent.py",
                                "max_lines": {"ResultRef": "SECRET_MARKER"},
                            }
                        )
                    ]
                )
            ),
        )

    def test_text_limits_controls_and_real_bool_are_enforced(self):
        cases = [
            decision_payload(goal="g" * 1001),
            decision_payload(message="m" * 2001),
            decision_payload(completed=1),
            decision_payload(goal="bad\x00SECRET_MARKER"),
            decision_payload(
                steps=[step_payload(goal="g" * 501)]
            ),
            decision_payload(
                steps=[step_payload(justification="bad\x01SECRET_MARKER")]
            ),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assert_code(
                    "invalid_model_plan",
                    lambda payload=payload: ModelPlanDecision.from_mapping(payload),
                )
        self.assert_code(
            "invalid_model_plan",
            lambda: ModelPlanStep(
                id="direct",
                tool="code_reader",
                action="read_file",
                args={"path": "brain/agent.py"},
                goal="g" * 501,
                depends_on=(),
                justification="",
            ),
        )

    def test_catalog_is_copied_and_exposed_read_only(self):
        source = {
            ("code_reader", "read_file"): ModelOperationContract(
                {
                    "path": ModelArgumentContract(
                        "string",
                        required=True,
                        min_length=1,
                        max_length=512,
                    )
                }
            )
        }
        adapter = ModelPlanAdapter(source)
        source.clear()
        self.assertIn(("code_reader", "read_file"), adapter.catalog)
        with self.assertRaises(TypeError):
            adapter.catalog[("test_runner", "run_tests")] = ModelOperationContract()

    def test_non_json_values_and_non_finite_numbers_are_rejected(self):
        values = [
            {"path": object()},
            {"path": ("brain/agent.py",)},
            {"path": {"nested": "value"}},
            {"path": math.inf},
            {1: "brain/agent.py"},
        ]
        for args in values:
            with self.subTest(args=args):
                self.assert_code(
                    "invalid_arguments",
                    lambda args=args: self.adapt(
                        decision_payload(steps=[step_payload(args=args)])
                    ),
                )

    def test_adaptation_is_pure_and_deterministic(self):
        decision = ModelPlanDecision.from_mapping(decision_payload())
        adapter = ModelPlanAdapter(SAFE_MODEL_OPERATION_CATALOG)
        with mock.patch("builtins.open", side_effect=AssertionError("filesystem")), \
             mock.patch("socket.create_connection", side_effect=AssertionError("network")), \
             mock.patch("tools.base_tool.Tool.execute", side_effect=AssertionError("tool")), \
             mock.patch("tools.action_logger.ActionLogger.log", side_effect=AssertionError("log")), \
             mock.patch("memory.memory.leer_memoria", side_effect=AssertionError("memory")):
            first = adapter.adapt(decision)
            second = adapter.adapt(decision)
        self.assertEqual(first.identity(), second.identity())
        self.assertEqual(
            tuple(step.id for step in first.execution_order()), ("inspect_1",)
        )

    def test_error_never_retains_secret_input_or_internal_exception(self):
        marker = "SECRET_MARKER"

        class ExplodingMapping(dict):
            def items(self):
                raise RuntimeError(marker)

        payload = decision_payload(steps=[step_payload(args=ExplodingMapping())])
        self.assert_code(
            "invalid_arguments",
            lambda: ModelPlanDecision.from_mapping(payload),
        )

    def test_hostile_mapping_exceptions_are_sanitized(self):
        marker = "SECRET_HOSTILE_MAPPING"

        class ExplodingDict(dict):
            def __iter__(self):
                raise RuntimeError(marker)

            def items(self):
                raise RuntimeError(marker)

        hostile_values = [
            (
                "invalid_model_plan",
                lambda: ModelPlanDecision.from_mapping(
                    ExplodingDict(decision_payload())
                ),
            ),
            (
                "invalid_model_plan",
                lambda: ModelPlanDecision.from_mapping(
                    decision_payload(steps=ExplodingDict())
                ),
            ),
            (
                "invalid_model_plan",
                lambda: ModelPlanDecision.from_mapping(
                    decision_payload(steps=[ExplodingDict(step_payload())])
                ),
            ),
            (
                "invalid_arguments",
                lambda: ModelPlanDecision.from_mapping(
                    decision_payload(steps=[step_payload(args=ExplodingDict())])
                ),
            ),
        ]
        for code, operation in hostile_values:
            with self.subTest(code=code, operation=operation):
                self.assert_code(code, operation)

        try:
            ModelOperationContract(ExplodingDict())
        except BaseException as error:
            self.assertIs(type(error), ModelPlanAdaptationError)
            self.assertEqual(error.code, "invalid_arguments")
            self.assertNotIn(marker, str(error))
            self.assertNotIn(marker, repr(error))
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
        else:
            self.fail("Un mapping hostil de contrato debe rechazarse")

        try:
            ModelPlanAdapter(ExplodingDict())
        except BaseException as error:
            self.assertIs(type(error), ModelPlanAdaptationError)
            self.assertEqual(error.code, "invalid_model_plan")
            self.assertNotIn(marker, str(error))
            self.assertNotIn(marker, repr(error))
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
        else:
            self.fail("Un catálogo hostil debe rechazarse")

    def test_external_mappingproxy_is_rejected_without_traversing_wrapped_dict(self):
        marker = "SECRET_MAPPINGPROXY_ITEMS"

        class HostileDict(dict):
            calls = 0

            def items(self):
                type(self).calls += 1
                raise RuntimeError(marker)

        contract_proxy = MappingProxyType(HostileDict())
        self.assert_code(
            "invalid_arguments",
            lambda: ModelOperationContract(contract_proxy),
        )
        self.assertEqual(HostileDict.calls, 0)

        catalog_proxy = MappingProxyType(HostileDict())
        self.assert_code(
            "invalid_model_plan",
            lambda: ModelPlanAdapter(catalog_proxy),
        )
        self.assertEqual(HostileDict.calls, 0)

    def test_contract_and_catalog_validation_use_closed_public_errors(self):
        invalid_contracts = [
            lambda: ModelPlanLimits(max_steps=0),
            lambda: ModelArgumentContract("unknown"),
            lambda: ModelArgumentContract("string", required=1),
            lambda: ModelArgumentContract("string", min_length=-1),
            lambda: ModelArgumentContract("integer", min_value=2, max_value=1),
            lambda: ModelOperationContract({"path": object()}),
        ]
        for operation in invalid_contracts:
            with self.subTest(operation=operation):
                expected = (
                    "invalid_model_plan"
                    if operation is invalid_contracts[0]
                    else "invalid_arguments"
                )
                self.assert_code(expected, operation)

        invalid_catalogs = [
            None,
            [],
            {("unknown", "read"): ModelOperationContract()},
            {("test_runner", "run_tests"): object()},
            {("test_runner",): ModelOperationContract()},
        ]
        for catalog in invalid_catalogs:
            with self.subTest(catalog=catalog):
                self.assert_code(
                    "invalid_model_plan",
                    lambda catalog=catalog: ModelPlanAdapter(catalog),
                )

        # The one internal proxy remains valid, as do exact-dict restrictions.
        ModelPlanAdapter(SAFE_MODEL_OPERATION_CATALOG)
        ModelPlanAdapter({})

    def test_custom_limits_are_applied_without_mutating_decision(self):
        decision = ModelPlanDecision.from_mapping(decision_payload())
        adapter = ModelPlanAdapter(
            SAFE_MODEL_OPERATION_CATALOG,
            limits=ModelPlanLimits(max_steps=1),
        )
        self.assertEqual(adapter.adapt(decision).steps[0].id, "inspect_1")
        self.assertEqual(decision.steps[0].id, "inspect_1")

    def test_output_schema_is_exact_closed_and_bounded(self):
        schema = MODEL_PLAN_OUTPUT_SCHEMA.to_openai_schema()
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), {
            "schema_version", "goal", "completed", "steps", "message",
        })
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertEqual(
            schema["properties"]["schema_version"]["enum"], ["1"]
        )
        self.assertEqual(schema["properties"]["goal"]["maxLength"], 1000)
        self.assertEqual(schema["properties"]["message"]["maxLength"], 2000)

        steps = schema["properties"]["steps"]
        self.assertEqual((steps["minItems"], steps["maxItems"]), (0, 8))
        step = steps["items"]
        self.assertFalse(step["additionalProperties"])
        self.assertEqual(set(step["required"]), set(step["properties"]))
        self.assertEqual(
            set(step["properties"]),
            {
                "id", "tool", "action", "args", "goal",
                "depends_on", "justification",
            },
        )
        self.assertEqual(
            (
                step["properties"]["id"]["minLength"],
                step["properties"]["id"]["maxLength"],
            ),
            (1, 64),
        )
        self.assertEqual(step["properties"]["goal"]["maxLength"], 500)
        self.assertEqual(
            step["properties"]["justification"]["maxLength"], 500
        )
        self.assertEqual(
            step["properties"]["depends_on"]["maxItems"], 8
        )
        args = step["properties"]["args"]
        self.assertFalse(args["additionalProperties"])
        self.assertEqual(args["required"], [])
        self.assertEqual(
            set(args["properties"]), {"path", "max_lines", "new_content"}
        )
        self.assertEqual(
            set(step["properties"]["tool"]["enum"]),
            {tool for tool, _ in SAFE_MODEL_OPERATION_CATALOG},
        )
        self.assertEqual(
            set(step["properties"]["action"]["enum"]),
            {action for _, action in SAFE_MODEL_OPERATION_CATALOG},
        )
        self.assertEqual(
            set(args["properties"]),
            {
                name
                for contract in SAFE_MODEL_OPERATION_CATALOG.values()
                for name in contract.arguments
            },
        )
        self.assertEqual(
            (
                args["properties"]["path"]["minLength"],
                args["properties"]["path"]["maxLength"],
            ),
            (1, 512),
        )
        self.assertEqual(
            args["properties"]["new_content"]["maxLength"], 256 * 1024
        )

    def test_output_schema_supports_base_catalog_but_not_semantic_bypass(self):
        schema = MODEL_PLAN_OUTPUT_SCHEMA
        examples = [
            step_payload(),
            step_payload(
                tool="code_analyzer",
                action="summarize",
                args={"path": "brain/agent.py"},
            ),
            step_payload(
                tool="patch_generator",
                action="generate_patch",
                args={"path": "brain/agent.py", "new_content": "pass\n"},
            ),
            step_payload(
                tool="test_runner", action="run_tests", args={}
            ),
            step_payload(tool="git_tools", action="status", args={}),
        ]
        for model_step in examples:
            with self.subTest(model_step=model_step):
                payload = decision_payload(steps=[model_step])
                schema.validate(payload)
                self.adapt(payload)

        mismatched = decision_payload(
            steps=[
                step_payload(
                    tool="code_reader",
                    action="status",
                    args={"path": "brain/agent.py"},
                )
            ]
        )
        schema.validate(mismatched)
        self.assert_code(
            "unknown_action",
            lambda: self.adapt(mismatched),
        )
        with self.assertRaises(TypeError):
            schema.schema["properties"] = {}


if __name__ == "__main__":
    unittest.main()
