import tempfile
import unittest
from pathlib import Path

from brain.memory_manager import MemoryManager


class MemoryManagerTests(unittest.TestCase):
    def test_should_store_meaningful_notes_and_ignore_noise(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            memory_file = Path(tmpdir) / "memory.json"
            manager = MemoryManager(memory_file=memory_file)

            self.assertFalse(manager.should_store("hola"))
            self.assertFalse(manager.should_store("prueba"))
            self.assertTrue(manager.should_store("El proyecto usa SearXNG local"))

            stored = manager.store("El proyecto usa SearXNG local")
            self.assertEqual(stored, "El proyecto usa SearXNG local")
            self.assertEqual(manager.retrieve("SearXNG")[0], "El proyecto usa SearXNG local")


if __name__ == "__main__":
    unittest.main()
