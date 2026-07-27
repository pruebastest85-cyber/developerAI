from pathlib import Path

from memory.memory import agregar_recuerdo, leer_memoria
from tools.action_logger import ActionLogger
from tools.code_analyzer import CodeAnalyzer
from tools.code_reader import CodeReader
from tools.git_tools import GitTools
from tools.patch_applier import PatchApplier
from tools.patch_generator import PatchGenerator
from tools.project_scanner import buscar_en_indice
from tools.test_runner import TestRunner
from brain.context_manager import ContextManager
from brain.execution_engine import ExecutionEngine
from brain.memory_manager import MemoryManager
from brain.permission_manager import PermissionManager
from brain.planner import Planner
from brain.tool_router import ToolRouter


class DeveloperAgent:
    def __init__(self, client, memory_file=None, prompt_dir=None, base_dir=None):
        self.client = client
        self.memory_file = memory_file
        self.prompt_dir = Path(prompt_dir or "prompts")
        self.base_dir = Path(base_dir or ".").resolve()
        self.system_prompt_path = self.prompt_dir / "system.txt"
        self.project_context_path = self.prompt_dir / "project_context.txt"
        self.history = []
        self.code_reader = CodeReader(base_dir=self.base_dir)
        self.code_analyzer = CodeAnalyzer(base_dir=self.base_dir)
        self.patch_generator = PatchGenerator(base_dir=self.base_dir)
        self.patch_applier = PatchApplier(base_dir=self.base_dir)
        self.test_runner = TestRunner(base_dir=self.base_dir)
        self.git_tools = GitTools(base_dir=self.base_dir)
        self.action_logger = ActionLogger()
        self.context_manager = ContextManager(base_dir=self.base_dir)
        self.planner = Planner()
        self.memory_manager = MemoryManager(memory_file=self.memory_file)
        self.permission_manager = PermissionManager(registry=None)
        self.tool_router = ToolRouter(self)
        self.execution_engine = ExecutionEngine(self)
        self._initialize_history()

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
                stored = self.memory_manager.store(detail)
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
            results = buscar_en_indice(text)
            if results:
                parts = []
                for name, data in results[:5]:
                    parts.append(f"- {name}: {data.get('ruta', '/')}" )
                return "Encontré coincidencias:\n" + "\n".join(parts)
            return "No encontré una coincidencia clara en el índice del proyecto."

        if text.lower().startswith("explícame ") or text.lower().startswith("explicame "):
            target = text.split(maxsplit=1)[1].strip()
            try:
                content = self.code_reader.read_file_with_limit(target)
                return f"Contenido de {target}:\n\n" + content
            except (FileNotFoundError, ValueError) as exc:
                return str(exc)

        if text.lower().startswith("analiza "):
            target = text.split(maxsplit=1)[1].strip()
            try:
                return self.code_analyzer.summarize(target)
            except (FileNotFoundError, ValueError, SyntaxError) as exc:
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
                patch = self.patch_generator.generate_patch_from_file(relative_path, new_content)
                if patch:
                    return "Propuesta de cambio:\n\n" + patch
                return "No se generó un parche."
            except (FileNotFoundError, ValueError) as exc:
                return str(exc)

        if text.lower().startswith("prueba") or text.lower().startswith("ejecuta tests"):
            return self.test_runner.run_tests_report()

        if text.lower().startswith("git status"):
            return self._format_git_result(self.git_tools.status())

        if text.lower().startswith("checkpoint"):
            return self._format_git_result(self.git_tools.checkpoint())

        if text.lower().startswith("rollback"):
            return self._format_git_result(self.git_tools.rollback())

        if text.lower().startswith("aplica cambio ") or text.lower().startswith("aplica el cambio "):
            try:
                target = text.split(maxsplit=2)[2].strip()
            except IndexError:
                return "Formato esperado: 'Aplica cambio <archivo> | <contenido nuevo> | <contenido anterior>'"

            parts = target.split(" | ", 2)
            if len(parts) != 3:
                return "Formato esperado: 'Aplica cambio <archivo> | <contenido nuevo> | <contenido anterior>'"

            relative_path, new_content, old_content = parts
            try:
                result = self.patch_applier.apply_patch(relative_path, old_content, new_content)
                test_report = self.test_runner.run_tests_report()
                return "Cambio aplicado correctamente.\n\n" + str(result) + "\n\n" + test_report
            except (FileNotFoundError, ValueError) as exc:
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

    def respond(self, message):
        memory_response = self.handle_memory(message)
        if memory_response is not None:
            return memory_response

        plan = self.planner.plan(message)
        if plan:
            for tool_name in plan:
                if tool_name == "internet_search":
                    continue
                if not self.permission_manager.can_execute(tool_name, user_confirmation=False):
                    return self.permission_manager.explain(tool_name, user_confirmation=False)

        if self._looks_like_complex_task(message):
            result = self.execution_engine.run(message)
            self.action_logger.log("execution_engine", params={"message": message}, result=result)
            return str(result)

        routed = self.tool_router.dispatch(plan, message)
        if routed is not None:
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
