import unittest
import hashlib

from brain.permission_manager import PermissionManager
from tools.registry import ToolRegistry


class PermissionManagerTests(unittest.TestCase):
    def test_create_approval_request_returns_request_id_not_token(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        request = manager.create_approval_request("patch_applier", "apply_patch", {"path": "main.py"})

        self.assertIsInstance(request, dict)
        self.assertIn("request_id", request)
        self.assertNotIn("approval_token", request)
        self.assertEqual(request["tool"], "patch_applier")
        self.assertEqual(request["action"], "apply_patch")

    def test_request_alone_does_not_allow_execution(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        request = manager.create_approval_request("patch_applier", "apply_patch", {"path": "main.py"})
        self.assertFalse(
            manager.can_execute(
                "patch_applier",
                action_name="apply_patch",
                important_args={"path": "main.py"},
            )
        )
        self.assertIsNotNone(request)

    def test_request_id_does_not_work_as_approval_token(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        request = manager.create_approval_request("patch_applier", "apply_patch", {"path": "main.py"})
        self.assertFalse(
            manager.can_execute(
                "patch_applier",
                action_name="apply_patch",
                important_args={"path": "main.py"},
                approval_token=request["request_id"],
            )
        )

    def test_grant_approval_with_valid_request_returns_token(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        request = manager.create_approval_request("patch_applier", "apply_patch", {"path": "main.py"})
        token = manager.grant_approval(request["request_id"])

        self.assertIsInstance(token, str)
        self.assertTrue(token)

    def test_grant_approval_with_unknown_request_returns_none(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        self.assertIsNone(manager.grant_approval("missing-request-id"))

    def test_request_can_only_be_granted_once(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        request = manager.create_approval_request("patch_applier", "apply_patch", {"path": "main.py"})
        token = manager.grant_approval(request["request_id"])

        self.assertIsNotNone(token)
        self.assertIsNone(manager.grant_approval(request["request_id"]))

    def test_granted_token_allows_exactly_requested_operation(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        request = manager.create_approval_request("patch_applier", "apply_patch", {"path": "main.py"})
        token = manager.grant_approval(request["request_id"])

        self.assertTrue(
            manager.can_execute(
                "patch_applier",
                action_name="apply_patch",
                important_args={"path": "main.py"},
                approval_token=token,
            )
        )

    def test_token_consumes_after_successful_execution(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        request = manager.create_approval_request("patch_applier", "apply_patch", {"path": "main.py"})
        token = manager.grant_approval(request["request_id"])

        self.assertTrue(
            manager.can_execute(
                "patch_applier",
                action_name="apply_patch",
                important_args={"path": "main.py"},
                approval_token=token,
            )
        )
        self.assertFalse(
            manager.can_execute(
                "patch_applier",
                action_name="apply_patch",
                important_args={"path": "main.py"},
                approval_token=token,
            )
        )

    def test_token_with_wrong_args_is_rejected_and_not_consumed(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        request = manager.create_approval_request("patch_applier", "apply_patch", {"path": "main.py"})
        token = manager.grant_approval(request["request_id"])

        self.assertFalse(
            manager.can_execute(
                "patch_applier",
                action_name="apply_patch",
                important_args={"path": "other.py"},
                approval_token=token,
            )
        )
        self.assertTrue(
            manager.can_execute(
                "patch_applier",
                action_name="apply_patch",
                important_args={"path": "main.py"},
                approval_token=token,
            )
        )

    def test_patch_applier_token_is_bound_to_path_hash_and_size(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        old_content = "hola\n"
        new_content = "adios\n"
        old_bytes = old_content.encode("utf-8")
        new_bytes = new_content.encode("utf-8")
        args = {
            "path": "main.py",
            "old_sha256": hashlib.sha256(old_bytes).hexdigest(),
            "new_sha256": hashlib.sha256(new_bytes).hexdigest(),
            "old_bytes": len(old_bytes),
            "new_bytes": len(new_bytes),
        }
        request = manager.create_approval_request("patch_applier", "apply_patch", args)
        token = manager.grant_approval(request["request_id"])

        invalid_variants = [
            {"path": "otro.py", **{k: v for k, v in args.items() if k != "path"}},
            {"path": args["path"], "old_sha256": "0" * 64, "new_sha256": args["new_sha256"], "old_bytes": args["old_bytes"], "new_bytes": args["new_bytes"]},
            {"path": args["path"], "old_sha256": args["old_sha256"], "new_sha256": "1" * 64, "old_bytes": args["old_bytes"], "new_bytes": args["new_bytes"]},
            {"path": args["path"], "old_sha256": args["old_sha256"], "new_sha256": args["new_sha256"], "old_bytes": args["old_bytes"] + 1, "new_bytes": args["new_bytes"]},
            {"path": args["path"], "old_sha256": args["old_sha256"], "new_sha256": args["new_sha256"], "old_bytes": args["old_bytes"], "new_bytes": args["new_bytes"] + 1},
        ]

        for variant in invalid_variants:
            with self.subTest(variant=variant):
                self.assertFalse(
                    manager.can_execute(
                        "patch_applier",
                        action_name="apply_patch",
                        important_args=variant,
                        approval_token=token,
                    )
                )

        self.assertTrue(
            manager.can_execute(
                "patch_applier",
                action_name="apply_patch",
                important_args=args,
                approval_token=token,
            )
        )
        self.assertFalse(
            manager.can_execute(
                "patch_applier",
                action_name="apply_patch",
                important_args=args,
                approval_token=token,
            )
        )

    def test_token_with_wrong_action_is_rejected_and_not_consumed(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        request = manager.create_approval_request("patch_applier", "apply_patch", {"path": "main.py"})
        token = manager.grant_approval(request["request_id"])

        self.assertFalse(
            manager.can_execute(
                "patch_applier",
                action_name="other_action",
                important_args={"path": "main.py"},
                approval_token=token,
            )
        )
        self.assertTrue(
            manager.can_execute(
                "patch_applier",
                action_name="apply_patch",
                important_args={"path": "main.py"},
                approval_token=token,
            )
        )

    def test_file_creator_token_is_bound_to_path_hash_and_size(self):
        registry = ToolRegistry()
        registry.register("file_creator", "Create file", True, risk="high")
        manager = PermissionManager(registry=registry)

        content = "Hola ñ🙂"
        encoded = content.encode("utf-8")
        args = {
            "path": "notas/hola.txt",
            "content_sha256": hashlib.sha256(encoded).hexdigest(),
            "content_bytes": len(encoded),
        }
        request = manager.create_approval_request("file_creator", "create_file", args)
        token = manager.grant_approval(request["request_id"])

        self.assertFalse(
            manager.can_execute(
                "file_creator",
                action_name="create_file",
                important_args={"path": "notas/otro.txt", "content_sha256": args["content_sha256"], "content_bytes": args["content_bytes"]},
                approval_token=token,
            )
        )
        self.assertFalse(
            manager.can_execute(
                "file_creator",
                action_name="create_file",
                important_args={"path": args["path"], "content_sha256": "0" * 64, "content_bytes": args["content_bytes"]},
                approval_token=token,
            )
        )
        self.assertFalse(
            manager.can_execute(
                "file_creator",
                action_name="create_file",
                important_args={"path": args["path"], "content_sha256": args["content_sha256"], "content_bytes": args["content_bytes"] + 1},
                approval_token=token,
            )
        )
        self.assertTrue(
            manager.can_execute(
                "file_creator",
                action_name="create_file",
                important_args=args,
                approval_token=token,
            )
        )
        self.assertFalse(
            manager.can_execute(
                "file_creator",
                action_name="create_file",
                important_args=args,
                approval_token=token,
            )
        )

    def test_low_runs_without_request_or_token(self):
        registry = ToolRegistry()
        registry.register("internet_search", "Search", False, risk="low")
        manager = PermissionManager(registry=registry)

        self.assertTrue(manager.can_execute("internet_search", action_name="search"))

    def test_unknown_tool_is_rejected(self):
        registry = ToolRegistry()
        manager = PermissionManager(registry=registry)

        self.assertFalse(manager.can_execute("does_not_exist", action_name="run"))

    def test_unknown_tool_cannot_create_request(self):
        registry = ToolRegistry()
        manager = PermissionManager(registry=registry)

        self.assertIsNone(manager.create_approval_request("does_not_exist", "run", {}))

    def test_low_operation_does_not_create_request(self):
        registry = ToolRegistry()
        registry.register("internet_search", "Search", False, risk="low")
        manager = PermissionManager(registry=registry)

        self.assertIsNone(manager.create_approval_request("internet_search", "search", {"query": "python"}))

    def test_non_serializable_args_do_not_create_request(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        self.assertIsNone(manager.create_approval_request("patch_applier", "apply_patch", {"bad": {1, 2}}))

    def test_non_serializable_args_rejected_in_can_execute(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        self.assertFalse(manager.can_execute("patch_applier", action_name="apply_patch", important_args={"bad": {1, 2}}))

    def test_user_confirmation_parameter_is_not_supported(self):
        registry = ToolRegistry()
        registry.register("patch_applier", "Apply patch", True, risk="high")
        manager = PermissionManager(registry=registry)

        with self.assertRaises(TypeError):
            manager.can_execute("patch_applier", action_name="apply_patch", user_confirmation=True)


if __name__ == "__main__":
    unittest.main()
