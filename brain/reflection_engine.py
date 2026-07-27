from __future__ import annotations

from typing import Any, Dict


class ReflectionEngine:
    def __init__(self):
        self.last_decision = None

    def decide(self, result: Dict[str, Any]) -> Dict[str, Any]:
        status = result.get("status", "unknown")
        if status == "failed":
            decision = {
                "action": "replan",
                "reason": "El paso falló; es necesario replanificar y probar otra estrategia.",
            }
        elif status == "ok":
            decision = {
                "action": "continue",
                "reason": "El paso funcionó y se puede continuar con el siguiente.",
            }
        else:
            decision = {
                "action": "continue",
                "reason": "No se detectó un fallo claro; se continúa con el siguiente paso.",
            }

        self.last_decision = decision
        return decision
