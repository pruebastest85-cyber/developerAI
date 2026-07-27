class Tool:
    name = "tool"
    description = "Herramienta base"
    requires_confirmation = False
    risk = "low"

    def execute(self, args=None):
        raise NotImplementedError("Cada herramienta debe implementar execute")
