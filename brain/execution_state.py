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

    def record_success(self, step_name: str, result: Any) -> None:
        self.completed_steps.append({"step": step_name, "result": result})
        self.current_step = len(self.completed_steps) + len(self.failed_steps)
        self.observations.append(f"success:{step_name}")

    def record_failure(self, step_name: str, error: Any) -> None:
        self.failed_steps.append({"step": step_name, "error": error})
        self.retries += 1
        self.current_step = len(self.completed_steps) + len(self.failed_steps)
        self.observations.append(f"failure:{step_name}:{error}")

    def mark_finished(self) -> None:
        self.finished = True

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
        }
