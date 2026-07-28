import hashlib
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from brain.change_proposal import ChangeProposal, FileChange, ProposalBudget, TestSpec
from brain.change_transaction import (
    ChangeTransaction,
    ChangeTransactionResult,
    DuplicateProposalApplicationError,
    TransactionApplyError,
    TransactionPreconditionError,
    TransactionRollbackError,
    TransactionErrorInfo,
)
from brain.change_validator import ChangeProposalValidator


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def proposal(*changes):
    write_bytes = sum(len(change.new_content.encode("utf-8")) for change in changes)
    changed_lines = sum(len(change.new_content.splitlines()) for change in changes)
    return ChangeProposal(
        changes=changes,
        tests=(TestSpec("full"),),
        justification="transaction test",
        risks=(),
        budget=ProposalBudget(
            modified_files=len(changes),
            new_files=sum(change.operation == "create" for change in changes),
            write_bytes=write_bytes,
            changed_lines=changed_lines,
        ),
    )


class ChangeTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        self.validator = ChangeProposalValidator(self.root)

    def _replace(self, name, old=b"old\n", new="new\n"):
        path = self.root / "src" / name
        path.write_bytes(old)
        return FileChange(f"src/{name}", "replace", new, sha(old))

    def test_replace_create_multifile_and_multibyte_are_atomic(self):
        change = proposal(
            self._replace("one.py", new="café\n"),
            FileChange("src/two.py", "create", "á\n", None),
        )
        validated = self.validator.validate(change)

        result = ChangeTransaction(self.root).apply(validated)

        self.assertEqual((self.root / "src" / "one.py").read_text("utf-8"), "café\n")
        self.assertEqual((self.root / "src" / "two.py").read_text("utf-8"), "á\n")
        self.assertTrue(result.applied)
        self.assertEqual(result.modified_paths, ("src/one.py",))
        self.assertEqual(result.created_paths, ("src/two.py",))
        self.assertIsInstance(result, ChangeTransactionResult)
        with self.assertRaises(AttributeError):
            result.applied = False

    def test_publication_uses_same_directory_temporaries_and_os_replace(self):
        change = proposal(self._replace("one.py"))
        validated = self.validator.validate(change)
        real_replace = os.replace
        calls = []

        def observed(source, target):
            calls.append((Path(source), Path(target)))
            return real_replace(source, target)

        with mock.patch("brain.change_transaction.os.replace", side_effect=observed):
            ChangeTransaction(self.root).apply(validated)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0].parent, calls[0][1].parent)
        self.assertFalse(list((self.root / "src").glob("*.tmp")))

    def test_requires_validated_matching_identity_and_rejects_duplicates(self):
        change = proposal(FileChange("src/new.py", "create", "x", None))
        transaction = ChangeTransaction(self.root)
        with self.assertRaises(TransactionPreconditionError):
            transaction.apply(change)

        validated = self.validator.validate(change)
        tampered = object.__new__(type(validated))
        object.__setattr__(tampered, "proposal", validated.proposal)
        object.__setattr__(tampered, "proposal_id", "0" * 64)
        object.__setattr__(tampered, "resolved_changes", validated.resolved_changes)
        object.__setattr__(tampered, "calculated_budget", validated.calculated_budget)
        object.__setattr__(tampered, "rendered_diffs", validated.rendered_diffs)
        with self.assertRaises(TransactionPreconditionError):
            transaction.apply(tampered)

        changed_resolution = replace(
            validated.resolved_changes[0],
            new_bytes=b"tampered",
        )
        inconsistent = replace(
            validated,
            resolved_changes=(changed_resolution,),
        )
        with self.assertRaises(TransactionPreconditionError):
            transaction.apply(inconsistent)

        forged_copy = replace(validated)
        with self.assertRaises(TransactionPreconditionError):
            transaction.apply(forged_copy)

        transaction.apply(validated)
        with self.assertRaises(DuplicateProposalApplicationError):
            transaction.apply(validated)
        self.assertEqual((self.root / "src" / "new.py").read_text(), "x")

    def test_all_preconditions_are_checked_before_first_write(self):
        scenarios = ("stale", "missing", "create_appeared")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                case = self.root / scenario
                case.mkdir()
                target = case / "old.py"
                target.write_bytes(b"old")
                change = proposal(
                    FileChange(
                        f"{scenario}/old.py",
                        "replace",
                        "new",
                        sha(b"old"),
                    ),
                    FileChange(
                        f"{scenario}/created.py",
                        "create",
                        "created",
                        None,
                    ),
                )
                validated = self.validator.validate(change)
                if scenario == "stale":
                    target.write_bytes(b"external")
                elif scenario == "missing":
                    target.unlink()
                else:
                    (case / "created.py").write_text("external")
                with self.assertRaises(TransactionPreconditionError):
                    ChangeTransaction(self.root).apply(validated)
                self.assertFalse(target.exists() and target.read_bytes() == b"new")

    def test_later_target_is_revalidated_immediately_before_publish(self):
        first = self._replace("one.py")
        second = FileChange("src/two.py", "create", "two", None)
        validated = self.validator.validate(proposal(first, second))
        transaction = ChangeTransaction(self.root)
        original_publish = transaction._publish
        calls = 0

        def introduce_external_file(change, temporaries):
            nonlocal calls
            calls += 1
            result = original_publish(change, temporaries)
            if calls == 1:
                (self.root / "src" / "two.py").write_text("external")
            return result

        with mock.patch.object(
            transaction,
            "_publish",
            side_effect=introduce_external_file,
        ):
            with self.assertRaises(TransactionPreconditionError):
                transaction.apply(validated)
        self.assertEqual((self.root / "src" / "one.py").read_bytes(), b"old\n")
        self.assertEqual((self.root / "src" / "two.py").read_text(), "external")

    def test_failure_after_partial_application_rolls_back_exact_bytes(self):
        first = self._replace("one.py", old=b"\x00old\n", new="one\n")
        second = self._replace("two.py", old=b"second\n", new="two\n")
        validated = self.validator.validate(proposal(first, second))
        real_replace = os.replace
        publication_count = 0

        def fail_second(source, target):
            nonlocal publication_count
            if ".rollback." not in str(source):
                publication_count += 1
                if publication_count == 2:
                    raise OSError("second publication failed")
            return real_replace(source, target)

        with mock.patch("brain.change_transaction.os.replace", side_effect=fail_second):
            with self.assertRaises(TransactionApplyError) as raised:
                ChangeTransaction(self.root).apply(validated)

        self.assertEqual((self.root / "src" / "one.py").read_bytes(), b"\x00old\n")
        self.assertEqual((self.root / "src" / "two.py").read_bytes(), b"second\n")
        self.assertTrue(raised.exception.result.rollback_attempted)
        self.assertTrue(raised.exception.result.rollback_succeeded)
        self.assertFalse(list((self.root / "src").glob("*.tmp")))

    def test_failure_before_first_publication_changes_nothing(self):
        validated = self.validator.validate(proposal(self._replace("one.py")))
        with mock.patch(
            "brain.change_transaction.os.replace",
            side_effect=OSError("first publication failed"),
        ):
            with self.assertRaises(TransactionApplyError) as raised:
                ChangeTransaction(self.root).apply(validated)
        self.assertEqual((self.root / "src" / "one.py").read_bytes(), b"old\n")
        self.assertFalse(raised.exception.result.rollback_attempted)
        self.assertIsNone(raised.exception.result.rollback_succeeded)

    def test_cleanup_failure_rolls_back_published_files(self):
        validated = self.validator.validate(proposal(self._replace("one.py")))
        transaction = ChangeTransaction(self.root)
        cleanup_error = (
            TransactionErrorInfo("cleanup", "temp", "OSError", "cleanup"),
        )
        real_cleanup = transaction._cleanup_temporaries
        calls = 0

        def fail_once(temporaries):
            nonlocal calls
            calls += 1
            if calls == 1:
                return cleanup_error
            return real_cleanup(temporaries)

        with mock.patch.object(
            transaction,
            "_cleanup_temporaries",
            side_effect=fail_once,
        ):
            with self.assertRaises(TransactionApplyError) as raised:
                transaction.apply(validated)
        self.assertEqual((self.root / "src" / "one.py").read_bytes(), b"old\n")
        self.assertTrue(raised.exception.result.rollback_attempted)
        self.assertTrue(raised.exception.result.rollback_succeeded)

    def test_rollback_removes_only_files_created_by_transaction(self):
        existing = self.root / "src" / "existing.py"
        existing.write_text("keep")
        validated = self.validator.validate(
            proposal(
                FileChange("src/new.py", "create", "new", None),
                self._replace("existing.py", old=b"keep", new="changed"),
            )
        )
        real_replace = os.replace
        count = 0

        def fail_second(source, target):
            nonlocal count
            if ".rollback." not in str(source):
                count += 1
                if count == 2:
                    raise OSError("stop")
            return real_replace(source, target)

        with mock.patch("brain.change_transaction.os.replace", side_effect=fail_second):
            with self.assertRaises(TransactionApplyError):
                ChangeTransaction(self.root).apply(validated)

        self.assertFalse((self.root / "src" / "new.py").exists())
        self.assertEqual(existing.read_text(), "keep")

    def test_rollback_failure_preserves_original_error(self):
        validated = self.validator.validate(
            proposal(
                self._replace("one.py"),
                self._replace("two.py"),
            )
        )
        transaction = ChangeTransaction(self.root)
        original_publish = transaction._publish
        calls = 0

        def fail_after_one(change, temporaries):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("original apply error")
            return original_publish(change, temporaries)

        with (
            mock.patch.object(transaction, "_publish", side_effect=fail_after_one),
            mock.patch.object(
                transaction,
                "_restore_snapshot",
                side_effect=OSError("rollback error"),
            ),
        ):
            with self.assertRaises(TransactionRollbackError) as raised:
                transaction.apply(validated)

        self.assertIsInstance(raised.exception.original_error, OSError)
        self.assertFalse(raised.exception.result.rollback_succeeded)
        self.assertGreaterEqual(len(raised.exception.result.errors), 2)
        self.assertEqual(raised.exception.result.modified_paths, ("src/one.py",))
        self.assertGreater(raised.exception.result.write_bytes, 0)

    def test_unexpected_internal_error_is_rerolled_back_then_propagated(self):
        validated = self.validator.validate(
            proposal(self._replace("one.py"), self._replace("two.py"))
        )
        transaction = ChangeTransaction(self.root)
        original_publish = transaction._publish
        calls = 0

        def defect(change, temporaries):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("programming defect")
            return original_publish(change, temporaries)

        with mock.patch.object(transaction, "_publish", side_effect=defect):
            with self.assertRaisesRegex(RuntimeError, "programming defect"):
                transaction.apply(validated)
        self.assertEqual((self.root / "src" / "one.py").read_bytes(), b"old\n")

    def test_symlink_introduced_after_validation_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        directory = self.root / "src" / "real"
        directory.mkdir()
        validated = self.validator.validate(
            proposal(FileChange("src/real/new.py", "create", "x", None))
        )
        directory.rmdir()
        try:
            directory.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"No se pudieron crear symlinks: {exc}")
        with self.assertRaises(TransactionPreconditionError):
            ChangeTransaction(self.root).apply(validated)

    def test_transaction_has_no_test_approval_or_git_dependencies(self):
        validated = self.validator.validate(
            proposal(FileChange("src/new.py", "create", "x", None))
        )
        transaction = ChangeTransaction(self.root)
        self.assertFalse(hasattr(transaction, "test_runner"))
        self.assertFalse(hasattr(transaction, "approval_service"))
        self.assertFalse(hasattr(transaction, "git"))
        transaction.apply(validated)


if __name__ == "__main__":
    unittest.main()
