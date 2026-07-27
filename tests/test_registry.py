import unittest

from tools.base_tool import Tool
from tools.registry import ToolRegistry, build_default_registry


class RegistryTests(unittest.TestCase):
    def test_registry_can_register_and_list_tools(self):
        registry = ToolRegistry()
        registry.register("demo", "Herramienta de prueba", False)
        self.assertEqual(registry.get("demo")["name"], "demo")
        self.assertIn("demo", registry.names())

    def test_default_registry_contains_expected_tools(self):
        registry = build_default_registry()
        self.assertIn("code_reader", registry.names())
        self.assertIn("test_runner", registry.names())

    def test_registry_can_store_tool_instance(self):
        class DemoTool(Tool):
            name = "demo"
            description = "Herramienta demo"

            def execute(self, args=None):
                return "ok"

        registry = ToolRegistry()
        registry.register("demo", "Herramienta demo", False, tool_instance=DemoTool())
        self.assertIn("tool", registry.get("demo"))


if __name__ == "__main__":
    unittest.main()
