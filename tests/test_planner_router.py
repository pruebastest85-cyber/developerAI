import unittest

from brain.planner import Planner
from brain.tool_router import ToolRouter
from brain.agent import DeveloperAgent


class PlannerRouterTests(unittest.TestCase):
    def test_planner_returns_expected_plan(self):
        planner = Planner()
        plan = planner.plan("Analiza brain/agent.py")
        self.assertIn("code_analyzer", plan)
        self.assertIn("code_reader", plan)

    def test_router_dispatches_to_memory_tool(self):
        agent = DeveloperAgent(client=None, memory_file="memory/memory.json", prompt_dir="prompts", base_dir=".")
        router = ToolRouter(agent)
        result = router.dispatch(["memory"], "Recuerda que estoy probando el router")
        self.assertIn("Lo recordaré", result)


if __name__ == "__main__":
    unittest.main()
