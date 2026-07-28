from tools.base_tool import Tool


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def register(self, name, description, requires_confirmation=False, tool_instance=None, risk="low"):
        payload = {
            "name": name,
            "description": description,
            "requires_confirmation": requires_confirmation,
            "risk": risk,
        }
        if isinstance(tool_instance, Tool):
            payload["tool"] = tool_instance
        self.tools[name] = payload

    def get(self, name):
        return self.tools.get(name)

    def list(self):
        return list(self.tools.values())

    def names(self):
        return list(self.tools.keys())


def build_default_registry():
    registry = ToolRegistry()
    registry.register("code_reader", "Lee archivos del proyecto", False, risk="low")
    registry.register("code_analyzer", "Analiza la estructura de archivos Python", False, risk="low")
    registry.register("patch_generator", "Genera propuestas de cambio en formato diff", True, risk="medium")
    registry.register("patch_applier", "Aplica cambios aprobados de forma segura", True, risk="high")
    registry.register("file_creator", "Crea archivos nuevos de forma segura", True, risk="high")
    registry.register("test_runner", "Ejecuta pruebas del proyecto", False, risk="medium")
    registry.register("git_tools", "Gestiona checkpoints y rollback de Git", True, risk="high")
    registry.register("memory", "Gestiona recuerdos y memoria persistente", False, risk="low")
    registry.register("project_scanner", "Indexa archivos del proyecto", False, risk="low")
    registry.register("internet_search", "Busca información actualizada en internet", False, risk="low")
    return registry
