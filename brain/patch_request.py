import hashlib


PATCH_COMMAND_PREFIXES = (
    ("aplica", "cambio"),
    ("aplica", "el", "cambio"),
)


def build_patch_approval_args(relative_path, old_content, new_content):
    old_bytes = old_content.encode("utf-8")
    new_bytes = new_content.encode("utf-8")
    return {
        "path": relative_path,
        "old_sha256": hashlib.sha256(old_bytes).hexdigest(),
        "new_sha256": hashlib.sha256(new_bytes).hexdigest(),
        "old_bytes": len(old_bytes),
        "new_bytes": len(new_bytes),
    }


def parse_patch_command(message):
    command = message.lstrip()
    lower_command = command.lower()
    prefix = None
    for candidate in PATCH_COMMAND_PREFIXES:
        joined = " ".join(candidate)
        if lower_command.startswith(joined):
            prefix = joined
            break

    if prefix is None:
        return None

    remainder = command[len(prefix):]
    if not remainder.startswith(" "):
        raise ValueError("Formato esperado: 'aplica cambio <archivo> | <contenido nuevo> | <contenido anterior>'")

    payload = remainder[1:]
    parts = payload.split(" | ", 2)
    if len(parts) != 3:
        raise ValueError("Formato esperado: 'aplica cambio <archivo> | <contenido nuevo> | <contenido anterior>'")

    relative_path, new_content, old_content = parts
    relative_path = relative_path.strip()
    if not relative_path or not new_content or not old_content:
        raise ValueError("Formato esperado: 'aplica cambio <archivo> | <contenido nuevo> | <contenido anterior>'")

    return relative_path, new_content, old_content