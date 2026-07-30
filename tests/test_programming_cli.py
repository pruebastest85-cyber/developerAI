import unittest
from unittest.mock import Mock, patch

import programming_cli


class ProgrammingCliTests(unittest.TestCase):
    def test_requires_explicit_local_model_opt_in(self):
        output = []
        code = programming_cli.main(
            ["--source", "unused"],
            output_fn=output.append,
        )
        self.assertEqual(code, 2)
        self.assertIn("no fue autorizado", output[0])

    def test_start_failure_is_sanitized(self):
        output = []
        with patch(
            "programming_cli.create_operator_from_env",
            side_effect=programming_cli.IsolatedEnvironmentError(),
        ):
            code = programming_cli.main(
                ["--source", "unused", "--enable-local-model"],
                output_fn=output.append,
            )
        self.assertEqual(code, 2)
        self.assertEqual(output, ["No fue posible iniciar el entorno controlado."])

    def test_interactive_exit_closes_operator(self):
        operator = Mock()
        operator.isolated_snapshot.repository = "isolated"
        operator.isolated_snapshot.baseline_commit = "abc"
        output = []
        with patch(
            "programming_cli.create_operator_from_env",
            return_value=operator,
        ):
            code = programming_cli.main(
                ["--source", "source", "--enable-local-model"],
                input_fn=lambda prompt: "salir",
                output_fn=output.append,
            )
        self.assertEqual(code, 0)
        operator.close.assert_called_once_with()
        operator.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
