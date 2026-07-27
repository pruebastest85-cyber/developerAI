import unittest

from brain.permission_manager import PermissionManager
from tools.registry import ToolRegistry


class PermissionManagerTests(unittest.TestCase):
    def test_low_risk_tools_execute_without_confirmation(self):
        registry = ToolRegistry()
        registry.register("internet_search", "Search", False, risk="low")
        manager = PermissionManager(registry=registry)

        self.assertTrue(manager.can_execute("internet_search", user_confirmation=False))

    def test_high_risk_tools_require_confirmation(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        self.assertFalse(manager.can_execute("patch_applier", user_confirmation=False))
        self.assertTrue(manager.can_execute("patch_applier", user_confirmation=True))


if __name__ == "__main__":
    unittest.main()
