import json
from pathlib import Path

MEMORY_FILE = Path(__file__).with_name("memory.json")


def leer_memoria(memory_file=None):
    file_path = Path(memory_file) if memory_file else MEMORY_FILE
    if not file_path.exists():
        return {
            "usuario": {},
            "proyectos": [],
            "notas": []
        }

    with file_path.open("r", encoding="utf-8") as archivo:
        return json.load(archivo)


def guardar_memoria(datos, memory_file=None):
    file_path = Path(memory_file) if memory_file else MEMORY_FILE
    with file_path.open("w", encoding="utf-8") as archivo:
        json.dump(datos, archivo, ensure_ascii=False, indent=4)
        archivo.write("\n")


def agregar_recuerdo(clave, valor, memory_file=None):
    datos = leer_memoria(memory_file=memory_file)

    if clave not in datos:
        datos[clave] = []

    if isinstance(datos[clave], list):
        datos[clave].append(valor)
    else:
        datos[clave] = valor

    guardar_memoria(datos, memory_file=memory_file)
    return datos
