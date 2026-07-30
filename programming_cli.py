"""Opt-in interactive entrypoint for an isolated controlled programming session."""

from __future__ import annotations

import argparse
import sys

from brain.isolated_environment import IsolatedEnvironmentError
from brain.model_errors import LocalModelError
from brain.programming_operator import create_operator_from_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="developerai-programming")
    parser.add_argument("--source", required=True)
    parser.add_argument("--keep-environment", action="store_true")
    parser.add_argument(
        "--enable-local-model",
        action="store_true",
        help="Confirm explicit permission to contact the configured local endpoint.",
    )
    return parser


def main(argv=None, *, input_fn=input, output_fn=print, environ=None) -> int:
    arguments = build_parser().parse_args(argv)
    if not arguments.enable_local_model:
        output_fn("El modelo local no fue autorizado explícitamente.")
        return 2
    try:
        operator = create_operator_from_env(
            arguments.source,
            environ=environ,
            keep_environment=arguments.keep_environment,
        )
    except (IsolatedEnvironmentError, LocalModelError):
        output_fn("No fue posible iniciar el entorno controlado.")
        return 2
    try:
        output_fn(f"Entorno aislado: {operator.isolated_snapshot.repository}")
        output_fn(f"Commit base: {operator.isolated_snapshot.baseline_commit}")
        while True:
            command = input_fn("DeveloperAI> ").strip()
            if command in {"salir", "exit"}:
                return 0
            view = operator.execute(command)
            output_fn(view.presentation)
    except (EOFError, KeyboardInterrupt):
        return 130
    finally:
        operator.close()


if __name__ == "__main__":
    sys.exit(main())
