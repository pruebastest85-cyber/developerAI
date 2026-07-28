from __future__ import annotations

from brain.workflow_report import WorkflowReport


class WorkflowReportRenderer:
    """Pure Markdown presentation for a completed or suspended report."""

    @staticmethod
    def render_markdown(report: WorkflowReport) -> str:
        if not isinstance(report, WorkflowReport):
            raise TypeError("report debe ser WorkflowReport")
        lines = [
            "# Informe de workflow",
            "",
            f"- Workflow ID: `{report.workflow_id}`",
            f"- Objetivo: {report.goal or '(sin objetivo)'}",
            f"- Estado: **{report.status}**",
            f"- Terminal: {'sí' if report.is_terminal else 'no'}",
            f"- Reanudable: {'sí' if report.is_resumable else 'no'}",
            f"- Commit automático: {'sí' if report.automatic_commit_performed else 'no'}",
            f"- Push automático: {'sí' if report.automatic_push_performed else 'no'}",
            "",
            "## Pasos",
            "",
        ]
        if report.steps:
            for step in report.steps:
                detail = f" — {step.error or step.message}" if (step.error or step.message) else ""
                lines.append(
                    f"- `{step.step_id}` · {step.tool}.{step.action}: **{step.status}** "
                    f"(intentos: {step.attempts}){detail}"
                )
        else:
            lines.append("- Sin pasos.")
        lines.extend(["", "## Cambios", ""])
        if report.changes.files:
            for changed in report.changes.files:
                stats = (
                    "binario"
                    if changed.binary
                    else f"+{changed.insertions or 0} / -{changed.deletions or 0}"
                )
                omitted = f" · omitido: {changed.omitted_reason}" if changed.omitted_reason else ""
                lines.append(f"- `{changed.path}` ({changed.kind}, {stats}){omitted}")
        else:
            lines.append("- Sin cambios detectados.")
        lines.append(
            f"- Totales: +{report.changes.insertions} / -{report.changes.deletions}"
        )
        if report.tests:
            lines.extend(["", "## Pruebas", ""])
            for test in report.tests:
                lines.append(
                    f"- **{test.scope}**: {test.status}; ejecutadas {test.tests_run}, "
                    f"correctas {test.passed}, fallos {test.failures}, "
                    f"errores {test.errors}, omitidas {test.skipped}"
                )
        if report.corrections:
            correction = report.corrections
            lines.extend([
                "", "## Correcciones", "",
                f"- Estado: **{correction.status}**",
                f"- Runtime ID: `{correction.runtime_id}`",
                f"- Iteraciones: {correction.correction_iterations}",
                f"- Propuestas aplicadas: {len(correction.applied_proposal_ids)}",
            ])
            if correction.terminal_reason:
                lines.append(f"- Motivo terminal: {correction.terminal_reason}")
        if report.approval:
            lines.extend([
                "", "## Aprobación", "",
                f"- Estado: **{report.approval.status}**",
            ])
            if report.approval.request_id:
                lines.append(f"- Solicitud: `{report.approval.request_id}`")
        if report.limits:
            limits = report.limits
            lines.extend([
                "", "## Límites", "",
                f"- Correcciones: {limits.correction_iterations}/{limits.max_correction_iterations}",
                f"- Archivos: {limits.modified_files}/{limits.max_modified_files}",
                f"- Bytes: {limits.total_change_bytes}/{limits.max_total_change_bytes}",
                f"- Líneas: {limits.changed_lines}/{limits.max_changed_lines}",
            ])
            if limits.reached:
                lines.append("- Alcanzados: " + ", ".join(limits.reached))
        if report.terminal_error:
            lines.extend(["", "## Error terminal", "", report.terminal_error])
        lines.extend(["", "## Diff final", ""])
        if report.diff is None:
            lines.append("_Diff no solicitado._")
        elif not report.diff.available:
            lines.append(
                f"_Diff no disponible ({report.diff.error_code}): "
                f"{report.diff.error_message or ''}_"
            )
        elif report.diff.text:
            if report.diff.truncated:
                lines.append(
                    "> Advertencia: diff truncado; el contenido mostrado está incompleto."
                )
                lines.append("")
            if report.diff.binary_files:
                lines.append(
                    "> Archivos binarios omitidos: " + ", ".join(
                        f"`{path}`" for path in report.diff.binary_files
                    )
                )
                lines.append("")
            if report.diff.omitted_paths:
                lines.append(
                    "> Rutas omitidas: " + ", ".join(
                        f"`{path}`" for path in report.diff.omitted_paths
                    )
                )
                lines.append("")
            lines.extend(["```diff", report.diff.text.rstrip(), "```"])
        else:
            lines.append("_Diff vacío._")
        return "\n".join(lines) + "\n"
