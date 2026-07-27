class PermissionManager:
    def __init__(self, registry=None):
        self.registry = registry

    def can_execute(self, tool_name, user_confirmation=False):
        if not self.registry:
            return True

        entry = self.registry.get(tool_name)
        if not entry:
            return True

        requires_confirmation = bool(entry.get("requires_confirmation", False))
        risk = entry.get("risk", "low")

        if risk == "high" or requires_confirmation:
            return bool(user_confirmation)

        return True

    def explain(self, tool_name, user_confirmation=False):
        if not self.registry:
            return "Permiso concedido"

        entry = self.registry.get(tool_name)
        if not entry:
            return "Permiso concedido"

        requires_confirmation = bool(entry.get("requires_confirmation", False))
        risk = entry.get("risk", "low")

        if risk == "high" or requires_confirmation:
            if user_confirmation:
                return f"Permiso concedido para {tool_name} con riesgo {risk}."
            return f"Se requiere confirmación explícita para {tool_name} (riesgo {risk})."

        return f"Permiso concedido para {tool_name} con riesgo {risk}."
