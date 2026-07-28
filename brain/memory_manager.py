import re

from memory.memory import agregar_recuerdo, leer_memoria
from tools.tool_result import ToolResult, execute_and_normalize, legacy_tool_value


class MemoryManager:
    name = "memory"
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

    def execute(self, args=None, structured=False):
        if not isinstance(args, dict):
            result = ToolResult.failure(
                self.name, error="Los argumentos deben ser un diccionario"
            )
            return result if structured else legacy_tool_value(result)
        payload = args
        action = payload.get("action", "retrieve")
        if action == "store":
            if not isinstance(payload.get("text", ""), str) or not isinstance(
                payload.get("category", "notas"), str
            ):
                result = ToolResult.failure(
                    self.name, error="text y category deben ser cadenas"
                )
                return result if structured else legacy_tool_value(result)
            result = execute_and_normalize(
                self.name,
                lambda: self.store(
                    payload.get("text", ""),
                    payload.get("category", "notas"),
                ),
                none_policy="ok",
                operational_exceptions=(OSError, UnicodeError),
            )
            if result.data is None:
                result = ToolResult.success(
                    self.name,
                    message="El recuerdo no cumplió los criterios de almacenamiento.",
                )
            return result if structured else legacy_tool_value(result)
        if action == "retrieve":
            if not isinstance(payload.get("query", ""), str):
                result = ToolResult.failure(self.name, error="query debe ser una cadena")
                return result if structured else legacy_tool_value(result)
            limit = payload.get("limit", 5)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                result = ToolResult.failure(
                    self.name, error="limit debe ser un entero no negativo"
                )
                return result if structured else legacy_tool_value(result)
            result = execute_and_normalize(
                self.name,
                lambda: self.retrieve(
                    payload.get("query", ""),
                    limit,
                ),
                operational_exceptions=(OSError, UnicodeError),
            )
            return result if structured else legacy_tool_value(result)
        result = ToolResult.failure(
            self.name,
            error=f"Acción de memoria no soportada: {action}",
        )
        return result if structured else legacy_tool_value(result)
