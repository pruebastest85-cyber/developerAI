import tempfile
import unittest
from pathlib import Path

from brain.agent import DeveloperAgent
from memory.memory import leer_memoria


class DeveloperAgentTests(unittest.TestCase):
    def test_memory_note_is_saved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            memory_file = temp_dir / "memory.json"
            agent = DeveloperAgent(
                client=None,
                memory_file=memory_file,
                prompt_dir=Path("prompts"),
                action_log_file=temp_dir / "agent_actions.json",
            )

            response = agent.handle_memory("Recuerda que estoy construyendo un agente modular")

            self.assertIn("Lo recordaré", response)
            self.assertIn(
                "estoy construyendo un agente modular",
                leer_memoria(memory_file=memory_file)["notas"],
            )


if __name__ == "__main__":
    unittest.main()
