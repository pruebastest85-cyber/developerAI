from __future__ import annotations

from collections.abc import Mapping

from brain.correction_runtime import CorrectionRuntimeState
from brain.workflow_diff import WorkflowDiffCollector
from brain.workflow_limits import WorkflowLimits
from brain.workflow_plan import WorkflowPlan
from brain.workflow_report import (
    ApprovalReport, ChangeReport, CorrectionReport, LimitReport, TestRunReport,
    WorkflowReport, WorkflowStepReport,
)
from brain.workflow_runtime import WorkflowRuntimeState


class WorkflowReportBuilder:
    """Project stable runtime evidence into an immutable public report."""

    def __init__(self, base_dir, *, limits=None, diff_collector=None):
        self.limits = limits or WorkflowLimits()
        self.diff_collector = diff_collector or WorkflowDiffCollector(base_dir)

    def build(self, plan: WorkflowPlan, runtime: WorkflowRuntimeState) -> WorkflowReport:
        if not isinstance(plan, WorkflowPlan):
            raise TypeError("plan debe ser WorkflowPlan")
        if not isinstance(runtime, WorkflowRuntimeState):
            raise TypeError("runtime debe ser WorkflowRuntimeState")
        runtime.validate_for_plan(plan)
        by_id = {step.id: step for step in plan.steps}
        steps = []
        correction_states = []
        owned = set(runtime.modified_files)
        for step_id in runtime.execution_order:
            spec, state = by_id[step_id], runtime.steps[step_id]
            result = state.result
            steps.append(WorkflowStepReport(
                step_id, spec.tool, spec.action, spec.goal, state.status,
                spec.required, state.attempts,
                result.message if result else "",
                result.error if result else state.reason,
                result.retryable if result else False,
            ))
            if state.correction_runtime is not None:
                correction_states.append(state.correction_runtime)
                owned.update(state.correction_runtime.modified_files)
                owned.update(state.correction_runtime.new_files)

        diff = self.diff_collector.capture(
            owned, max_bytes=self.limits.max_total_change_bytes
        )
        changes = ChangeReport(
            diff.files, diff.insertions, diff.deletions,
            diff.binary_files, diff.omitted_paths,
        )
        tests = []
        for step_id in runtime.execution_order:
            result = runtime.steps[step_id].result
            if result and result.tool_name == "test_runner":
                tests.append(self._test_report(result, None))
        for correction in correction_states:
            for run in correction.test_runs:
                tests.append(self._test_report(run.result, run.test_spec))

        correction = self._correction_report(correction_states[-1]) if correction_states else None
        approval = self._approval_report(runtime)
        reached = []
        if correction and correction.status == "correction_limit_reached":
            reached.append("max_correction_iterations")
        if correction and correction.status == "repeated_failure_limit_reached":
            reached.append("max_repeated_failure")
        correction_iterations = correction.correction_iterations if correction else 0
        limit_report = LimitReport(
            self.limits.max_correction_iterations, correction_iterations,
            self.limits.max_modified_files, len(owned),
            self.limits.max_total_change_bytes,
            max(runtime.total_change_bytes, correction_states[-1].total_write_bytes if correction_states else 0),
            self.limits.max_changed_lines,
            max(runtime.changed_lines, correction_states[-1].total_changed_lines if correction_states else 0),
            tuple(reached),
        )
        terminal_error = None
        if runtime.status in {"failed", "cancelled"}:
            terminal_error = next(
                (runtime.steps[item].reason for item in runtime.execution_order
                 if runtime.steps[item].reason),
                correction.terminal_reason if correction else None,
            )
        return WorkflowReport(
            runtime.runtime_id, runtime.goal, runtime.status, tuple(steps), changes,
            tuple(tests), correction, approval, limit_report, diff, terminal_error,
            False, False,
        )

    @staticmethod
    def _test_report(result, spec):
        data = result.data if isinstance(result.data, Mapping) else {}
        scope = getattr(spec, "scope", None) or str(data.get("scope") or "unknown")
        targets = getattr(spec, "targets", ()) or tuple(data.get("targets") or ())
        return TestRunReport(
            scope=scope, targets=tuple(str(item) for item in targets), status=result.status,
            tests_run=int(data.get("tests_run") or 0), passed=int(data.get("passed") or 0),
            failures=int(data.get("failures") or 0), errors=int(data.get("errors") or 0),
            skipped=int(data.get("skipped") or 0),
            failed_test_ids=tuple(str(item) for item in data.get("failed_test_ids") or ()),
            error_test_ids=tuple(str(item) for item in data.get("error_test_ids") or ()),
            message=result.message, error=result.error,
        )

    @staticmethod
    def _correction_report(state: CorrectionRuntimeState):
        return CorrectionReport(
            state.runtime_id, state.status,
            tuple(item.proposal_id for item in state.proposal_history),
            tuple(sorted(state.applied_proposal_ids)), state.correction_iterations,
            tuple(sorted(state.modified_files)), tuple(sorted(state.new_files)),
            state.terminal_reason,
        )

    @staticmethod
    def _approval_report(runtime):
        relevant = [
            state for state in runtime.steps.values()
            if state.approval_status != "not_required"
        ]
        if not relevant:
            return ApprovalReport()
        state = relevant[-1]
        return ApprovalReport(state.approval_status, state.step_id, state.approval_request_id)
