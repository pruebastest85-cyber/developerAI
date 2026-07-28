import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from brain.change_proposal import ChangeProposal, FileChange, ProposalBudget, TestSpec
from brain.change_validator import (
    ChangeContentError,
    ChangeLimitExceededError,
    ChangePathError,
    ChangePreconditionError,
    ChangeProposalValidator,
    ProposalBudgetMismatchError,
)
from brain.workflow_limits import WorkflowLimits


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def make_proposal(changes, budget):
    return ChangeProposal(
        tuple(changes),
        (TestSpec("full"),),
        "test",
        (),
        budget,
    )


class ChangeProposalValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()

    def test_valid_replace_and_create_are_resolved_without_writes(self):
        existing = self.root / "src" / "main.py"
        original = b"one\n"
        existing.write_bytes(original)
        proposal = make_proposal(
            (
                FileChange(
                    "src/main.py",
                    "replace",
                    "two\n",
                    digest(original),
                ),
                FileChange("src/new.py", "create", "new\n", None),
            ),
            ProposalBudget(2, 1, 8, 2),
        )
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

        validated = ChangeProposalValidator(self.root).validate(proposal)

        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.root / "src" / "new.py").exists())
        self.assertEqual(validated.proposal_id, proposal.proposal_id)
        self.assertEqual(validated.calculated_budget, proposal.budget)
        self.assertEqual(len(validated.rendered_diffs), 2)
        self.assertIn("-one", validated.rendered_diffs[0])
        self.assertIn("+two", validated.rendered_diffs[0])

    def test_one_invalid_operation_invalidates_whole_without_effects(self):
        original = self.root / "src" / "main.py"
        original.write_text("one\n", encoding="utf-8")
        proposal = make_proposal(
            (
                FileChange("src/new.py", "create", "new\n", None),
                FileChange("src/missing.py", "replace", "x", "0" * 64),
            ),
            ProposalBudget(2, 1, 5, 2),
        )
        with self.assertRaises(ChangePreconditionError):
            ChangeProposalValidator(self.root).validate(proposal)
        self.assertEqual(original.read_text(encoding="utf-8"), "one\n")
        self.assertFalse((self.root / "src" / "new.py").exists())

    def test_missing_existing_and_stale_hash_are_rejected(self):
        with self.assertRaises(ChangePreconditionError):
            ChangeProposalValidator(self.root).validate(
                make_proposal(
                    (FileChange("src/missing.py", "replace", "x", "0" * 64),),
                    ProposalBudget(1, 0, 1, 1),
                )
            )
        target = self.root / "src" / "exists.py"
        target.write_text("old", encoding="utf-8")
        with self.assertRaises(ChangePreconditionError):
            ChangeProposalValidator(self.root).validate(
                make_proposal(
                    (FileChange("src/exists.py", "create", "x", None),),
                    ProposalBudget(1, 1, 1, 1),
                )
            )
        with self.assertRaises(ChangePreconditionError):
            ChangeProposalValidator(self.root).validate(
                make_proposal(
                    (FileChange("src/exists.py", "replace", "x", "0" * 64),),
                    ProposalBudget(1, 0, 1, 1),
                )
            )

    def test_invalid_utf8_and_read_limit_are_rejected(self):
        target = self.root / "src" / "binary.py"
        target.write_bytes(b"\xff")
        with self.assertRaises(ChangeContentError):
            ChangeProposalValidator(self.root).validate(
                make_proposal(
                    (FileChange("src/binary.py", "replace", "x", digest(b"\xff")),),
                    ProposalBudget(1, 0, 1, 1),
                )
            )
        target.write_bytes(b"12345")
        limits = WorkflowLimits(max_read_bytes_per_file=4)
        with self.assertRaises(ChangeLimitExceededError):
            ChangeProposalValidator(self.root, limits).validate(
                make_proposal(
                    (FileChange("src/binary.py", "replace", "x", digest(b"12345")),),
                    ProposalBudget(1, 0, 1, 1),
                )
            )

    def test_forbidden_secret_backup_and_escape_paths_are_rejected(self):
        for path in (
            "project/a.py",
            ".git/config",
            ".env",
            "src/key.backup",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ChangePathError):
                    ChangeProposalValidator(self.root).validate(
                        make_proposal(
                            (FileChange(path, "create", "x", None),),
                            ProposalBudget(1, 1, 1, 1),
                        )
                    )

    def test_symlink_components_are_rejected(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"No se pudieron crear symlinks: {exc}")
        with self.assertRaises(ChangePathError):
            ChangeProposalValidator(self.root).validate(
                make_proposal(
                    (FileChange("link/new.py", "create", "x", None),),
                    ProposalBudget(1, 1, 1, 1),
                )
            )

    def test_budget_must_match_exactly(self):
        proposal = make_proposal(
            (FileChange("src/new.py", "create", "abc\n", None),),
            ProposalBudget(1, 1, 999, 1),
        )
        with self.assertRaises(ProposalBudgetMismatchError):
            ChangeProposalValidator(self.root).validate(proposal)

    def test_each_applicable_limit_is_enforced(self):
        cases = (
            (
                WorkflowLimits(max_modified_files=1),
                (
                    FileChange("src/a.py", "create", "x", None),
                    FileChange("src/b.py", "create", "y", None),
                ),
                ProposalBudget(2, 2, 2, 2),
                "max_modified_files",
            ),
            (
                WorkflowLimits(max_new_file_bytes=1),
                (FileChange("src/a.py", "create", "xx", None),),
                ProposalBudget(1, 1, 2, 1),
                "max_new_file_bytes",
            ),
            (
                WorkflowLimits(max_total_change_bytes=1),
                (FileChange("src/a.py", "create", "xx", None),),
                ProposalBudget(1, 1, 2, 1),
                "max_total_change_bytes",
            ),
            (
                WorkflowLimits(max_changed_lines=1),
                (FileChange("src/a.py", "create", "x\ny\n", None),),
                ProposalBudget(1, 1, 4, 2),
                "max_changed_lines",
            ),
        )
        for limits, changes, budget, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ChangeLimitExceededError, expected):
                    ChangeProposalValidator(self.root, limits).validate(
                        make_proposal(changes, budget)
                    )

    def test_validation_does_not_change_proposal_identity(self):
        proposal = make_proposal(
            (FileChange("src/new.py", "create", "x", None),),
            ProposalBudget(1, 1, 1, 1),
        )
        before = proposal.proposal_id
        ChangeProposalValidator(self.root).validate(proposal)
        self.assertEqual(proposal.proposal_id, before)


if __name__ == "__main__":
    unittest.main()
