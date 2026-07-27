import re

from memory.memory import agregar_recuerdo, leer_memoria


class MemoryManager:
    def __init__(self, memory_file=None):
        self.memory_file = memory_file

    def should_store(self, text):
        if not isinstance(text, str):
            return False

        cleaned = text.strip()
        if not cleaned:
            return False

        lowered = cleaned.lower()
        if lowered in {"hola", "gracias", "ok", "okay", "prueba", "test"}:
            return False

        if re.fullmatch(r"[\W_]+", cleaned):
            return False

        if len(cleaned.split()) <= 2 and not any(keyword in lowered for keyword in ["proyecto", "agente", "searxng", "memory", "python", "qwen", "developerai", "archivo", "herramienta", "contexto"]):
            return False

        return True

    def store(self, text, category="notas"):
        if not self.should_store(text):
            return None

        agregar_recuerdo(category, text, memory_file=self.memory_file)
        return text

    def retrieve(self, query, limit=5):
        data = leer_memoria(memory_file=self.memory_file)
        notes = data.get("notas", [])
        if not notes:
            return []

        lowered = query.lower()
        scored = []
        for note in notes:
            if not isinstance(note, str):
                continue
            score = 0
            if lowered in note.lower():
                score += 5
            for keyword in lowered.split():
                if keyword in note.lower():
                    score += 1
            if score > 0:
                scored.append((score, note))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [note for _, note in scored[:limit]]
