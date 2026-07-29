import tempfile
import unittest
import inspect
from pathlib import Path
from unittest import mock

from brain.agent import DeveloperAgent
from brain.approval_controller import ApprovalRequiredError
from brain.model_plan import ModelPlanDecision, SAFE_MODEL_OPERATION_CATALOG, ModelPlanAdapter
from brain.model_planning_service import (
    ModelPlanningResult,
    ModelPlanningServiceError,
)
from brain.local_model_client import ModelResponseMetadata
from memory.memory import leer_memoria
from tools.tool_result import ToolResult, UNHANDLED


class DeveloperAgentTests(unittest.TestCase):
    def test_memory_note_is_saved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            memory_file = temp_dir / "memory.json"
            agent = DeveloperAgent(
                client=None,
                memory_file=memory_file,
                prompt_dir=Path("prompts"),
                action_log_file=temp_dir / "agent_actions.json",
            )

            response = agent.handle_memory("Recuerda que estoy construyendo un agente modular")

            self.assertIn("Lo recordaré", response)
            self.assertIn(
                "estoy construyendo un agente modular",
                leer_memoria(memory_file=memory_file)["notas"],
            )

    def test_execute_tool_preserves_raw_compatibility_and_supports_structured_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            agent = DeveloperAgent(
                client=None,
                memory_file=temp_dir / "memory.json",
                prompt_dir=Path("prompts"),
                action_log_file=temp_dir / "agent_actions.json",
            )

            raw = agent.execute_tool("code_reader", lambda: {"value": 1})
            structured = agent.execute_tool(
                "code_reader",
                lambda: {"value": 1},
                structured=True,
            )

            self.assertEqual(raw, {"value": 1})
            self.assertIsInstance(structured, ToolResult)
            self.assertEqual(structured.data, {"value": 1})

    def test_agent_presents_tool_result_message_instead_of_dataclass_repr(self):
        result = ToolResult.failure(
            "demo",
            error="technical",
            message="Mensaje legible",
        )

        self.assertEqual(
            DeveloperAgent._present_tool_result(result),
            "Mensaje legible\n\nError: technical",
        )

    def test_agent_presentation_combines_or_deduplicates_message_and_error(self):
        self.assertEqual(
            DeveloperAgent._present_tool_result(
                ToolResult.failure("demo", message="friendly", error="technical")
            ),
            "friendly\n\nError: technical",
        )
        self.assertEqual(
            DeveloperAgent._present_tool_result(
                ToolResult.failure("demo", message="same", error="same")
            ),
            "same",
        )
        self.assertEqual(
            DeveloperAgent._present_tool_result(
                ToolResult.failure("demo", error="only error")
            ),
            "only error",
        )
        self.assertEqual(
            DeveloperAgent._present_tool_result(
                ToolResult.success("demo", data={"value": 1})
            ),
            {"value": 1},
        )

    def test_approval_propagates_from_execute_tool_in_both_modes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            agent = DeveloperAgent(
                client=None,
                memory_file=temp_dir / "memory.json",
                prompt_dir=Path("prompts"),
                action_log_file=temp_dir / "agent_actions.json",
            )

            for structured in (False, True):
                with self.subTest(structured=structured):
                    with self.assertRaises(ApprovalRequiredError):
                        agent.execute_tool(
                            "patch_applier",
                            lambda: "must not run",
                            action_name="apply_patch",
                            important_args={"path": "safe.py"},
                            structured=structured,
                        )

    @staticmethod
    def planning_result():
        decision = ModelPlanDecision.from_mapping({
            "schema_version": "1",
            "goal": "Inspect",
            "completed": False,
            "steps": [{
                "id": "inspect_1",
                "tool": "code_reader",
                "action": "read_file",
                "args": {"path": "brain/agent.py"},
                "goal": "Inspect",
                "depends_on": [],
                "justification": "Needed",
            }],
            "message": "",
        })
        workflow = ModelPlanAdapter(
            SAFE_MODEL_OPERATION_CATALOG
        ).adapt(decision)
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
        return ModelPlanningResult(decision, workflow, metadata)

    def test_model_planning_dependency_is_keyword_only_and_positional_compatible(self):
        parameter = inspect.signature(
            DeveloperAgent.__init__
        ).parameters["model_planning_service"]
        self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        review_parameter = inspect.signature(
            DeveloperAgent.__init__
        ).parameters["model_plan_review_controller"]
        self.assertIs(review_parameter.kind, inspect.Parameter.KEYWORD_ONLY)

        with tempfile.TemporaryDirectory() as tmpdir:
            agent = DeveloperAgent(
                None,
                Path(tmpdir) / "memory.json",
                "prompts",
                tmpdir,
                Path(tmpdir) / "actions.json",
            )
        self.assertIsNone(agent.model_planning_service)

    def test_plan_with_model_without_service_is_closed_and_sanitized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = DeveloperAgent(
                None,
                base_dir=tmpdir,
                action_log_file=Path(tmpdir) / "actions.json",
            )
            with self.assertRaises(ModelPlanningServiceError) as caught:
                agent.plan_with_model("SECRET_REQUEST")
        self.assertEqual(caught.exception.code, "service_unavailable")
        self.assertNotIn("SECRET_REQUEST", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_plan_with_model_delegates_only_and_preserves_result_and_history(self):
        result = self.planning_result()
        service = mock.Mock()
        service.plan.return_value = result
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = DeveloperAgent(
                None,
                base_dir=tmpdir,
                action_log_file=Path(tmpdir) / "actions.json",
                model_planning_service=service,
            )
            history = list(agent.history)
            with mock.patch.object(agent.planner, "plan") as planner, \
                 mock.patch.object(agent.tool_router, "dispatch") as router, \
                 mock.patch.object(agent.execution_engine, "run") as legacy, \
                 mock.patch.object(agent.execution_engine, "run_workflow") as workflow, \
                 mock.patch.object(agent.action_logger, "log") as logger, \
                 mock.patch.object(agent.permission_manager, "can_execute") as permission, \
                 mock.patch.object(agent, "execute_tool") as execute_tool:
                returned = agent.plan_with_model("Inspect")

        self.assertIs(returned, result)
        service.plan.assert_called_once_with("Inspect")
        self.assertEqual(agent.history, history)
        planner.assert_not_called()
        router.assert_not_called()
        legacy.assert_not_called()
        workflow.assert_not_called()
        logger.assert_called_once()
        logged = logger.call_args.kwargs["params"]
        self.assertEqual(logged["event"], "model_plan_generated")
        self.assertNotIn("args", logged)
        permission.assert_not_called()
        execute_tool.assert_not_called()
        self.assertIs(
            agent.model_plan_review_controller._pending.result,
            result,
        )

    def test_plan_with_model_propagates_service_error(self):
        service = mock.Mock()
        service.plan.side_effect = ModelPlanningServiceError(
            "invalid_model_response"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = DeveloperAgent(
                None,
                base_dir=tmpdir,
                action_log_file=Path(tmpdir) / "actions.json",
                model_planning_service=service,
            )
            previous = self.planning_result()
            agent.model_plan_review_controller.register(previous)
            with self.assertRaises(ModelPlanningServiceError) as caught:
                agent.plan_with_model("Plan")
        self.assertEqual(caught.exception.code, "invalid_model_response")
        self.assertIs(
            agent.model_plan_review_controller._pending.result,
            previous,
        )

    def test_respond_keeps_historical_planner_router_and_client_path(self):
        response = mock.Mock()
        response.choices = [mock.Mock(message=mock.Mock(content="legacy"))]
        client = mock.Mock()
        client.chat.completions.create.return_value = response
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = DeveloperAgent(
                client,
                base_dir=tmpdir,
                action_log_file=Path(tmpdir) / "actions.json",
                model_planning_service=mock.Mock(),
            )
            with mock.patch.object(
                agent, "handle_memory", return_value=None
            ), mock.patch.object(
                agent.planner, "plan", return_value=["default"]
            ) as planner, mock.patch.object(
                agent.tool_router, "dispatch", return_value=UNHANDLED
            ) as router, mock.patch.object(
                agent, "_looks_like_complex_task", return_value=False
            ):
                output = agent.respond("ordinary request")

        self.assertEqual(output, "legacy")
        planner.assert_called_once_with("ordinary request")
        router.assert_called_once_with(["default"], "ordinary request")
        agent.model_planning_service.plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
