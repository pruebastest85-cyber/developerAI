import json
from pathlib import Path

from brain.approval_controller import ApprovalRequiredError
from brain.controlled_programming_session import ControlledProgrammingSession
from memory.memory import agregar_recuerdo, leer_memoria
from tools.action_logger import ActionLogger
from tools.code_analyzer import CodeAnalyzer
from tools.code_reader import CodeReader
from tools.file_creator import FileCreator
from tools.git_tools import GitTools
from tools.patch_applier import PatchApplier
from tools.patch_generator import PatchGenerator
from tools.project_scanner import buscar_en_indice
from tools.test_runner import TestRunner
from tools.tool_result import (
    UNHANDLED,
    execute_and_normalize,
    legacy_tool_value,
    present_tool_result,
)
from brain.context_manager import ContextManager
from brain.execution_engine import ExecutionEngine
from brain.memory_manager import MemoryManager
from brain.permission_manager import PermissionManager
from brain.planner import Planner
from brain.patch_request import build_patch_approval_args, parse_patch_command
from brain.tool_router import ToolRouter
from tools.registry import build_default_registry


class DeveloperAgent:
    def __init__(
        self,
        client,
        memory_file=None,
        prompt_dir=None,
        base_dir=None,
        action_log_file=None,
        *,
        model_planning_service=None,
        model_plan_review_controller=None,
    ):
        self.client = client
        self.model_planning_service = model_planning_service
        self.memory_file = memory_file
        self.prompt_dir = Path(prompt_dir or "prompts")
        self.base_dir = Path(base_dir or ".").resolve()
        self.system_prompt_path = self.prompt_dir / "system.txt"
        self.project_context_path = self.prompt_dir / "project_context.txt"
        self.settings_path = self.base_dir / "config" / "settings.json"
        self.history = []
        self.code_reader = CodeReader(base_dir=self.base_dir)
        self.code_analyzer = CodeAnalyzer(base_dir=self.base_dir)
        self.patch_generator = PatchGenerator(base_dir=self.base_dir)
        self.patch_applier = PatchApplier(base_dir=self.base_dir)
        self.file_creator = FileCreator(base_dir=self.base_dir)
        self.test_runner = TestRunner(base_dir=self.base_dir)
        self.git_tools = GitTools(base_dir=self.base_dir)
        self.action_logger = ActionLogger(log_file=action_log_file)
        self.context_manager = ContextManager(base_dir=self.base_dir)
        self.planner = Planner()
        self.memory_manager = MemoryManager(memory_file=self.memory_file)
        self.registry = build_default_registry()
        self.registry.register("file_creator", "Crea archivos nuevos de forma segura", True, tool_instance=self.file_creator, risk="high")
        self.permission_manager = PermissionManager(
            registry=self.registry,
            medium_requires_confirmation=self._read_medium_risk_policy(),
            fail_closed=True,
        )
        self.tool_router = ToolRouter(self)
        self.execution_engine = ExecutionEngine(self)
        if model_plan_review_controller is None:
            from brain.model_plan_review import ModelPlanReviewController

            model_plan_review_controller = ModelPlanReviewController(self)
        self.model_plan_review_controller = model_plan_review_controller
        self._programming_session = ControlledProgrammingSession(self)
        self._initialize_history()

    def _read_medium_risk_policy(self):
        if not self.settings_path.is_file():
            return True
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return True
        value = data.get("medium_risk_requires_confirmation")
        if not isinstance(value, bool):
            return True
        return value

    def create_operation_approval_request(
        self,
        tool_name,
        action_name,
        important_args=None,
        force=False,
    ):
        return self.permission_manager.create_approval_request(
            tool_name,
            action_name,
            important_args=important_args,
            force=force,
        )

    def execute_tool(
        self,
        tool_name,
        action,
        action_name="execute",
        important_args=None,
        approval_token=None,
        structured=False,
        none_policy="ok",
        operational_exceptions=(),
        retryable=False,
        require_approval=False,
    ):
        allowed = self.permission_manager.can_execute(
            tool_name,
            action_name=action_name,
            important_args=important_args,
            approval_token=approval_token,
            require_confirmation=require_approval,
        )

        if not allowed:
            message = self.permission_manager.explain(
                tool_name,
                action_name=action_name,
                force=require_approval,
            )
            if self.permission_manager.is_confirmation_required(
                tool_name,
                action_name=action_name,
                force=require_approval,
            ):
                raise ApprovalRequiredError(
                    tool_name=tool_name,
                    action_name=action_name,
                    important_args=important_args or {},
                    execute=action,
                    message=message,
                    force_approval=require_approval,
                )
            raise PermissionError(message)
        result = execute_and_normalize(
            tool_name,
            action,
            none_policy=none_policy,
            operational_exceptions=operational_exceptions,
            retryable=retryable,
        )
        return result if structured else legacy_tool_value(result)

    def _initialize_history(self):
        self.history = [
            {
                "role": "system",
                "content": self._read_system_prompt(),
            }
        ]

    def _read_system_prompt(self):
        if self.system_prompt_path.exists():
            return self.system_prompt_path.read_text(encoding="utf-8").strip()
        return "Eres DeveloperAI, un asistente local útil y práctico."

    def _read_project_context(self):
        if self.project_context_path.exists():
            return self.project_context_path.read_text(encoding="utf-8").strip()
        return "Proyecto actual sin contexto adicional."

    def handle_memory(self, message):
        text = message.strip()

        if text.lower().startswith("recuerda"):
            detail = text[8:].strip()
            if detail.lower().startswith("que "):
                detail = detail[4:].strip()
            if detail:
                try:
                    stored = self.execute_tool(
                        "memory",
                        lambda: self.memory_manager.store(detail),
                        action_name="store",
                        important_args={"detail": detail[:120]},
                    )
                except ApprovalRequiredError:
                    raise
                except PermissionError as exc:
                    return str(exc)
                if stored is None:
                    return "No guardé ese recuerdo porque no parecía lo suficientemente relevante."
                return f"Lo recordaré: {detail}"
            return "No pude guardar ese recuerdo. Prueba con algo más concreto."

        if text.lower() in {"¿qué estoy creando?", "que estoy creando?", "¿qué estoy haciendo?", "que estoy haciendo?"}:
            data = leer_memoria(memory_file=self.memory_file)
            notes = data.get("notas", [])
            if notes:
                return "Hasta ahora tienes registrados: " + "; ".join(notes)
            return "Aún no tengo recuerdos guardados sobre eso."

        if "dónde está" in text.lower() or "donde esta" in text.lower() or "dónde se encuentra" in text.lower() or "donde se encuentra" in text.lower():
            try:
                results = self.execute_tool(
                    "project_scanner",
                    lambda: buscar_en_indice(text),
                    action_name="search_index",
                    important_args={"query": text[:120]},
                )
            except ApprovalRequiredError:
                raise
            except PermissionError as exc:
                return str(exc)
            if results:
                parts = []
                for name, data in results[:5]:
                    parts.append(f"- {name}: {data.get('ruta', '/')}" )
                return "Encontré coincidencias:\n" + "\n".join(parts)
            return "No encontré una coincidencia clara en el índice del proyecto."

        if text.lower().startswith("explícame ") or text.lower().startswith("explicame "):
            target = text.split(maxsplit=1)[1].strip()
            try:
                content = self.execute_tool(
                    "code_reader",
                    lambda: self.code_reader.read_file_with_limit(target),
                    action_name="read_file",
                    important_args={"target": target},
                )
                return f"Contenido de {target}:\n\n" + content
            except ApprovalRequiredError:
                raise
            except (FileNotFoundError, ValueError, PermissionError) as exc:
                return str(exc)

        if text.lower().startswith("analiza "):
            target = text.split(maxsplit=1)[1].strip()
            try:
                return self.execute_tool(
                    "code_analyzer",
                    lambda: self.code_analyzer.summarize(target),
                    action_name="summarize",
                    important_args={"target": target},
                )
            except ApprovalRequiredError:
                raise
            except (FileNotFoundError, ValueError, SyntaxError, PermissionError) as exc:
                return str(exc)

        if text.lower().startswith("propón cambio ") or text.lower().startswith("propone cambio "):
            try:
                target = text.split(maxsplit=2)[2].strip()
            except IndexError:
                return "Formato esperado: 'Propón cambio <archivo> <nuevo contenido>'"

            parts = target.split(" | ", 1)
            if len(parts) != 2:
                return "Formato esperado: 'Propón cambio <archivo> | <nuevo contenido>'"

            relative_path, new_content = parts
            try:
                patch = self.execute_tool(
                    "patch_generator",
                    lambda: self.patch_generator.generate_patch_from_file(relative_path, new_content),
                    action_name="generate_patch",
                    important_args={"path": relative_path},
                )
                if patch:
                    return "Propuesta de cambio:\n\n" + patch
                return "No se generó un parche."
            except ApprovalRequiredError:
                raise
            except (FileNotFoundError, ValueError, PermissionError) as exc:
                return str(exc)

        if text.lower().startswith("prueba") or text.lower().startswith("ejecuta tests"):
            try:
                result = self.execute_tool(
                    "test_runner",
                    lambda: self.test_runner.execute(structured=True),
                    action_name="run_tests",
                    important_args={"scope": "default"},
                    structured=True,
                )
                return self._present_tool_result(result)
            except ApprovalRequiredError:
                raise
            except PermissionError as exc:
                return str(exc)

        if text.lower().startswith("git status"):
            try:
                return self.execute_tool(
                    "git_tools",
                    lambda: self._format_git_result(self.git_tools.status()),
                    action_name="status",
                    important_args={"command": "git status --short"},
                )
            except ApprovalRequiredError:
                raise
            except PermissionError as exc:
                return str(exc)

        if text.lower().startswith("checkpoint"):
            try:
                return self.execute_tool(
                    "git_tools",
                    lambda: self._format_git_result(self.git_tools.checkpoint()),
                    action_name="checkpoint",
                    important_args={"message": "Checkpoint before AI modification"},
                )
            except ApprovalRequiredError:
                raise
            except PermissionError as exc:
                return str(exc)

        if text.lower().startswith("rollback"):
            try:
                return self.execute_tool(
                    "git_tools",
                    lambda: self._format_git_result(self.git_tools.rollback()),
                    action_name="rollback",
                    important_args={"target": "HEAD"},
                )
            except ApprovalRequiredError:
                raise
            except PermissionError as exc:
                return str(exc)

        if text.lower().startswith("aplica cambio ") or text.lower().startswith("aplica el cambio "):
            try:
                parsed = parse_patch_command(message)
            except ValueError:
                return "Formato esperado: 'Aplica cambio <archivo> | <contenido nuevo> | <contenido anterior>'"

            if parsed is None:
                return None

            relative_path, new_content, old_content = parsed
            try:
                approval_args = build_patch_approval_args(relative_path, old_content, new_content)
                result = self.execute_tool(
                    "patch_applier",
                    lambda: self.patch_applier.apply_patch(relative_path, old_content, new_content),
                    action_name="apply_patch",
                    important_args=approval_args,
                )
                test_report = self.execute_tool(
                    "test_runner",
                    lambda: self.test_runner.run_tests_report(),
                    action_name="run_tests_report",
                    important_args={"scope": "default"},
                )
                return "Cambio aplicado correctamente.\n\n" + str(result) + "\n\n" + test_report
            except ApprovalRequiredError:
                raise
            except (FileNotFoundError, ValueError, PermissionError) as exc:
                return str(exc)

        return None

    def build_messages(self, message):
        self.history.append({"role": "user", "content": message})

        messages = list(self.history)
        context_text = self.context_manager.build_context(
            message,
            memory_file=self.memory_file,
            project_context=self._read_project_context(),
            history=self.history,
        )
        messages.append({"role": "system", "content": context_text})
        return messages

    def _looks_like_complex_task(self, message):
        text = message.lower().strip()
        return any(keyword in text for keyword in ["falla", "error", "fallo", "bug", "debug", "analiza el proyecto", "por qué falla", "qué falla"])

    def _format_git_result(self, result):
        parts = [f"Comando: {result['command']}", f"Código: {result['returncode']}"]
        if result["stdout"].strip():
            parts.append("Salida:\n" + result["stdout"].strip())
        if result["stderr"].strip():
            parts.append("Errores:\n" + result["stderr"].strip())
        return "\n\n".join(parts)

    @staticmethod
    def _present_tool_result(result):
        return present_tool_result(result)

    def plan_with_model(self, user_request):
        if self.model_planning_service is None:
            from brain.model_planning_service import ModelPlanningServiceError

            raise ModelPlanningServiceError("service_unavailable")
        result = self.model_planning_service.plan(user_request)
        self.model_plan_review_controller.register(result)
        return result

    def get_pending_model_plan(self):
        return self.model_plan_review_controller.get_pending()

    def render_pending_model_plan(self):
        return self.model_plan_review_controller.render_pending()

    def approve_model_plan(self, plan_id):
        return self.model_plan_review_controller.approve(plan_id)

    def reject_model_plan(self, plan_id):
        return self.model_plan_review_controller.reject(plan_id)

    def cancel_model_plan(self, plan_id):
        return self.model_plan_review_controller.cancel(plan_id)

    def get_programming_session(self):
        """Obtiene la sesión de programación controlada."""
        return self._programming_session

    def respond(self, message):
        if ControlledProgrammingSession.is_controlled_message(message):
            return self._programming_session.handle_message(message)

        memory_response = self.handle_memory(message)
        if memory_response is None:
            memory_response = UNHANDLED
        if memory_response is not UNHANDLED:
            return memory_response

        plan = self.planner.plan(message)

        if self._looks_like_complex_task(message):
            result = self.execution_engine.run(message)
            self.action_logger.log("execution_engine", params={"message": message}, result=result)
            return str(result)

        routed = self.tool_router.dispatch(plan, message)
        if routed is not UNHANDLED:
            self.action_logger.log("router", params={"plan": plan, "message": message}, result="executed")
            return routed

        messages = self.build_messages(message)
        if self.client is None:
            return "No hay cliente de modelo conectado."

        response = self.client.chat.completions.create(
            model="qwen3.6-35b-a3b",
            messages=messages,
        )
        text = response.choices[0].message.content
        self.history.append({"role": "assistant", "content": text})
        return text
