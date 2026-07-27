from pathlib import Path


def listar_archivos(base_dir, extensiones=None):
    base = Path(base_dir)
    if not base.exists():
        return []

    if extensiones is None:
        extensiones = {".py", ".json", ".txt", ".md"}

    archivos = []
    for ruta in base.rglob("*"):
        if ruta.is_file() and ruta.suffix.lower() in extensiones:
            archivos.append(str(ruta).replace("\\", "/"))
    return sorted(archivos)


def leer_archivo(ruta):
    path = Path(ruta)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")
