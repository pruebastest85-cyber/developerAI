import json
import unittest
from unittest import mock

from brain.local_model_client import (
    LocalModelClient,
    ModelMessage,
    ModelResponseMetadata,
    StructuredModelRequest,
    StructuredModelResponse,
)
from brain.local_model_config import LocalModelConfig
from brain.model_errors import (
    InvalidJsonError,
    ModelConnectionError,
    SchemaValidationError,
)
from brain.model_plan import (
    MODEL_PLAN_OUTPUT_SCHEMA,
    SAFE_MODEL_OPERATION_CATALOG,
    ModelArgumentContract,
    ModelOperationContract,
    ModelPlanAdaptationError,
    ModelPlanAdapter,
    ModelPlanDecision,
)
from brain.model_planning_service import (
    MAX_MODEL_PLANNING_REQUEST_BYTES,
    MODEL_PLANNING_SYSTEM_PROMPT,
    ModelPlanningResult,
    ModelPlanningService,
    ModelPlanningServiceError,
)
from brain.model_transport import TransportResponse
from brain.workflow_plan import WorkflowPlan


def step(**changes):
    value = {
        "id": "inspect_1",
        "tool": "code_reader",
        "action": "read_file",
        "args": {"path": "brain/agent.py", "max_lines": 100},
        "goal": "Inspect",
        "depends_on": [],
        "justification": "Needed",
    }
    value.update(changes)
    return value


def decision(**changes):
    value = {
        "schema_version": "1",
        "goal": "Review safely",
        "completed": False,
        "steps": [step()],
        "message": "",
    }
    value.update(changes)
    return value


def envelope(payload):
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return json.dumps({
        "id": "req-1",
        "model": "qwen",
        "choices": [{
            "message": {"content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }).encode("utf-8")


class FakeTransport:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return TransportResponse(
            200,
            (("Content-Type", "application/json"),),
            self.body,
        )


class ModelPlanningServiceTests(unittest.TestCase):
    def make(self, payload=None, *, transport=None, adapter=None):
        transport = transport or FakeTransport(
            envelope(decision() if payload is None else payload)
        )
        client = LocalModelClient(
            LocalModelConfig(
                provider="lm_studio",
                base_url="http://localhost:1234/v1",
                model="qwen",
            ),
            transport=transport,
            clock=iter([1.0, 1.1]).__next__,
        )
        return ModelPlanningService(client, adapter=adapter), transport, client

    def make_with_prompt_limit(self, max_prompt_bytes):
        transport = FakeTransport(envelope(decision()))
        client = LocalModelClient(
            LocalModelConfig(
                provider="lm_studio",
                base_url="http://localhost:1234/v1",
                model="qwen",
                max_prompt_bytes=max_prompt_bytes,
            ),
            transport=transport,
        )
        return ModelPlanningService(client), transport

    def assert_service_code(self, code, operation, marker=None):
        with self.assertRaises(ModelPlanningServiceError) as caught:
            operation()
        error = caught.exception
        self.assertEqual(error.code, code)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        if marker:
            exposed = " ".join([
                str(error), repr(error), repr(error.args),
                repr(vars(error)),
            ])
            self.assertNotIn(marker, exposed)

    def test_valid_response_returns_canonical_immutable_result(self):
        service, transport, _ = self.make()
        result = service.plan("Inspect the agent")
        self.assertIs(type(result), ModelPlanningResult)
        self.assertIs(type(result.decision), ModelPlanDecision)
        self.assertIs(type(result.workflow), WorkflowPlan)
        self.assertIs(type(result.metadata), ModelResponseMetadata)
        self.assertEqual(result.workflow.steps[0].approval, "policy")
        self.assertTrue(result.workflow.steps[0].required)
        self.assertFalse(result.workflow.steps[0].repeat_completed)
        with self.assertRaises((AttributeError, TypeError)):
            result.workflow = WorkflowPlan(())

        sent = json.loads(transport.requests[0].body)
        self.assertEqual(sent["temperature"], 0.0)
        self.assertEqual(
            sent["response_format"]["json_schema"]["schema"],
            MODEL_PLAN_OUTPUT_SCHEMA.to_openai_schema(),
        )
        self.assertEqual(
            sent["messages"],
            [
                {"role": "system", "content": MODEL_PLANNING_SYSTEM_PROMPT},
                {"role": "user", "content": "Inspect the agent"},
            ],
        )

    def test_dependencies_and_completed_decision_are_preserved(self):
        dependent = decision(steps=[
            step(id="first"),
            step(
                id="second",
                tool="code_analyzer",
                action="summarize",
                args={"path": "brain/agent.py"},
                depends_on=["first"],
            ),
        ])
        result = self.make(dependent)[0].plan("Plan")
        self.assertEqual(
            tuple(item.id for item in result.workflow.execution_order()),
            ("first", "second"),
        )

        completed = decision(
            completed=True,
            steps=[],
            message="Already complete",
        )
        result = self.make(completed)[0].plan("Check")
        self.assertTrue(result.decision.completed)
        self.assertEqual(result.workflow.steps, ())

    def test_structural_model_failures_remain_local_model_errors(self):
        cases = [
            ("not json", InvalidJsonError),
            ([], SchemaValidationError),
            ({**decision(), "extra": True}, SchemaValidationError),
            (
                {
                    key: value
                    for key, value in decision().items()
                    if key != "message"
                },
                SchemaValidationError,
            ),
            (decision(steps=[{"id": "missing"}]), SchemaValidationError),
            ("plain text", InvalidJsonError),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload, expected=expected):
                service = self.make(payload)[0]
                with self.assertRaises(expected):
                    service.plan("Plan")

    def test_schema_rejects_authority_runtime_and_extra_arguments(self):
        forbidden_fields = [
            "approval",
            "approval_token",
            "required",
            "repeat_completed",
            "bindings",
            "runtime_metadata",
            "execute",
            "status",
        ]
        for field_name in forbidden_fields:
            with self.subTest(field_name=field_name):
                hostile_step = step()
                hostile_step[field_name] = "SECRET_AUTHORITY"
                service = self.make(
                    decision(steps=[hostile_step])
                )[0]
                with self.assertRaises(SchemaValidationError):
                    service.plan("Plan")

        extra_arg = decision(
            steps=[step(args={"path": "x", "approval_token": "SECRET"})]
        )
        with self.assertRaises(SchemaValidationError):
            self.make(extra_arg)[0].plan("Plan")

    def test_semantic_failures_use_model_plan_adapter(self):
        cases = [
            (
                "unknown_action",
                decision(steps=[
                    step(
                        tool="code_reader",
                        action="status",
                        args={"path": "x"},
                    )
                ]),
            ),
            (
                "invalid_arguments",
                decision(steps=[
                    step(
                        tool="code_reader",
                        action="read_file",
                        args={},
                    )
                ]),
            ),
            (
                "invalid_dependency",
                decision(steps=[step(depends_on=["missing"])]),
            ),
            (
                "cyclic_dependencies",
                decision(steps=[
                    step(id="first", depends_on=["second"]),
                    step(id="second", depends_on=["first"]),
                ]),
            ),
        ]
        for expected, payload in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(ModelPlanAdaptationError) as caught:
                    self.make(payload)[0].plan("Plan")
                self.assertEqual(caught.exception.code, expected)

    def test_schema_rejects_unknown_names_and_more_than_eight_steps(self):
        cases = [
            decision(steps=[step(tool="unknown")]),
            decision(steps=[step(action="unknown")]),
            decision(steps=[step(id=f"s{index}") for index in range(9)]),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(SchemaValidationError):
                    self.make(payload)[0].plan("Plan")

    def test_catalog_injection_cannot_expand_authority(self):
        with self.assertRaises(ModelPlanAdaptationError) as caught:
            ModelPlanAdapter({
                ("test_runner", "run_tests"): ModelOperationContract({
                    "command": ModelArgumentContract("string"),
                })
            })
        self.assertEqual(caught.exception.code, "invalid_model_plan")

        restricted = ModelPlanAdapter({
            ("git_tools", "status"): SAFE_MODEL_OPERATION_CATALOG[
                ("git_tools", "status")
            ]
        })
        with self.assertRaises(ModelPlanAdaptationError) as caught:
            self.make(adapter=restricted)[0].plan("Plan")
        self.assertEqual(caught.exception.code, "unknown_tool")

    def test_invalid_request_is_bounded_and_never_calls_client(self):
        service, _, client = self.make()
        invalid = [
            None,
            "",
            b"text",
            "x" * (MAX_MODEL_PLANNING_REQUEST_BYTES + 1),
            "\ud800",
        ]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                with mock.patch.object(
                    client, "complete", wraps=client.complete
                ) as complete:
                    self.assert_service_code(
                        "invalid_request",
                        lambda value=value: service.plan(value),
                    )
                    complete.assert_not_called()

        service, transport = self.make_with_prompt_limit(16)
        self.assert_service_code(
            "invalid_request",
            lambda: service.plan("x" * 17),
        )
        self.assertEqual(transport.requests, [])

    def test_total_request_budget_rejects_before_complete(self):
        service, transport = self.make_with_prompt_limit(1024)
        marker = "SECRET_TOTAL_BUDGET_" + ("x" * 512)
        self.assertLess(
            len(marker.encode("utf-8")),
            service._max_request_bytes,
        )
        with mock.patch.object(
            service._model_client,
            "complete",
            wraps=service._model_client.complete,
        ) as complete:
            self.assert_service_code(
                "invalid_request",
                lambda: service.plan(marker),
                marker=marker,
            )
        complete.assert_not_called()
        self.assertEqual(transport.requests, [])

        service, transport = self.make_with_prompt_limit(1)
        with mock.patch.object(
            service._model_client,
            "complete",
            wraps=service._model_client.complete,
        ) as complete:
            self.assert_service_code(
                "invalid_request",
                lambda: service.plan("x"),
            )
        complete.assert_not_called()
        self.assertEqual(transport.requests, [])

    def test_total_request_budget_accepts_exact_boundary(self):
        reference, transport, _ = self.make()
        reference.plan("x")
        exact_size = len(transport.requests[0].body)

        service, transport = self.make_with_prompt_limit(exact_size)
        with mock.patch.object(
            service._model_client,
            "complete",
            wraps=service._model_client.complete,
        ) as complete:
            result = service.plan("x")
        complete.assert_called_once()
        self.assertIs(type(result), ModelPlanningResult)
        self.assertEqual(len(transport.requests[0].body), exact_size)

    def test_total_request_budget_counts_utf8_bytes(self):
        reference, transport, _ = self.make()
        reference.plan("x")
        ascii_size = len(transport.requests[0].body)

        service, transport = self.make_with_prompt_limit(ascii_size + 2)
        with mock.patch.object(
            service._model_client,
            "complete",
            wraps=service._model_client.complete,
        ) as complete:
            self.assert_service_code(
                "invalid_request",
                lambda: service.plan("éé"),
            )
        complete.assert_not_called()
        self.assertEqual(transport.requests, [])

    def test_whitespace_request_is_preserved_literally(self):
        service, transport, _ = self.make()
        service.plan("   ")
        sent = json.loads(transport.requests[0].body)
        self.assertEqual(sent["messages"][1]["content"], "   ")

    def test_false_hostile_response_is_rejected_without_method_calls(self):
        calls = {
            "items": 0, "keys": 0, "values": 0, "iter": 0,
            "getitem": 0, "len": 0, "copy": 0, "str": 0, "repr": 0,
        }

        class HostileResponse:
            def items(self): calls["items"] += 1; raise RuntimeError()
            def keys(self): calls["keys"] += 1; raise RuntimeError()
            def values(self): calls["values"] += 1; raise RuntimeError()
            def __iter__(self): calls["iter"] += 1; raise RuntimeError()
            def __getitem__(self, key): calls["getitem"] += 1; raise RuntimeError()
            def __len__(self): calls["len"] += 1; raise RuntimeError()
            def copy(self): calls["copy"] += 1; raise RuntimeError()
            def __str__(self): calls["str"] += 1; raise RuntimeError()
            def __repr__(self): calls["repr"] += 1; raise RuntimeError()

        service, _, client = self.make()
        with mock.patch.object(
            client, "complete", return_value=HostileResponse()
        ):
            self.assert_service_code(
                "invalid_model_response",
                lambda: service.plan("Plan"),
            )
        self.assertEqual(calls, {name: 0 for name in calls})

    def test_internal_frozen_json_is_thawed_once_for_from_mapping(self):
        service, _, client = self.make()
        response = client.complete(StructuredModelRequest(
            messages=(
                # The transport body is the same valid plan used by service.
                # A direct request proves StructuredModelResponse owns the proxy.
                ModelMessage("user", "Plan"),
            ),
            output_schema=MODEL_PLAN_OUTPUT_SCHEMA,
        ))
        self.assertNotIsInstance(response.data, dict)
        with mock.patch.object(
            client, "complete", return_value=response
        ), mock.patch.object(
            ModelPlanDecision,
            "from_mapping",
            wraps=ModelPlanDecision.from_mapping,
        ) as from_mapping:
            result = service.plan("Plan")
        payload = from_mapping.call_args.args[0]
        self.assertIs(type(payload), dict)
        self.assertIs(type(payload["steps"]), list)
        self.assertIs(type(result.decision), ModelPlanDecision)

    def test_transport_error_remains_sanitized(self):
        marker = "SECRET_TRANSPORT"
        error = ModelConnectionError(code="connection_failed")
        service = self.make(
            transport=FakeTransport(error=error)
        )[0]
        with self.assertRaises(ModelConnectionError) as caught:
            service.plan(marker)
        exposed = " ".join([
            str(caught.exception),
            repr(caught.exception),
            repr(caught.exception.args),
            repr(vars(caught.exception)),
        ])
        self.assertNotIn(marker, exposed)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_response_must_be_exact_structured_model_response(self):
        service, _, client = self.make()

        class ResponseSubclass(StructuredModelResponse):
            pass

        valid = client.complete(StructuredModelRequest(
            messages=(
                ModelMessage("user", "Plan"),
            ),
            output_schema=MODEL_PLAN_OUTPUT_SCHEMA,
        ))
        subclass = ResponseSubclass(
            valid.to_dict()["data"],
            valid.metadata,
        )
        with mock.patch.object(client, "complete", return_value=subclass):
            self.assert_service_code(
                "invalid_model_response",
                lambda: service.plan("Plan"),
            )

    def test_service_calls_canonical_boundary_and_never_executes(self):
        service, _, _ = self.make()
        with mock.patch.object(
            ModelPlanDecision,
            "from_mapping",
            wraps=ModelPlanDecision.from_mapping,
        ) as from_mapping, mock.patch.object(
            ModelPlanAdapter,
            "adapt",
            wraps=service._adapter.adapt,
        ) as adapt, mock.patch(
            "brain.execution_engine.ExecutionEngine.run_workflow"
        ) as run_workflow, mock.patch(
            "brain.tool_router.ToolRouter.dispatch"
        ) as dispatch, mock.patch(
            "tools.base_tool.Tool.execute"
        ) as execute_tool, mock.patch(
            "tools.action_logger.ActionLogger.log"
        ) as logger, mock.patch(
            "brain.permission_manager.PermissionManager.can_execute"
        ) as permission, mock.patch(
            "brain.permission_manager.PermissionManager.grant_approval"
        ) as grant:
            result = service.plan("Plan")
        from_mapping.assert_called_once()
        adapt.assert_called_once_with(result.decision)
        run_workflow.assert_not_called()
        dispatch.assert_not_called()
        execute_tool.assert_not_called()
        logger.assert_not_called()
        permission.assert_not_called()
        grant.assert_not_called()

    def test_result_rejects_noncanonical_objects(self):
        metadata = ModelResponseMetadata(
            provider="lm_studio",
            requested_model="qwen",
            reported_model=None,
            request_id=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            finish_reason=None,
            duration_seconds=0,
            endpoint_id="lm_studio@localhost:1234",
            structured_format="json_schema",
        )
        self.assert_service_code(
            "invalid_model_response",
            lambda: ModelPlanningResult(object(), WorkflowPlan(()), metadata),
        )


if __name__ == "__main__":
    unittest.main()
