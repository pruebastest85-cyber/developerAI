import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from brain.local_model_client import LocalModelClient
from brain.local_model_config import LocalModelConfig
from brain.change_proposal import TestSpec
from brain.model_correction import (
    MODEL_CORRECTION_OUTPUT_SCHEMA,
    ModelCorrectionAdapter,
    ModelCorrectionContext,
    ModelCorrectionError,
    ModelCorrectionProposalDraft,
    ModelCorrectionService,
)
from brain.model_transport import TransportResponse
from brain.model_plan import SAFE_MODEL_OPERATION_CATALOG
from brain.workflow_limits import WorkflowLimits


def draft(**changes):
    value = {
        "schema_version": "1",
        "summary": "Apply a bounded correction",
        "changes": [
            {
                "operation": "create",
                "path": "created.py",
                "new_content": "value = 1\n",
                "expected_sha256": None,
                "justification": "Required",
            }
        ],
        "risks": [],
    }
    value.update(changes)
    return value


def envelope(payload):
    return json.dumps(
        {
            "id": "req-1",
            "model": "qwen",
            "choices": [
                {
                    "message": {"content": json.dumps(payload)},
                    "finish_reason": "stop",
                }
            ],
        }
    ).encode("utf-8")


class FakeTransport:
    def __init__(self, payload):
        self.body = envelope(payload)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return TransportResponse(
            200,
            (("Content-Type", "application/json"),),
            self.body,
        )


class ModelCorrectionContractTests(unittest.TestCase):
    def test_privileged_correction_workflow_is_not_in_general_model_catalog(self):
        self.assertNotIn(
            ("correction_workflow", "apply_change_proposal"),
            SAFE_MODEL_OPERATION_CATALOG,
        )

    def test_valid_contract_is_immutable_and_identity_is_deterministic(self):
        first = ModelCorrectionProposalDraft.from_mapping(draft())
        second = ModelCorrectionProposalDraft.from_mapping(
            {
                "risks": [],
                "changes": draft()["changes"],
                "summary": "Apply a bounded correction",
                "schema_version": "1",
            }
        )
        self.assertEqual(first.draft_id, second.draft_id)
        self.assertTrue(first.draft_id.startswith("mc1_"))
        with self.assertRaises(AttributeError):
            first.summary = "changed"
        with self.assertRaises(TypeError):
            first.changes[0] = first.changes[0]

    def test_strict_json_rejects_duplicates_invalid_and_trailing_content(self):
        values = (
            '{"schema_version":"1","schema_version":"1"}',
            "not-json",
            json.dumps(draft()) + " trailing",
        )
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ModelCorrectionError):
                    ModelCorrectionProposalDraft.from_json(value)

    def test_unknown_and_authority_fields_are_rejected_everywhere(self):
        forbidden = (
            "approval",
            "budget",
            "tests",
            "base_dir",
            "tool",
            "adapter",
            "command",
            "commit",
            "push",
            "approval_token",
        )
        for name in forbidden:
            value = draft()
            value[name] = True
            with self.subTest(name=name):
                with self.assertRaises(ModelCorrectionError):
                    ModelCorrectionProposalDraft.from_mapping(value)
        nested = draft()
        nested["changes"][0]["approved"] = True
        with self.assertRaises(ModelCorrectionError):
            ModelCorrectionProposalDraft.from_mapping(nested)

    def test_exact_types_version_operations_and_limits_are_closed(self):
        invalid = [
            draft(schema_version="2"),
            draft(summary=""),
            draft(changes=[]),
            draft(changes=draft()["changes"] * 6),
            draft(risks=[""] * 1),
        ]
        operation = draft()
        operation["changes"][0]["operation"] = "delete"
        invalid.append(operation)
        hostile = dict(draft())
        hostile["changes"] = tuple(hostile["changes"])
        invalid.append(hostile)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ModelCorrectionError):
                    ModelCorrectionProposalDraft.from_mapping(value)

        oversized = draft()
        oversized["changes"][0]["new_content"] = "x" * (256 * 1024 + 1)
        with self.assertRaises(ModelCorrectionError):
            ModelCorrectionProposalDraft.from_mapping(oversized)

    def test_create_requires_null_and_replace_requires_lowercase_sha256(self):
        create = draft()
        create["changes"][0]["expected_sha256"] = "0" * 64
        replace = draft()
        replace["changes"][0].update(
            operation="replace",
            expected_sha256=None,
        )
        uppercase = draft()
        uppercase["changes"][0].update(
            operation="replace",
            expected_sha256="A" * 64,
        )
        for value in (create, replace, uppercase):
            with self.assertRaises(ModelCorrectionError):
                ModelCorrectionProposalDraft.from_mapping(value)


class ModelCorrectionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.adapter = ModelCorrectionAdapter(self.base)

    def test_create_is_validated_without_writing_and_tests_are_system_owned(self):
        value = ModelCorrectionProposalDraft.from_mapping(draft())
        result = self.adapter.adapt(value, tests=(TestSpec("full"),))
        self.assertFalse((self.base / "created.py").exists())
        self.assertEqual(result.draft_id, value.draft_id)
        self.assertEqual(result.proposal.tests, (TestSpec("full"),))
        self.assertEqual(result.proposal.budget.modified_files, 1)
        self.assertEqual(result.proposal.proposal_id, result.validated.proposal_id)

    def test_replace_requires_exact_current_hash(self):
        path = self.base / "sample.py"
        path.write_text("old = 1\n", encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        value = draft(
            changes=[
                {
                    "operation": "replace",
                    "path": "sample.py",
                    "new_content": "old = 2\n",
                    "expected_sha256": digest,
                    "justification": "",
                }
            ]
        )
        result = self.adapter.adapt(
            ModelCorrectionProposalDraft.from_mapping(value),
            tests=(TestSpec("focused", ("tests.test_model_correction",)),),
        )
        self.assertEqual(
            tuple(item.scope for item in result.proposal.tests),
            ("focused", "full"),
        )
        self.assertEqual(path.read_text(encoding="utf-8"), "old = 1\n")

        value["changes"][0]["expected_sha256"] = "0" * 64
        with self.assertRaises(ModelCorrectionError) as caught:
            self.adapter.adapt(
                ModelCorrectionProposalDraft.from_mapping(value),
                tests=(TestSpec("full"),),
            )
        self.assertEqual(caught.exception.code, "stale_correction_precondition")

    def test_paths_limits_and_existing_file_rules_fail_without_effect(self):
        outside = self.base.parent / "outside.py"
        values = []
        for path in ("../outside.py", str(outside.resolve())):
            item = draft()
            item["changes"][0]["path"] = path
            values.append(item)
        existing = self.base / "existing.py"
        existing.write_text("safe\n", encoding="utf-8")
        item = draft()
        item["changes"][0]["path"] = "existing.py"
        values.append(item)
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ModelCorrectionError):
                    self.adapter.adapt(
                        ModelCorrectionProposalDraft.from_mapping(value),
                        tests=(TestSpec("full"),),
                    )
        self.assertEqual(existing.read_text(encoding="utf-8"), "safe\n")
        self.assertFalse(outside.exists())

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlink unavailable")
    def test_write_through_symlink_is_rejected(self):
        target = self.base / "target.py"
        target.write_text("safe\n", encoding="utf-8")
        link = self.base / "link.py"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        value = draft()
        value["changes"][0]["path"] = "link.py"
        with self.assertRaises(ModelCorrectionError):
            self.adapter.adapt(
                ModelCorrectionProposalDraft.from_mapping(value),
                tests=(TestSpec("full"),),
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "safe\n")

    def test_system_limits_cannot_be_expanded_by_draft(self):
        limited = ModelCorrectionAdapter(
            self.base,
            limits=WorkflowLimits(max_total_change_bytes=4),
        )
        with self.assertRaises(ModelCorrectionError) as caught:
            limited.adapt(
                ModelCorrectionProposalDraft.from_mapping(draft()),
                tests=(TestSpec("full"),),
            )
        self.assertEqual(caught.exception.code, "correction_limit_exceeded")


class ModelCorrectionServiceTests(unittest.TestCase):
    def test_service_uses_closed_schema_and_returns_only_a_draft(self):
        transport = FakeTransport(draft())
        client = LocalModelClient(
            LocalModelConfig(
                provider="lm_studio",
                base_url="http://localhost:1234/v1",
                model="qwen",
            ),
            transport=transport,
            clock=iter([1.0, 1.1]).__next__,
        )
        service = ModelCorrectionService(client)
        context = ModelCorrectionContext(
            session_id="session",
            runtime_id="runtime",
            step_id="correct",
            goal="Fix tests",
            failure_code="tests_failed",
            remaining_files=5,
            remaining_bytes=1024,
            remaining_lines=100,
        )
        result = service.propose(context)
        self.assertIs(type(result.draft), ModelCorrectionProposalDraft)
        sent = json.loads(transport.requests[0].body)
        self.assertEqual(
            sent["response_format"]["json_schema"]["schema"],
            MODEL_CORRECTION_OUTPUT_SCHEMA.to_openai_schema(),
        )
        serialized = str(sent)
        self.assertNotIn("session_id", serialized)
        self.assertNotIn("runtime_id", serialized)

    def test_direct_service_invocation_has_no_execution_authority(self):
        transport = FakeTransport(draft())
        client = LocalModelClient(
            LocalModelConfig(
                provider="lm_studio",
                base_url="http://localhost:1234/v1",
                model="qwen",
            ),
            transport=transport,
            clock=iter([1.0, 1.1]).__next__,
        )
        service = ModelCorrectionService(client)
        context = ModelCorrectionContext(
            session_id="session",
            runtime_id="runtime",
            step_id="correct",
            goal="Fix tests",
            failure_code="tests_failed",
            remaining_files=1,
            remaining_bytes=100,
            remaining_lines=10,
        )
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "sentinel.py"
            sentinel.write_text("unchanged\n", encoding="utf-8")

            generated = service.propose(context)

            self.assertIs(type(generated.draft), ModelCorrectionProposalDraft)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged\n")
            self.assertEqual(set(vars(service)), {"_model_client"})


if __name__ == "__main__":
    unittest.main()
