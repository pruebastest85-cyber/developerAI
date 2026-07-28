from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

from brain.path_policy import PathPolicy, PathValidationError
from brain.workflow_report import ChangedFileReport, DiffSnapshot


class WorkflowDiffCollector:
    """Capture a read-only HEAD diff for explicitly owned workflow paths."""

    def __init__(self, base_dir, *, path_policy=None, runner=None):
        self.base_dir = Path(base_dir).resolve()
        self.path_policy = path_policy or PathPolicy(self.base_dir)
        self.runner = runner or subprocess.run

    def capture(self, paths, *, max_bytes: int) -> DiffSnapshot:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes debe ser un entero positivo")
        try:
            resolved = self._validated_paths(paths)
        except (PathValidationError, TypeError, ValueError) as exc:
            return self._error("path_rejected", str(exc))
        try:
            root = self._git(["git", "rev-parse", "--show-toplevel"])
        except FileNotFoundError as exc:
            return self._error("git_unavailable", str(exc))
        except OSError as exc:
            return self._error("git_unavailable", str(exc))
        if root.returncode != 0:
            return self._error("not_git_repository", root.stderr.strip())
        try:
            git_root = Path(root.stdout.strip()).resolve()
        except (OSError, ValueError) as exc:
            return self._error("git_failed", str(exc))
        if git_root != self.base_dir:
            return self._error("not_git_repository", "La raíz Git no coincide con el workspace")

        chunks = []
        reports = []
        omitted = []
        binary_files = []
        insertions = deletions = used = 0
        truncated = False
        for relative, absolute in resolved:
            tracked = False
            try:
                tracked_result = self._git(
                    ["git", "ls-files", "--error-unmatch", "--", relative]
                )
                if tracked_result.returncode not in {0, 1}:
                    return self._error(
                        "git_failed",
                        tracked_result.stderr.strip() or "git ls-files falló",
                    )
                tracked = tracked_result.returncode == 0
                item = (
                    self._tracked(relative)
                    if tracked
                    else self._untracked(relative, absolute, max_bytes)
                )
            except FileNotFoundError as exc:
                return self._error("git_unavailable", str(exc))
            except OSError as exc:
                return self._error("git_failed" if tracked else "capture_failed", str(exc))
            if item is None:
                continue
            report, text = item
            if report.omitted_reason == "source_too_large":
                truncated = True
                if relative not in omitted:
                    omitted.append(relative)
            if report.binary:
                binary_files.append(relative)
            if report.insertions is not None:
                insertions += report.insertions
            if report.deletions is not None:
                deletions += report.deletions
            encoded = text.encode("utf-8")
            remaining = max_bytes - used
            if len(encoded) > remaining:
                truncated = True
                if relative not in omitted:
                    omitted.append(relative)
                marker = f"\n[DIFF TRUNCATED: {relative}]\n".encode("utf-8")
                if remaining >= len(marker):
                    prefix = encoded[: remaining - len(marker)].decode("utf-8", errors="ignore")
                    chunks.append(prefix + marker.decode())
                    used += len((prefix + marker.decode()).encode("utf-8"))
                report = ChangedFileReport(
                    path=report.path, kind=report.kind,
                    insertions=report.insertions, deletions=report.deletions,
                    binary=report.binary, diff_included=False,
                    omitted_reason="max_bytes",
                )
            else:
                chunks.append(text)
                used += len(encoded)
            reports.append(report)

        return DiffSnapshot(
            available=True,
            text="".join(chunks),
            files=tuple(reports),
            insertions=insertions,
            deletions=deletions,
            binary_files=tuple(binary_files),
            truncated=truncated,
            omitted_paths=tuple(omitted),
        )

    def _validated_paths(self, paths):
        values = []
        for path in paths:
            resolved = self.path_policy.resolve_for_read(path)
            relative = resolved.relative.as_posix()
            values.append((relative, resolved.absolute))
        return sorted(set(values), key=lambda item: item[0])

    def _git(self, args):
        return self.runner(
            args, cwd=str(self.base_dir), text=True, encoding="utf-8",
            errors="replace", capture_output=True, shell=False,
        )

    def _tracked(self, relative):
        numstat = self._git(["git", "diff", "HEAD", "--numstat", "--", relative])
        patch = self._git(
            ["git", "diff", "HEAD", "--no-ext-diff", "--no-color", "--unified=3", "--", relative]
        )
        if numstat.returncode != 0 or patch.returncode != 0:
            raise OSError((numstat.stderr or patch.stderr).strip() or "git diff falló")
        if not numstat.stdout.strip():
            return None
        added, removed, *_ = numstat.stdout.strip().split("\t")
        binary = added == "-" or removed == "-"
        report = ChangedFileReport(
            relative, "modified",
            None if binary else int(added), None if binary else int(removed), binary,
        )
        text = f"Binary diff omitted: {relative}\n" if binary else patch.stdout
        return report, text

    @staticmethod
    def _untracked(relative, absolute, max_bytes):
        if not absolute.is_file():
            return None
        with absolute.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        oversized = len(raw) > max_bytes
        sample = raw[:max_bytes]
        try:
            content = sample.decode("utf-8")
            binary = "\x00" in content
        except UnicodeDecodeError as exc:
            if exc.end == len(sample) and exc.reason == "unexpected end of data":
                content = sample[:exc.start].decode("utf-8")
                binary = "\x00" in content
            else:
                content, binary = "", True
        if binary:
            return ChangedFileReport(
                relative, "created", None, None, True,
                diff_included=not oversized,
                omitted_reason="source_too_large" if oversized else None,
            ), (
                f"Binary file omitted: {relative}\n"
            )
        lines = content.splitlines(keepends=True)
        patch = "".join(difflib.unified_diff(
            [], lines, fromfile="/dev/null", tofile=f"b/{relative}", lineterm="\n"
        ))
        if oversized:
            patch += f"\n[SOURCE TRUNCATED: {relative}]\n"
        return ChangedFileReport(
            relative, "created", None if oversized else len(lines), 0,
            diff_included=not oversized,
            omitted_reason="source_too_large" if oversized else None,
        ), patch

    @staticmethod
    def _error(code, message):
        return DiffSnapshot(False, error_code=code, error_message=message or code)
