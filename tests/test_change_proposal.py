import unittest

from brain.change_proposal import (
    ChangeProposal,
    ChangeProposalStructureError,
    FileChange,
    ProposalBudget,
    TestSpec,
    TestSpecificationError,
)


def proposal(change=None, **overrides):
    values = {
        "changes": (
            change
            or FileChange(
                "src/main.py",
                "create",
                "print('ok')\n",
                None,
            ),
        ),
        "tests": (TestSpec("full"),),
        "justification": "reason",
        "risks": ("low",),
        "budget": ProposalBudget(1, 1, 12, 1),
    }
    values.update(overrides)
    return ChangeProposal(**values)


class ChangeProposalTests(unittest.TestCase):
    def test_collections_are_frozen_and_defensively_copied(self):
        changes = [FileChange("a.py", "create", "x", None)]
        tests = [TestSpec("focused", ["tests.test_demo.Demo.test_one"])]
        risks = ["risk"]
        item = ChangeProposal(
            changes,
            tests,
            "why",
            risks,
            ProposalBudget(1, 1, 1, 1),
        )
        changes.clear()
        tests.clear()
        risks.clear()
        self.assertEqual(len(item.changes), 1)
        self.assertIsInstance(item.changes, tuple)
        self.assertIsInstance(item.tests, tuple)
        self.assertIsInstance(item.risks, tuple)
        with self.assertRaises(AttributeError):
            item.justification = "changed"

    def test_identity_is_stable_and_excludes_descriptive_fields(self):
        first = proposal(justification="one", risks=("risk one",))
        second = proposal(justification="two", risks=("risk two",))
        self.assertEqual(first.proposal_id, second.proposal_id)
        self.assertEqual(first.proposal_id, first.proposal_id)
        self.assertNotIn("print", str(first.identity_payload()))

    def test_effective_fields_change_identity(self):
        base = proposal()
        variants = (
            proposal(FileChange("src/main.py", "create", "different", None)),
            proposal(FileChange("src/other.py", "create", "print('ok')\n", None)),
            proposal(tests=(TestSpec("focused", ("tests.test_x.Case.test_y",)),)),
            proposal(budget=ProposalBudget(1, 1, 13, 1)),
        )
        for variant in variants:
            with self.subTest(variant=variant.identity_payload()):
                self.assertNotEqual(base.proposal_id, variant.proposal_id)

    def test_replace_hash_and_create_none_are_enforced(self):
        with self.assertRaises(ChangeProposalStructureError):
            FileChange("a.py", "replace", "x", None)
        with self.assertRaises(ChangeProposalStructureError):
            FileChange("a.py", "replace", "x", "A" * 64)
        with self.assertRaises(ChangeProposalStructureError):
            FileChange("a.py", "create", "x", "0" * 64)
        valid = FileChange("a.py", "replace", "x", "0" * 64)
        self.assertEqual(valid.operation, "replace")

    def test_invalid_operation_paths_and_duplicates_are_rejected(self):
        with self.assertRaises(ChangeProposalStructureError):
            FileChange("a.py", "delete", "", None)
        for path in ("../a.py", "C:\\a.py", "/a.py", ""):
            with self.subTest(path=path):
                with self.assertRaises(ChangeProposalStructureError):
                    FileChange(path, "create", "x", None)
        with self.assertRaises(ChangeProposalStructureError):
            proposal(
                changes=(
                    FileChange("src/./a.py", "create", "x", None),
                    FileChange("src\\a.py", "create", "y", None),
                )
            )

    def test_budget_rejects_booleans_wrong_types_and_negatives(self):
        for value in (True, False, -1, 1.5, "1", None):
            with self.subTest(value=value):
                with self.assertRaises(ChangeProposalStructureError):
                    ProposalBudget(value, 0, 0, 0)

    def test_empty_proposal_and_wrong_collection_items_are_rejected(self):
        with self.assertRaises(ChangeProposalStructureError):
            proposal(changes=())
        with self.assertRaises(ChangeProposalStructureError):
            proposal(changes=("not-a-change",))

    def test_test_spec_uses_closed_unittest_identifiers(self):
        focused = TestSpec(
            "focused",
            ["tests.test_demo.DemoTests.test_one"],
        )
        self.assertEqual(
            focused.canonical_command("python")[-1],
            "-v",
        )
        self.assertEqual(TestSpec("full").targets, ())
        invalid = (
            ("focused", ()),
            ("full", ("tests.test_x",)),
            ("focused", ("-k",)),
            ("focused", ("tests/test_x.py",)),
            ("focused", ("tests.test_x;whoami",)),
            ("focused", ("python -m unittest",)),
        )
        for scope, targets in invalid:
            with self.subTest(scope=scope, targets=targets):
                with self.assertRaises(TestSpecificationError):
                    TestSpec(scope, targets)


if __name__ == "__main__":
    unittest.main()
