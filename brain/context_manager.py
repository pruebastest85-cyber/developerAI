from pathlib import Path

from brain.context_ranker import ContextRanker
from memory.memory import leer_memoria
from tools.code_reader import CodeReader


class ContextManager:
    def __init__(self, base_dir=None, max_chars=3000):
        self.base_dir = Path(base_dir or ".").resolve()
        self.max_chars = max_chars
        self.code_reader = CodeReader(base_dir=self.base_dir)
        self.ranker = ContextRanker(max_items=6, max_chars=max_chars)

    def _read_relevant_memories(self, memory_file=None):
        if memory_file is None:
            return []
        data = leer_memoria(memory_file=memory_file)
        notes = data.get("notas", [])
        if not notes:
            return []
        return [f"- {note}" for note in notes[-3:]]

    def _read_relevant_files(self, message):
        lowered = message.lower()
        candidates = []

        if "greet" in lowered:
            candidates.append("greet.py")
        if "agent" in lowered:
            candidates.append("brain/agent.py")
        if "memory" in lowered:
            candidates.append("memory/memory.py")

        results = []
        for rel_path in candidates:
            full_path = (self.base_dir / rel_path).resolve()
            if full_path.exists() and full_path.is_file():
                try:
                    content = self.code_reader.read_file_with_limit(rel_path, max_lines=30)
                    results.append(f"Archivo: {rel_path}\n{content}")
                except (FileNotFoundError, ValueError):
                    continue
        return results

    def build_context(self, message, memory_file=None, project_context=None, history=None, external_results=None):
        sections = []

        memory_items = []
        memories = self._read_relevant_memories(memory_file=memory_file)
        for note in memories:
            memory_items.append({"source": "memory", "title": note, "snippet": note})
        if memory_items:
            sections.append("Recuerdos\n" + "\n".join(self._render_ranked_items(memory_items)))

        file_items = []
        for rel_path, content in self._read_relevant_files_with_content(message):
            file_items.append({"source": "project", "title": rel_path, "snippet": content})
        if file_items:
            sections.append("Archivos relevantes\n" + "\n\n".join(self._render_ranked_items(file_items)))

        if project_context:
            sections.append("Contexto de proyecto\n" + project_context)

        history_text = ""
        if history:
            recent = history[-3:]
            history_text = "\n".join(
                f"{entry.get('role', 'user')}: {entry.get('content', '')}" for entry in recent
            )
            if history_text:
                sections.append("Historial reciente\n" + history_text)

        if external_results:
            ranked_external = self.ranker.rank(external_results)
            if ranked_external:
                sections.append("Internet\n" + "\n".join(self._render_ranked_items(ranked_external)))

        context = "\n\n".join(sections)
        if len(context) > self.max_chars:
            return context[: self.max_chars - 3] + "..."
        return context

    def _read_relevant_files_with_content(self, message):
        lowered = message.lower()
        candidates = []

        if "greet" in lowered:
            candidates.append("greet.py")
        if "agent" in lowered:
            candidates.append("brain/agent.py")
        if "memory" in lowered:
            candidates.append("memory/memory.py")

        results = []
        for rel_path in candidates:
            full_path = (self.base_dir / rel_path).resolve()
            if full_path.exists() and full_path.is_file():
                try:
                    content = self.code_reader.read_file_with_limit(rel_path, max_lines=30)
                    results.append((rel_path, content))
                except (FileNotFoundError, ValueError):
                    continue
        return results

    def _render_ranked_items(self, items):
        if not items:
            return []
        ranked = self.ranker.rank(items)
        return [self._render_entry(entry) for entry in ranked]

    def _render_entry(self, entry):
        title = entry.get("title") or entry.get("name") or entry.get("source") or "item"
        snippet = entry.get("snippet") or entry.get("content") or ""
        source = entry.get("source", "unknown")
        priority = entry.get("priority", 0)
        return f"[{priority}] {source}: {title} | {snippet}".strip()
