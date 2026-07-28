import hashlib

from brain.approval_controller import ApprovalRequiredError
from brain.patch_request import build_patch_approval_args, parse_patch_command
from tools.tool_result import UNHANDLED, present_tool_result


class ToolRouter:
    def __init__(self, agent):
        self.agent = agent

    def dispatch(self, plan, message):
        if not plan:
            return UNHANDLED

        if "code_analyzer" in plan:
            if message.lower().startswith(("analiza", "analizar")):
                target = message.split(maxsplit=1)[1].strip() if len(message.split()) > 1 else ""
                try:
                    return self.agent.execute_tool(
                        "code_analyzer",
                        lambda: self.agent.code_analyzer.summarize(target),
                        action_name="summarize",
                        important_args={"target": target},
                    )
                except ApprovalRequiredError:
                    raise
                except PermissionError as exc:
                    return str(exc)

        if "code_reader" in plan:
            if message.lower().startswith(("explícame", "explicame")):
                target = message.split(maxsplit=1)[1].strip() if len(message.split()) > 1 else ""
                try:
                    return self.agent.execute_tool(
                        "code_reader",
                        lambda: self.agent.code_reader.read_file_with_limit(target),
                        action_name="read_file",
                        important_args={"target": target},
                    )
                except ApprovalRequiredError:
                    raise
                except PermissionError as exc:
                    return str(exc)

        if "memory" in plan:
            memory_result = self.agent.handle_memory(message)
            return UNHANDLED if memory_result is None else memory_result

        if "test_runner" in plan:
            try:
                result = self.agent.execute_tool(
                    "test_runner",
                    lambda: self.agent.test_runner.execute(structured=True),
                    action_name="run_tests",
                    important_args={"scope": "default"},
                    structured=True,
                )
                return self._present_tool_result(result)
            except ApprovalRequiredError:
                raise
            except PermissionError as exc:
                return str(exc)

        if "file_creator" in plan:
            command = message.lstrip()
            lower_command = command.lower()
            prefix = None
            for candidate in ("crea archivo", "crear archivo"):
                if lower_command == candidate or lower_command.startswith(candidate + " "):
                    prefix = candidate
                    break

            if prefix is None:
                return UNHANDLED

            remainder = command[len(prefix):]
            if not remainder.startswith(" "):
                return "Formato esperado: 'crea archivo <ruta> | <contenido>'"

            payload = remainder[1:]
            parts = payload.split(" | ", 1)
            if len(parts) != 2:
                return "Formato esperado: 'crea archivo <ruta> | <contenido>'"

            relative_path, content = parts
            relative_path = relative_path.strip()
            if not relative_path or content == "":
                return "Formato esperado: 'crea archivo <ruta> | <contenido>'"

            encoded_content = content.encode("utf-8")
            content_sha256 = hashlib.sha256(encoded_content).hexdigest()
            content_bytes = len(encoded_content)

            try:
                return self.agent.execute_tool(
                    "file_creator",
                    lambda: self.agent.file_creator.create_file(relative_path, content),
                    action_name="create_file",
                    important_args={
                        "path": relative_path,
                        "content_sha256": content_sha256,
                        "content_bytes": content_bytes,
                    },
                )
            except ApprovalRequiredError:
                raise
            except PermissionError as exc:
                return str(exc)

        if "git_tools" in plan:
            if "status" in message.lower():
                try:
                    result = self.agent.execute_tool(
                        "git_tools",
                        lambda: self.agent.git_tools.execute(
                            {"action": "status"}, structured=True
                        ),
                        action_name="status",
                        important_args={"command": "git status --short"},
                        structured=True,
                    )
                    return self._present_tool_result(result)
                except ApprovalRequiredError:
                    raise
                except PermissionError as exc:
                    return str(exc)
            if "checkpoint" in message.lower():
                try:
                    result = self.agent.execute_tool(
                        "git_tools",
                        lambda: self.agent.git_tools.execute(
                            {
                                "action": "checkpoint",
                                "message": "Checkpoint before AI modification",
                            },
                            structured=True,
                        ),
                        action_name="checkpoint",
                        important_args={"message": "Checkpoint before AI modification"},
                        structured=True,
                    )
                    return self._present_tool_result(result)
                except ApprovalRequiredError:
                    raise
                except PermissionError as exc:
                    return str(exc)
            if "rollback" in message.lower():
                try:
                    result = self.agent.execute_tool(
                        "git_tools",
                        lambda: self.agent.git_tools.execute(
                            {"action": "rollback"}, structured=True
                        ),
                        action_name="rollback",
                        important_args={"target": "HEAD"},
                        structured=True,
                    )
                    return self._present_tool_result(result)
                except ApprovalRequiredError:
                    raise
                except PermissionError as exc:
                    return str(exc)

        if "patch_generator" in plan:
            if message.lower().startswith(("propón cambio", "propone cambio")):
                try:
                    target = message.split(maxsplit=2)[2].strip()
                except IndexError:
                    return "Formato esperado: 'Propón cambio <archivo> | <nuevo contenido>'"

                parts = target.split(" | ", 1)
                if len(parts) != 2:
                    return "Formato esperado: 'Propón cambio <archivo> | <nuevo contenido>'"

                relative_path, new_content = parts
                try:
                    return self.agent.execute_tool(
                        "patch_generator",
                        lambda: self.agent.patch_generator.generate_patch_from_file(relative_path, new_content),
                        action_name="generate_patch",
                        important_args={"path": relative_path},
                    )
                except ApprovalRequiredError:
                    raise
                except PermissionError as exc:
                    return str(exc)

        if "patch_applier" in plan:
            try:
                parsed = parse_patch_command(message)
            except ValueError:
                return "Formato esperado: 'aplica cambio <archivo> | <contenido nuevo> | <contenido anterior>'"

            if parsed is None:
                return UNHANDLED

            relative_path, new_content, old_content = parsed

            approval_args = build_patch_approval_args(relative_path, old_content, new_content)

            try:
                return self.agent.execute_tool(
                    "patch_applier",
                    lambda: self.agent.patch_applier.apply_patch(relative_path, old_content, new_content),
                    action_name="apply_patch",
                    important_args=approval_args,
                )
            except ApprovalRequiredError:
                raise
            except PermissionError as exc:
                return str(exc)

        return UNHANDLED

    @staticmethod
    def _present_tool_result(result):
        return present_tool_result(result)
