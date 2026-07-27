import tempfile
import unittest
from pathlib import Path

from brain.context_manager import ContextManager
from memory.memory import agregar_recuerdo


class ContextManagerTests(unittest.TestCase):
    def test_build_context_includes_relevant_memories_and_project_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            memory_file = temp_dir / "memory.json"
            agregar_recuerdo("notas", "estamos trabajando en el módulo greet", memory_file=memory_file)

            project_file = temp_dir / "greet.py"
            project_file.write_text(
                "def greet(name):\n    return f'Hola {name}'\n",
                encoding="utf-8",
            )

            manager = ContextManager(base_dir=temp_dir, max_chars=800)
            context = manager.build_context(
                "Explícame greet",
                memory_file=memory_file,
                project_context="Proyecto de ejemplo",
                history=[{"role": "user", "content": "Explícame greet"}],
                external_results=[
                    {"source": "web", "title": "Old blog", "snippet": "Python tutorial"},
                    {"source": "official", "title": "Python docs", "snippet": "How to handle errors"},
                ],
            )

            self.assertIn("Recuerdos", context)
            self.assertIn("greet", context.lower())
            self.assertIn("Proyecto de ejemplo", context)
            self.assertIn("Internet", context)
            self.assertLessEqual(len(context), 800)


if __name__ == "__main__":
    unittest.main()
