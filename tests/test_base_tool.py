import unittest

from tools.base_tool import Tool


class BaseToolTests(unittest.TestCase):
    def test_tool_interface_requires_execute(self):
        class DemoTool(Tool):
            name = "demo"
            description = "Herramienta demo"

        tool = DemoTool()
        self.assertEqual(tool.name, "demo")
        self.assertEqual(tool.description, "Herramienta demo")

        with self.assertRaises(NotImplementedError):
            tool.execute()


if __name__ == "__main__":
    unittest.main()
