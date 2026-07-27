from tools.registry import build_default_registry


class Planner:
    def __init__(self, tools=None, registry=None):
        self.tools = tools or {}
        self.registry = registry or build_default_registry()

    def plan(self, message):
        text = message.lower().strip()

        if any(keyword in text for keyword in ["analiza", "explica", "explícame"]):
            return ["code_analyzer", "code_reader"]

        if any(keyword in text for keyword in ["recuerda", "memoria", "recuerdo"]):
            return ["memory"]

        if any(keyword in text for keyword in ["prueba", "test", "tests", "ejecuta"]):
            return ["test_runner"]

        if any(keyword in text for keyword in ["git", "checkpoint", "rollback"]):
            return ["git_tools"]

        if any(keyword in text for keyword in ["cambio", "patch", "propón", "propone", "aplica"]):
            return ["patch_generator", "patch_applier"]

        if any(keyword in text for keyword in ["dónde está", "donde esta", "ubicación", "archivo"]):
            return ["project_scanner"]

        return ["default"]

    def available_tools(self):
        return self.registry.list()
