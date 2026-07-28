from tools.registry import build_default_registry


FILE_COMMAND_PREFIXES = (
    ("crea", "archivo"),
    ("crear", "archivo"),
)

PATCH_COMMAND_PREFIXES = (
    ("aplica", "cambio"),
    ("aplica", "el", "cambio"),
)

TEST_COMMAND_PREFIXES = (
    ("prueba",),
    ("pruebas",),
    ("ejecutar", "pruebas"),
    ("ejecuta", "pruebas"),
    ("ejecutar", "tests"),
    ("ejecuta", "tests"),
    ("run", "tests"),
)


class Planner:
    def __init__(self, tools=None, registry=None):
        self.tools = tools or {}
        self.registry = registry or build_default_registry()

    def plan(self, message):
        text = message.lower().strip()
        words = text.split()

        if self._matches_file_command(words):
            return ["file_creator"]

        if any(keyword in text for keyword in ["analiza", "explica", "explícame"]):
            return ["code_analyzer", "code_reader"]

        if any(keyword in text for keyword in ["recuerda", "memoria", "recuerdo"]):
            return ["memory"]

        if self._matches_test_command(words):
            return ["test_runner"]

        if any(keyword in text for keyword in ["git", "checkpoint", "rollback"]):
            return ["git_tools"]

        if self._matches_patch_command(words):
            return ["patch_generator", "patch_applier"]

        if any(keyword in text for keyword in ["dónde está", "donde esta", "ubicación", "archivo"]):
            return ["project_scanner"]

        return ["default"]

    def available_tools(self):
        return self.registry.list()

    @staticmethod
    def _matches_file_command(words):
        if len(words) < 2:
            return False

        for prefix in FILE_COMMAND_PREFIXES:
            if tuple(words[: len(prefix)]) == prefix:
                return True

        return False

    @staticmethod
    def _matches_test_command(words):
        if not words:
            return False

        for prefix in TEST_COMMAND_PREFIXES:
            if len(words) < len(prefix):
                continue
            if tuple(words[: len(prefix)]) == prefix:
                return True

        return False

    @staticmethod
    def _matches_patch_command(words):
        if not words:
            return False

        for prefix in PATCH_COMMAND_PREFIXES:
            if len(words) < len(prefix):
                continue
            if tuple(words[: len(prefix)]) == prefix:
                return True

        return False
