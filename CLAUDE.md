# DeveloperAI — contexto operativo para Claude

Lee este archivo completo antes de proponer cualquier cambio.

---

## 0. Reglas duras — no negociables

Estas reglas no se relajan por ningún motivo, ni siquiera si una sesión anterior parece haberlas ignorado o si el usuario parece tener prisa. Si algo parece requerir violarlas, **detente y pregunta**.

**Prohibido sin autorización explícita en la sesión actual:**

- `git add`, `git commit`, `git push`, o cualquier forma de staging. **Nunca automáticamente.**
- Contactar LM Studio o Qwen. Ningún transporte real al modelo. Las pruebas usan transportes simulados.
- Ejecutar un ensayo real (*real trial*).
- Inspeccionar, modificar, añadir o eliminar el directorio `project/`. Está fuera de alcance por completo. No lo abras ni para leerlo.
- Modificar `controlled_trial_harness.py`, el workflow productivo, o contratos públicos no relacionados con la tarea en curso.

**Alcance autorizado actualmente (Fase 8.5):**

- `controlled_trial_process.py`
- `tests/test_controlled_trial_process.py`
- `trial_windows_fs.py` (auxiliar privado del proceso controlado)

Cualquier archivo fuera de esa lista requiere permiso explícito antes de tocarlo.

**Cómo debe trabajar el asistente:**

- El usuario no programa a nivel avanzado. **Explica cada decisión técnica en lenguaje sencillo**, sin asumir conocimiento de Windows internals, ctypes o NT APIs.
- No inventes el contenido de archivos que no hayas leído.
- No asumas que las cifras de pruebas o el estado de Git siguen igual: **verifícalos siempre** al empezar.
- Si una garantía de seguridad no puede implementarse de forma segura en Windows con el diseño actual, **detente y explícalo** en lugar de simular seguridad.
- Conserva las garantías ya implementadas. No las debilites para hacer pasar una prueba.
- Cambios pequeños, verificables y por fases. Un hallazgo a la vez.

---

## 1. Qué es DeveloperAI

Asistente de programación local y autónomo pero **controlado**, en `C:\Users\black\DeveloperAI`.

Filosofía central:

> el modelo propone → el sistema valida → el usuario aprueba → herramientas deterministas ejecutan → todo queda registrado → ninguna acción peligrosa ocurre automáticamente.

Aprobar un plan **no** equivale a aprobar todas las operaciones sensibles que contiene. Las aprobaciones operacionales son adicionales e independientes.

El sistema no debe generar automáticamente un segundo plan cuando algo falla. Debe conservar la misma sesión, el mismo plan, el mismo workflow, el mismo `ProgrammingOperator` y las aprobaciones ya registradas, con un historial inmutable y auditable.

## 2. Entorno

- Windows 11, Intel i5-12400F, 48 GB RAM, RTX 3090 (24 GB VRAM)
- Modelo local: **Qwen3.6-35B-A3B** vía **LM Studio**, endpoint OpenAI-compatible en `http://localhost:1234/v1`
- Ollama instalado pero el diseño gira alrededor del transporte de LM Studio
- Sin `pytest.ini`, `pyproject.toml` ni `requirements.txt`. Suite en `tests/` (60 archivos)

**Ejecutar pruebas (PowerShell, desde la raíz del repo):**

```powershell
# Suite completa
python -m pytest tests/ -q

# Solo el alcance de la Fase 8.5
python -m pytest tests/test_controlled_trial_process.py tests/test_controlled_trial_harness.py -v
```

Las pruebas del proceso controlado **solo son significativas en Windows nativo**. Muchas usan `@unittest.skipUnless(os.name == "nt", ...)` porque dependen de `NtCreateFile`, `icacls`, SIDs y DACLs. Un entorno Linux o WSL las omite y da un falso «todo verde».

## 3. Arquitectura

- **Núcleo:** `brain/agent.py`, `brain/planner.py`, `brain/tool_router.py`
- **Herramientas:** `tools/` — `code_reader`, `code_analyzer`, `patch_generator`, `patch_applier`, `test_runner`, `git_tools`, `registry`, `base_tool`, `action_logger`
- **Memoria:** `memory/memory.py` — conserva contexto estructurado sin que la información restaurada obtenga autoridad operacional automáticamente
- **Cambios declarativos:** `ValidatedChangeProposal`, `ChangeTransaction`, `ChangeTransactionResult`. `ChangeTransaction` solo acepta propuestas ya validadas, comprueba precondiciones, impide aplicar dos veces la misma propuesta, preserva el estado anterior y hace rollback

## 4. Fase 8.5 — trabajo actual

**Objetivo:** demostrar mediante un ensayo controlado que una sesión de programación permanece viva en un proceso propietario persistente, **aunque el proceso que la inició termine por completo**. Un cliente o shell puede sufrir timeout; la sesión no debe perderse.

**Archivos:** `controlled_trial_process.py`, `controlled_trial_harness.py`, `trial_windows_fs.py`, y sus pruebas.

**Separación de roles:**

| Rol | Qué tiene |
|---|---|
| Proceso iniciador | Lanza al propietario y puede morir completamente |
| Proceso propietario | Autoridad real: sesión viva, workflow, `ProgrammingOperator`, callbacks, transporte simulado, aprobaciones, estado |
| Cliente autorizado | Envía comandos autenticados al propietario |
| Evidencia pública | Solo observación. **No** puede continuar la sesión ni reconstruir autoridad |

La evidencia **nunca** debe contener secretos, capacidad reutilizable, callbacks, closures, transporte, aprobaciones operacionales, el `ProgrammingOperator`, ni autoridad para ejecutar comandos. Si el propietario muere, la evidencia puede mostrar el último estado o marcar la sesión como `lost`, pero no revivirla.

## 5. Garantías implementadas — no debilitar

- **Separación de autoridad:** `TrialProcessHandle` no contiene la capacidad secreta. `_CommandAuthority` es un objeto opaco que rechaza `copy`, `deepcopy` y `pickle`, no expone la clave en `repr`/`str`, y `dataclasses.asdict()` no puede extraer autoridad del handle público.
- **Comandos autenticados:** capacidad de `secrets.token_bytes(32)`, HMAC-SHA256, serialización canónica, `hmac.compare_digest`, secuencias monotónicas estrictas (`last_sequence + 1`), request IDs, e identificadores de propietario/sesión/workflow/plan. El campo `expected_state` es obligatorio, forma parte del documento firmado y debe coincidir exactamente con el estado vivo.
- **Terminación segura en Windows:** prohibido el patrón «consultar PID → cerrar → `os.kill(pid)`». Se abre el proceso, se verifica identidad (PID, tiempo de creación, ejecutable) **con el mismo handle**, el handle permanece abierto, se termina con ese handle y se cierra en éxito y en error. **En Windows nunca `os.kill`** — en Python, `os.kill` en Windows llama a `TerminateProcess` incluso con señal 0.
- **Seguridad de archivos:** validación de archivo regular, ausencia de symlinks, ausencia de junctions y reparse points, propietario esperado, SID, DACL, entradas autorizadas, ausencia de grupos compartidos no permitidos, y permisos equivalentes en POSIX.

## 6. Estado verificado en Windows (30 jul 2026)

Cifras **medidas**, no heredadas. Sustituyen a cualquier cifra anterior.

| Dato | Valor verificado |
|---|---|
| Rama / `HEAD` / `origin/master` | `master` / `cf6163b…` / igual, sincronizado |
| Último commit | `cf6163b` — 3 archivos, 161 inserciones, 59 eliminaciones. Árbol limpio |
| Suite completa | **695 correctas, 0 fallos, 0 omitidas**, 684 subtests, **~45 s** |
| Reproducibilidad | pasadas consecutivas en verde, banda estrecha de 42-43 s |
| Fase 8.5 (proceso + harness) | **49 correctas, 0 fallos**, ~27 s |
| `stderr` de los procesos propietarios | vacío en los 24: ninguna excepción |

La suite tardaba 100-156 s con mucha varianza antes de corregir el hallazgo Q. Ahora 42 s en banda estrecha: la lentitud era la carrera reintentando y agotando esperas.

**Modo de desarrollador de Windows ACTIVADO**. Por eso ya no hay omitidas: las 13 pruebas de symlinks que antes se saltaban por `WinError 1314` ahora se ejecutan de verdad. Cualquier «0 fallos» anterior a esto era un falso verde.

### Hallazgos corregidos en esta sesión

#### Hallazgo S — CORREGIDO (31 jul 2026)

**Lo encontró la prueba del criterio 13 en su primera ejecución útil.** Era un fallo silencioso, la peor clase.

En Windows el estado de enumeración de un directorio **vive en el handle, no en la llamada**. `entries()` usaba siempre `FileIdBothDirectoryInfo` (clase 10), que *continúa* la enumeración. La primera llamada la agotaba; una segunda sobre el mismo handle recibía `ERROR_NO_MORE_FILES` y devolvía lista vacía.

Consecuencia: `_remove_stable_contents` **no era reintentable** sobre el mismo `_StableRoot`, y el reintento **no fallaba** — informaba de éxito dejando todos los archivos en disco. Violación directa del criterio 13 y del punto 9 (orden de limpieza).

**Corrección:** la primera consulta usa `FileIdBothDirectoryRestartInfo` (clase 11) y las siguientes la clase 10.

**Patrón a vigilar en este módulo:** la variable `restart` estaba declarada y nunca usada, igual que `FILE_RENAME_POSIX_SEMANTICS` en el hallazgo Q. **Dos veces ya.** En `trial_windows_fs.py`, una constante o variable declarada y sin usar debe tratarse como una pieza a medio implementar, no como suciedad.

#### Hallazgo Q — CORREGIDO (30 jul 2026)

**Causa raíz de toda la intermitencia residual.** Diagnosticado capturando el `stderr` del propietario, que iba a `DEVNULL`.

El error real era `ntstatus:c0000022` (`STATUS_ACCESS_DENIED`) en `_winfs.rename`, dentro de `_atomic_json`. El propietario reemplaza `owner-status.json` renombrando su temporal encima; el cliente lo está leyendo en ese instante. En Windows, un reemplazo con `FILE_RENAME_REPLACE_IF_EXISTS` **falla con acceso denegado si el destino está abierto por cualquiera**. Carrera clásica escritor/lector.

`trial_windows_fs.py` ya definía `FILE_RENAME_POSIX_SEMANTICS = 0x2` y **nunca la usaba**. Esa bandera es justo la que hace que el reemplazo funcione con el destino abierto: el lector conserva el archivo viejo, quien abra después ve el nuevo. Es lo que significa «reemplazo atómico», y es el comportamiento de `rename(2)`. Requiere Windows 10 1709+, el mismo mínimo que ya asumía `FILE_DISPOSITION_POSIX_SEMANTICS` en `delete()`.

**Corrección:** una línea — añadir la bandera al reemplazo.

**Verificado:** 5 pasadas de 5 en verde, 40 procesos propietarios, cero excepciones en `stderr`, tiempos en banda estrecha de 27-30 s.

**Por qué era tan escurridizo:** dependía de que el cliente tuviera el archivo abierto en el instante exacto del reemplazo. Cuanto más lenta la máquina, más ancha la ventana. Por eso empeoraba bajo carga y desaparecía al instrumentar en exceso.

#### Hallazgo K — CORREGIDO (30 jul 2026)
Causa raíz de los dos fallos. Fue determinista y se demostró experimentalmente. **Ya está corregido**; se documenta aquí para que no se reintroduzca.

El test escribe un comando malformado con `Path.write_text()`. Ese archivo hereda la DACL del directorio contenedor, es decir **`protected = False`**. El bucle del propietario (línea ~1610) llama a `self.root_guard.file_exists(...)` **fuera** del `try/except` que maneja comandos inválidos. Y `file_exists` llama a `_validate_open_file(handle)` con `require_protected=True` por defecto, que rechaza cualquier DACL no protegida lanzando `TrialProcessError("invalid_trial_root")`. Ese tipo no está en el `except (FileNotFoundError, _winfs.NativeFileError)` de `file_exists`, así que escapa, sale del bucle y lo atrapa el `except BaseException` de la línea ~1655, que publica **`state="failed"`**.

**Esto es un defecto de seguridad, no solo un test roto.** Cualquiera capaz de escribir un archivo llano llamado `owner-command.json` en la raíz **destruye la sesión viva**. Es un interruptor de apagado de un solo archivo, y contradice frontalmente el punto 7 («la sesión no debe perderse»). Fallar cerrado debe significar *rechazar el comando y conservar la sesión*, no *destruir la sesión*.

**Corrección aplicada (tres cambios, ninguna garantía debilitada):**

1. `file_exists` responde **solo** existencia: valida con `require_protected=False`. La comprobación de ACL sigue viva en la ruta de lectura, que es donde corresponde.
2. `_validate_open_file` traduce el rechazo de DACL a `invalid_status` en vez de `invalid_trial_root`. Sigue fallando cerrado; solo deja de mentir sobre la causa. Es obligatorio: la línea 229 del test exige ese código.
3. La llamada a `file_exists` del bucle del propietario está **dentro** del `try`. Un archivo hostil se trata ahora como comando rechazado —se borra, se publica el mismo estado con `error_code`, la sesión continúa— y no como fallo fatal.

El comando sigue sin ejecutarse nunca. HMAC, secuencias monotónicas y `expected_state` intactos. Lo único que cambió es la reacción ante basura: rechazar en lugar de destruir.

Prueba de regresión: `test_plain_file_is_rejected_without_destroying_the_session`.

#### Hallazgo C — CORREGIDO (30 jul 2026)
**Problema:** La identidad de la raíz en Windows comparaba solo el índice MFT, ignorando el número de volumen. Esto podría causar falsos positivos si el mismo MFT index aparece en volúmenes diferentes.

**Corrección:** La comparación de identidad ahora usa el tuple completo `(volume_serial, index)` en lugar de solo el índice.

**Cambios en `controlled_trial_process.py`:**
1. Línea 140: `_path_identity` retorna `(volume_serial, index)` para Windows
2. Línea 163: Comparación completa del tuple en `_trial_root`
3. Línea 242: Validación de identidad esperada usa comparación completa del tuple
4. Línea 318: `file_exists` captura `TrialProcessError` para errores de seguridad

#### Hallazgo G — PARCIAL (30 jul 2026)
**Problema:** en Windows, `open_relative` lanza `NativeFileError` para *todos* los fallos, así que `file_exists` no distingue «no existe» de «acceso denegado».

**Intento fallido previo, documentado para que no se repita:** se hizo que `file_exists` capturara `TrialProcessError` y devolviera `False` («no existe para nosotros»). **Eso empeoraba el hallazgo en vez de corregirlo:** un `owner-command.json` que fuese reparse point, directorio o tuviera una ACL hostil quedaba **invisible** — ni se borraba, ni se rechazaba, ni se reportaba, y permanecía en la raíz indefinidamente. Ya está revertido.

**Estado actual:** `file_exists` devuelve `False` solo ante `FileNotFoundError` o `NativeFileError`, y deja propagar `TrialProcessError`, que el bucle del propietario trata como comando rechazado (lo borra, publica el `error_code`, conserva la sesión).

**Lo que falta:** que `open_relative` traduzca `STATUS_OBJECT_NAME_NOT_FOUND` (`0xC0000034`) y `STATUS_OBJECT_PATH_NOT_FOUND` (`0xC000003A`) a `FileNotFoundError` y deje el resto como `NativeFileError`. **Cuidado al hacerlo:** hay un `except _winfs.NativeFileError` en `_atomic_json` (comprobación de duplicado con `replace=False`) que dejaría de capturar y habría que ampliar.

#### Hallazgo J — CORREGIDO (30 jul 2026)
**Problema:** `trial_windows_fs.close()` lanzaba una excepción si `CloseHandle` fallaba, mientras que `_StableRoot.close()` no.

**Corrección:** `trial_windows_fs.close()` ahora envuelve la llamada a `CloseHandle` en un try/except para no lanzar excepciones y mantener la consistencia con `_StableRoot.close()`.

#### Hallazgo F — CORREGIDO (30 jul 2026)
**Problema:** No se verificaba que el ensayo dejó de existir tras la limpieza (criterio 7 del punto 13).

**Intento fallido previo, documentado para que no se repita:** una comprobación con `self.root.exists()` rompía 9 pruebas y se llegó a marcar el hallazgo como «no aplicable». **Esa conclusión era incorrecta.** La causa no era un comportamiento raro de `pathlib`: `test_stable_root_remains_bound_when_pathname_is_reused` **crea a propósito un directorio nuevo con el mismo nombre** antes de limpiar, así que comprobar el *nombre* está condenado a fallar por diseño.

**Corrección real:** `_verify_trial_is_gone(root, identity)` comprueba la **identidad**, no el nombre. Si la ruta ya no resuelve, correcto. Si algo la ocupa con otra identidad, también correcto. Solo falla si sigue existiendo *ese mismo* ensayo.

**Regla que esto ilustra:** nunca se relaja una garantía porque una prueba falle. Si una prueba falla, o la garantía está mal implementada o la prueba mide lo que no debe. Aquí era lo primero.

### Hallazgos abiertos

| # | Hallazgo | Severidad | Ubicación |
|---|----------|-----------|-----------|
| C (corregido) | ~~Identidad de raíz incompleta~~ | ~~Alta~~ | ~~resuelto 30 jul~~ |
| F (no aplicable) | ~~Verificar ensayo tras limpieza~~ | ~~Media~~ | ~~resuelto 30 jul~~ |
| G (corregido) | ~~file_exists no distingue errores~~ | ~~Media~~ | ~~resuelto 30 jul~~ |
| J (corregido) | ~~trial_windows_fs.close() inconsistente~~ | ~~Baja~~ | ~~resuelto 30 jul~~ |
| ~~Q~~ | ~~Intermitencia residual: el propietario moría aleatoriamente~~ **CORREGIDO**, ver abajo | — | resuelto 30 jul |
| ~~R~~ | ~~La suite se volvió un 50% más lenta y con mucha varianza~~ **RESUELTO como efecto colateral de Q.** La lentitud era *consecuencia* de la carrera, no su causa: al corregirla, el subconjunto pasó de 58-164 s con varianza enorme a 27-30 s en banda estrecha | — | resuelto 30 jul |
| P | **Riesgo de diseño abierto:** los descendientes del propietario le sobreviven en Windows | Media (diseño) | `TrialProcessLauncher.start` |

**A y B explican mecánicamente el incidente de limpieza transitoria:** mientras un handle esté abierto, Windows no permite eliminar el directorio que lo contiene. Solo se alcanzan por rutas adversariales que la suite actual no ejercita, lo que explica que el fallo fuera intermitente.

### Ya resuelto correctamente

`_remove_stable_contents` (enumera y borra por handle, recurre en profundidad, rechaza reparse points, cierra en `finally`, sin globs); el reemplazo atómico ligado al handle de la raíz vía `_winfs.rename`; la guarda que impide `os.kill` en Windows.

### Cobertura de los 18 criterios adversariales

**15 cumplidos, 3 parciales (5, 6, 12), ninguno sin prueba.**

Añadidos el 31 jul:

- **Criterio 4** — `test_commands_in_a_substitute_directory_are_not_accepted`. Renombra la raíz, crea otra carpeta con el pathname anterior y comprueba que el comando de la impostora es invisible, que una escritura aterriza en la raíz original **aunque el pathname apunte a la sustituta**, y que la limpieza no la toca. Es la prueba que caza cualquier reintroducción de operaciones por pathname.
- **Criterio 8** — `test_reparse_point_destination_is_rejected_and_never_followed`. Leer un destino convertido en enlace se rechaza con `invalid_status`; `file_exists` deja propagar en vez de mentir; y nada fuera de la raíz se modifica.
- **Criterio 7** — `test_substituted_temporary_is_rejected_and_never_followed`. Fija el nombre del temporal para poder ocuparlo con un enlace y verifica que la creación exclusiva falla sin escribir a través de él.
- **Criterio 15** — `test_repeated_create_fail_cleanup_leaves_zero_temporaries`. Cinco iteraciones de creación, fallo y limpieza sin acumular temporales.

- **Criterio 14** — `test_handles_are_closed_on_success_and_on_error`. Cuenta cada `open_relative` y cada `close` durante el camino feliz, un `_atomic_json` que falla, una lectura y la limpieza completa, y comprueba que las cuentas cuadran tras cada paso.
- **Criterio 13** — `test_partial_removal_can_be_retried_idempotently`. Fuerza un fallo a mitad del borrado, comprueba que hubo progreso y que no terminó, reintenta y verifica que completa, y repite una tercera vez para confirmar idempotencia. **Comprueba propiedades, no un estado intermedio concreto:** el orden de enumeración de NTFS no es parte de ningún contrato y asumirlo produjo tres falsos fallos seguidos.

Faltan pruebas de: comandos colocados en una carpeta sustituta no aceptados (4); temporal sustituido o convertido en reparse point rechazado (7); destino sustituido o convertido en reparse point rechazado (8); varias iteraciones consecutivas de creación, fallo y limpieza dejan cero temporales (15).

## 7. Secuencia de trabajo recomendada

~~1-3. Corregir A, B, C, D, F, G, I, J, K, L, N, O.~~ Hecho el 30 jul.

1. Diagnosticar **Q**, la intermitencia residual. Es el bloqueante real. Técnica que funcionó hoy: un plugin de pytest en `%TEMP%` que parchea `subprocess.Popen` dentro de `controlled_trial_process` para redirigir el `stderr` del propietario (que va a `DEVNULL`) a un archivo. Sin eso, cuando el propietario falla no queda ni una línea. Cargarlo con `-p nombre_plugin` sin instalarlo, con `PYTHONPATH` apuntando al repo y a `%TEMP%`.
4. Escribir las 6 pruebas ausentes (criterios 4, 7, 8, 13, 14, 15) y reforzar las 3 parciales (5, 6, 12). **Activar antes el Modo de desarrollador de Windows**, o las de reparse points se omitirán en silencio.
5. Decidir sobre **E**: no requiere código, requiere reformular la documentación del modelo de amenaza.
6. Documentar **H** como riesgo residual aceptado en POSIX.
7. Limpiar **I y J**.
8. Ejecutar la suite completa en Windows y **reauditar sin modificar código**.

## 8. Criterio para autorizar el ensayo real con Qwen

Solo después de una reauditoría independiente, sin modificar código y sin contactar el modelo, que demuestre:

- ninguna operación sensible vuelve a depender de pathname después de validar el handle;
- la raíz original permanece ligada a todas las operaciones;
- la limpieza no deja temporales;
- las pruebas adversariales pasan;
- la suite completa pasa;
- no se debilitó ninguna garantía anterior.

Entonces se preparará un ensayo **nuevo desde cero**. No debe reutilizarse ningún ensayo anterior.

## 9. Orden obligatorio de limpieza

1. Impedir nuevas operaciones.
2. Terminar o confirmar la muerte del propietario **mediante su handle estable**.
3. Esperar su finalización.
4. Cerrar tuberías, archivos y handles secundarios.
5. Limpiar **exclusivamente** las entradas de la raíz estable.
6. Cerrar el handle de la raíz cuando corresponda.
7. Verificar que el ensayo exacto ya no existe.
8. Reportar de forma saneada cualquier fallo.

**Nunca usar globs ni limpiezas generales.** Los handles se cierran siempre, en éxito y en error.