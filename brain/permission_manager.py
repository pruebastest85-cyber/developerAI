import json
import uuid


class PermissionManager:
    ACTION_RISK_OVERRIDES = {
        ("git_tools", "status"): "low",
        ("git_tools", "diff"): "low",
        ("git_tools", "checkpoint"): "high",
        ("git_tools", "rollback"): "high",
        ("patch_applier", "apply_patch"): "high",
        ("file_creator", "create_file"): "high",
    }

    def __init__(self, registry=None, medium_requires_confirmation=False, fail_closed=True):
        self.registry = registry
        self.medium_requires_confirmation = bool(medium_requires_confirmation)
        self.fail_closed = bool(fail_closed)
        self._pending_requests = {}
        self._granted_tokens = {}

    def _normalize_args(self, important_args):
        payload = important_args or {}
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def _operation_fingerprint(self, tool_name, action_name, important_args):
        normalized = self._normalize_args(important_args)
        return f"{tool_name}::{action_name}::{normalized}"

    def _resolve_entry(self, tool_name):
        if not self.registry:
            return None
        return self.registry.get(tool_name)

    def _resolve_risk(self, tool_name, action_name, entry):
        if (tool_name, action_name) in self.ACTION_RISK_OVERRIDES:
            return self.ACTION_RISK_OVERRIDES[(tool_name, action_name)]
        return entry.get("risk", "low") if entry else "unknown"

    def _requires_confirmation(self, tool_name, action_name, entry):
        has_action_override = (tool_name, action_name) in self.ACTION_RISK_OVERRIDES
        risk = self._resolve_risk(tool_name, action_name, entry)
        if risk == "high":
            return True
        if risk == "medium" and self.medium_requires_confirmation:
            return True
        if has_action_override:
            return False
        return bool(entry and entry.get("requires_confirmation", False))

    def is_confirmation_required(self, tool_name, action_name="execute"):
        entry = self._resolve_entry(tool_name)
        if not entry:
            return False
        return self._requires_confirmation(tool_name, action_name, entry)

    def get_risk_level(self, tool_name, action_name="execute"):
        entry = self._resolve_entry(tool_name)
        return self._resolve_risk(tool_name, action_name, entry)

    def create_approval_request(self, tool_name, action_name, important_args=None):
        if not self.registry:
            return None

        entry = self._resolve_entry(tool_name)
        if entry is None:
            return None

        if not self._requires_confirmation(tool_name, action_name, entry):
            return None

        try:
            fingerprint = self._operation_fingerprint(tool_name, action_name, important_args)
        except (TypeError, ValueError):
            return None

        request_id = str(uuid.uuid4())
        self._pending_requests[request_id] = fingerprint
        return {
            "request_id": request_id,
            "tool": tool_name,
            "action": action_name,
            "important_args": important_args or {},
            "message": "Se requiere aprobación del usuario.",
        }

    # Trusted external boundary:
    # grant_approval must only be called by the user-interface/controller after an
    # explicit user confirmation. Model-driven execution must never call it.
    def grant_approval(self, request_id):
        fingerprint = self._pending_requests.pop(request_id, None)
        if fingerprint is None:
            return None

        approval_token = str(uuid.uuid4())
        self._granted_tokens[approval_token] = fingerprint
        return approval_token

    def cancel_approval_request(self, request_id):
        return self._pending_requests.pop(request_id, None) is not None

    def can_execute(self, tool_name, action_name="execute", important_args=None, approval_token=None):
        entry = self._resolve_entry(tool_name)
        if not entry:
            return not self.fail_closed

        if self._requires_confirmation(tool_name, action_name, entry):
            if not approval_token:
                return False

            try:
                fingerprint = self._operation_fingerprint(tool_name, action_name, important_args)
            except (TypeError, ValueError):
                return False
            approved_fingerprint = self._granted_tokens.get(approval_token)
            if approved_fingerprint != fingerprint:
                return False

            del self._granted_tokens[approval_token]
            return True

        return True

    def explain(self, tool_name, action_name="execute"):
        if not self.registry:
            return "Permiso denegado: no hay registro de herramientas disponible."

        entry = self.registry.get(tool_name)
        if not entry:
            return f"Permiso denegado: la herramienta {tool_name} no está registrada."

        requires_confirmation = self._requires_confirmation(tool_name, action_name, entry)
        risk = self._resolve_risk(tool_name, action_name, entry)

        if requires_confirmation:
            return f"Se requiere confirmación explícita para {tool_name}.{action_name} (riesgo {risk})."

        return f"Permiso concedido para {tool_name}.{action_name} con riesgo {risk}."
