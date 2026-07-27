class ToolRouter:
    def __init__(self, agent):
        self.agent = agent

    def dispatch(self, plan, message):
        if not plan:
            return None

        if "code_analyzer" in plan:
            if message.lower().startswith(("analiza", "analizar")):
                target = message.split(maxsplit=1)[1].strip() if len(message.split()) > 1 else ""
                return self.agent.code_analyzer.summarize(target)

        if "code_reader" in plan:
            if message.lower().startswith(("explícame", "explicame")):
                target = message.split(maxsplit=1)[1].strip() if len(message.split()) > 1 else ""
                return self.agent.code_reader.read_file_with_limit(target)

        if "memory" in plan:
            return self.agent.handle_memory(message)

        if "test_runner" in plan:
            return self.agent.test_runner.run_tests_report()

        if "git_tools" in plan:
            if "status" in message.lower():
                return self.agent._format_git_result(self.agent.git_tools.status())
            if "checkpoint" in message.lower():
                return self.agent._format_git_result(self.agent.git_tools.checkpoint())
            if "rollback" in message.lower():
                return self.agent._format_git_result(self.agent.git_tools.rollback())

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
                return self.agent.patch_generator.generate_patch_from_file(relative_path, new_content)

        if "patch_applier" in plan:
            if message.lower().startswith(("aplica cambio", "aplica el cambio")):
                try:
                    target = message.split(maxsplit=2)[2].strip()
                except IndexError:
                    return "Formato esperado: 'Aplica cambio <archivo> | <contenido nuevo> | <contenido anterior>'"

                parts = target.split(" | ", 2)
                if len(parts) != 3:
                    return "Formato esperado: 'Aplica cambio <archivo> | <contenido nuevo> | <contenido anterior>'"

                relative_path, new_content, old_content = parts
                return self.agent.patch_applier.apply_patch(relative_path, old_content, new_content)

        return None
