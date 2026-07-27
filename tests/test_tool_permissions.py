import unittest

from tools.base_tool import Tool


class ToolPermissionTests(unittest.TestCase):
    def test_tools_can_expose_risk_and_confirmation_requirements(self):
        class SafeTool(Tool):
            name = "safe_tool"
            description = "Tool without elevated risk"
            requires_confirmation = False
            risk = "low"

        tool = SafeTool()
        self.assertEqual(tool.risk, "low")
        self.assertFalse(tool.requires_confirmation)

        class RiskyTool(Tool):
            name = "risky_tool"
            description = "Tool with elevated risk"
            requires_confirmation = True
            risk = "high"

        risky = RiskyTool()
        self.assertEqual(risky.risk, "high")
        self.assertTrue(risky.requires_confirmation)


if __name__ == "__main__":
    unittest.main()
