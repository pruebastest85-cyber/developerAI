import json
from pathlib import Path

from tools.file_tools import listar_archivos

PROJECT_INDEX_PATH = Path(__file__).resolve().parent.parent / "project" / "index.json"


def construir_indice(base_dir=None):
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent

    archivos = listar_archivos(base_dir)
    indice = {}

    for ruta in archivos:
        path = Path(ruta)
        nombre = path.name
        rel = path.relative_to(base_dir).as_posix()
        indice[nombre] = {
            "tipo": path.suffix.lower().lstrip("."),
            "ubicacion": "/" + str(path.parent.relative_to(base_dir)).replace("\\", "/") if path.parent != base_dir else "/",
            "ruta": rel,
        }

    PROJECT_INDEX_PATH.write_text(json.dumps(indice, ensure_ascii=False, indent=2), encoding="utf-8")
    return indice


def leer_indice():
    if not PROJECT_INDEX_PATH.exists():
        return {}
    return json.loads(PROJECT_INDEX_PATH.read_text(encoding="utf-8"))


def buscar_en_indice(pregunta):
    indice = leer_indice()
    texto = pregunta.lower()
    resultados = []

    for nombre, datos in indice.items():
        if nombre.lower() in texto or datos.get("ruta", "").lower() in texto:
            resultados.append((nombre, datos))

    return resultados
