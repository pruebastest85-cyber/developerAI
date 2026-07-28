from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ExecutionState:
    goal: str
    current_step: int = 0
    completed_steps: List[Dict[str, Any]] = field(default_factory=list)
    failed_steps: List[Dict[str, Any]] = field(default_factory=list)
    observations: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    retries: int = 0
    max_retries: int = 2
    finished: bool = False
    status: str = "running"
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    alternative_plans: List[List[Dict[str, Any]]] = field(default_factory=list)

    def record_success(self, step_name: str, result: Any, step_identity: Any = None) -> None:
        entry = {"step": step_name, "result": result}
        if step_identity is not None:
            entry["identity"] = step_identity
        self.completed_steps.append(entry)
        self.current_step = len(self.completed_steps) + len(self.failed_steps)
        self.observations.append(f"success:{step_name}")

    def record_failure(self, step_name: str, error: Any) -> None:
        self.failed_steps.append({"step": step_name, "error": error})
        self.current_step = len(self.completed_steps) + len(self.failed_steps)
        self.observations.append(f"failure:{step_name}:{error}")

    def record_replan(self, plan: List[Dict[str, Any]]) -> None:
        self.retries += 1
        self.alternative_plans.append(plan)

    def mark_finished(self, status: str = "completed") -> None:
        self.status = status
        self.finished = True

    def mark_awaiting_approval(self) -> None:
        self.status = "awaiting_approval"
        self.finished = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "current_step": self.current_step,
            "completed_steps": self.completed_steps,
            "failed_steps": self.failed_steps,
            "observations": self.observations,
            "context": self.context,
            "retries": self.retries,
            "max_retries": self.max_retries,
            "finished": self.finished,
            "status": self.status,
            "attempts": self.attempts,
            "alternative_plans": self.alternative_plans,
        }
