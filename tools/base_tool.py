class Tool:
    name = "tool"
    description = "Herramienta base"
    requires_confirmation = False
    risk = "low"

    def execute(self, args=None, structured=False):
        """Execute the tool; structured=True requests a ToolResult."""
        raise NotImplementedError("Cada herramienta debe implementar execute")
