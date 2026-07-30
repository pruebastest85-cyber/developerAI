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
- **Transporte:** las pruebas confirman transporte simulado exactamente en 1 y cero transportes reales.

## 6. Estado verificado en Windows (30 jul 2026)

Cifras **medidas**, no heredadas. Sustituyen a cualquier cifra anterior.

| Dato | Valor verificado |
|---|---|
| Rama / `HEAD` / `origin/master` | `master` / `6d3f3f7b…` / igual |
| Diff rastreado | 18 archivos, 891 inserciones, 91 eliminaciones (coincide con la línea base histórica) |
| Suite completa | **689 correctas, 0 fallos, 0 omitidas**, 684 subtests, ~100 s |
| Reproducibilidad | **3 pasadas consecutivas en verde** |
| Fase 8.5 (proceso + harness) | **47 correctas, 0 fallos** |
| `stderr` de los 8 procesos propietarios | vacío: ninguna excepción |

**Modo de desarrollador de Windows ACTIVADO.** Por eso ya no hay omitidas: las 13 pruebas de symlinks que antes se saltaban por `WinError 1314` ahora se ejecutan de verdad. Cualquier «0 fallos» anterior a esto era un falso verde.

Historial de esta sesión: se partió de 673 correctas con 2 fallos; al activar el Modo de desarrollador afloraron más (2-3 por corrida). Cinco defectos corregidos, detallados abajo.

**Aviso metodológico importante:** `controlled_trial_process.py`, `trial_windows_fs.py` y `tests/test_controlled_trial_process.py` están **sin trackear** en Git. El `git diff --stat` es **ciego** a los tres archivos en alcance — no sirve para medir el avance de la Fase 8.5.

**Las 13 omitidas no son las de `os.name == "nt"`.** Son todas `WinError 1314`: falta de privilegio para crear symlinks. Las dos pruebas con `@unittest.skipUnless(os.name == "nt")` sí se ejecutan y pasan. Consecuencia: las pruebas pendientes de los criterios 7 y 8 (rechazo de reparse points) se omitirán en silencio si se escriben con symlinks. **Activar el Modo de desarrollador de Windows antes de escribirlas.**

### Hallazgo K — CORREGIDO (30 jul 2026)

Causa raíz de los dos fallos. Fue determinista y se demostró experimentalmente. **Ya está corregido**; se documenta aquí para que no se reintroduzca.

El test escribe un comando malformado con `Path.write_text()`. Ese archivo hereda la DACL del directorio contenedor, es decir **`protected = False`**. El bucle del propietario (línea ~1610) llama a `self.root_guard.file_exists(...)` **fuera** del `try/except` que maneja comandos inválidos. Y `file_exists` llama a `_validate_open_file(handle)` con `require_protected=True` por defecto, que rechaza cualquier DACL no protegida lanzando `TrialProcessError("invalid_trial_root")`. Ese tipo no está en el `except (FileNotFoundError, _winfs.NativeFileError)` de `file_exists`, así que escapa, sale del bucle y lo atrapa el `except BaseException` de la línea ~1655, que publica **`state="failed"`**.

Verificado en aislamiento:

| Cómo se crea el archivo | `protected` | `file_exists` |
|---|---|---|
| `Path.write_text()` | `False` | **lanza `TrialProcessError('invalid_trial_root')`** |
| `_atomic_json()` (usa `_protect_windows_child`) | `True` | devuelve `True` |

**Esto es un defecto de seguridad, no solo un test roto.** Cualquiera capaz de escribir un archivo llano llamado `owner-command.json` en la raíz **destruye la sesión viva**. Es un interruptor de apagado de un solo archivo, y contradice frontalmente el punto 7 («la sesión no debe perderse»). Fallar cerrado debe significar *rechazar el comando y conservar la sesión*, no *destruir la sesión*.

**Corrección aplicada (tres cambios, ninguna garantía debilitada):**

1. `file_exists` responde **solo** existencia: valida con `require_protected=False`. La comprobación de ACL sigue viva en la ruta de lectura, que es donde corresponde.
2. `_validate_open_file` traduce el rechazo de DACL a `invalid_status` en vez de `invalid_trial_root`. Sigue fallando cerrado; solo deja de mentir sobre la causa. Es obligatorio: la línea 229 del test exige ese código.
3. La llamada a `file_exists` del bucle del propietario está **dentro** del `try`. Un archivo hostil se trata ahora como comando rechazado —se borra, se publica el mismo estado con `error_code`, la sesión continúa— y no como fallo fatal.

El comando sigue sin ejecutarse nunca. HMAC, secuencias monotónicas y `expected_state` intactos. Lo único que cambió es la reacción ante basura: rechazar en lugar de destruir.

Prueba de regresión: `test_plain_file_is_rejected_without_destroying_the_session`.

Nota: el hallazgo M (`test_owner_terminated_before_pending_has_no_evidence` fallaba solo en la suite completa) **desapareció con esta corrección** — era contaminación de estado provocada por el mismo defecto. Conviene reejecutar la suite completa varias veces para confirmarlo, ya que era intermitente por naturaleza.

### Hipótesis refutadas experimentalmente — no volver sobre ellas

- **Atributo ReadOnly de los objetos Git.** Refutada: `_remove_stable_contents` borra archivos de solo lectura sin problema. `FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE` funciona sin `FILE_WRITE_ATTRIBUTES`.
- **Ventana de DACL vacía en `_secure_private_path`.** Refutada.
- **Procesos `git.exe` hijos supervivientes.** Refutada: nunca hubo un `git.exe` vivo en 40 capturas, y el fallo es determinista, no una carrera.

### El punto 11.2 debe reformularse

- **Cero carpetas huérfanas nuevas en 40 corridas**, fallara o no el test.
- De las 28 carpetas acumuladas en `%TEMP%`, **20 pertenecen a la cuenta `ANGEL\CodexSandboxOffli…`**, no a `ANGEL\black`. No las creó tu usuario y no son un fallo de limpieza. Requieren `takeown` desde una PowerShell elevada para borrarlas.

El «incidente de limpieza transitoria» documentado en el punto 11.2 **en gran parte no ocurrió como se creía**. Los hallazgos A y B siguen siendo defectos reales de código, pero **no** son la causa de ningún fallo observado.

### Hallazgos abiertos

| # | Hallazgo | Severidad | Ubicación |
|---|---|---|---|
| ~~A~~ | ~~Fuga de handle en `_atomic_json`~~ **CORREGIDO**: el cierre está en su propio `finally` anidado; un borrado fallido ya no se salta el cierre | — | resuelto 30 jul |
| ~~B~~ | ~~Fuga del handle de la raíz~~ **CORREGIDO**: el `finally` cierra `_root_guard` y `_authority`. `_StableRoot.close()` es idempotente | — | resuelto 30 jul |
| C | La identidad de la raíz en Windows compara solo el índice MFT e ignora el volumen | **Alta** | líneas ~113 y ~192 |
| ~~D~~ | ~~`root_guard.close()` antes de la limpieza que necesita el handle~~ **CORREGIDO**: el cierre se movió al `finally`, tras la limpieza del spec | — | resuelto 30 jul |
| E | La capacidad persiste en `owner-capability.bin` y el cliente la relee: la ACL es la frontera de seguridad real, no la tubería anónima | Media (contradicción documental) | `_write_capability` / `_read_capability` |
| F | No se verifica que el ensayo dejó de existir tras la limpieza | Media | línea ~1296 |
| G | En Windows `file_exists` convierte todo error de `NtCreateFile` en `False`, sin distinguir «no existe» de «acceso denegado» | Media | líneas ~252-262 |
| H | Operaciones por nombre relativas a `dir_fd` en POSIX | Baja (residual aceptado) | varias |
| ~~I~~ | ~~`_entry_name`: condición tautológica~~ **CORREGIDO**: sustituida por `isinstance(path, Path)` | — | resuelto 30 jul |
| J | `trial_windows_fs.close()` lanza excepción mientras `_StableRoot.close()` no | Baja | `trial_windows_fs.py` línea ~126 |
| ~~K~~ | ~~`file_exists` deja escapar `TrialProcessError` y mata la sesión~~ **CORREGIDO** | — | resuelto 30 jul |
| ~~L~~ | ~~El patrón `raise ... from None` destruía la causa nativa en 14 rutas~~ **CORREGIDO**: todas encadenan ahora la excepción original. Sin esto, los defectos N y O eran indiagnosticables | — | resuelto 30 jul |
| ~~N~~ | ~~El hilo del latido moría con una excepción no capturada~~ **CORREGIDO**. Un solo fallo de escritura mataba el hilo; el propietario seguía vivo pero mudo para siempre y el cliente declaraba la sesión perdida. Ahora salta el tick y reintenta | — | resuelto 30 jul |
| ~~O~~ | ~~La limpieza fallaba con `STATUS_SHARING_VIOLATION` (`c0000043`)~~ **CORREGIDO**: `_remove_stable_contents` reintenta hasta 15 s. `TerminateProcess` mata al propietario pero **no a sus descendientes**, y un `git.exe` del entorno aislado retenía `repository`. Cumple el criterio 13 | — | resuelto 30 jul |
| P | **Riesgo de diseño abierto:** los descendientes del propietario le sobreviven en Windows. Un Job Object con `KILL_ON_JOB_CLOSE` lo resolvería, pero mataría al propietario al morir el iniciador — lo contrario de lo que la Fase 8.5 demuestra. **Requiere decisión explícita antes del ensayo real** | Media (diseño) | `TrialProcessLauncher.start` |
| ~~M~~ | ~~Interferencia entre pruebas en la suite completa~~ **Desapareció al corregir K.** Reconfirmar con varias corridas | — | resuelto 30 jul |

**A y B explican mecánicamente el incidente de limpieza transitoria:** mientras un handle esté abierto, Windows no permite eliminar el directorio que lo contiene. Solo se alcanzan por rutas adversariales que la suite actual no ejercita, lo que explica que el fallo fuera intermitente.

### Ya resuelto correctamente

`_remove_stable_contents` (enumera y borra por handle, recurre en profundidad, rechaza reparse points, cierra en `finally`, sin globs); el reemplazo atómico ligado al handle de la raíz vía `_winfs.rename`; la guarda que impide `os.kill` en Windows.

### Cobertura de los 18 criterios adversariales

**9 cumplidos, 3 parciales (5, 6, 12), 6 sin prueba: criterios 4, 7, 8, 13, 14, 15.**

El criterio 14 («los handles se cierran en éxito y error») **fallaría hoy** por los hallazgos A y B.

Faltan pruebas de: comandos colocados en una carpeta sustituta no aceptados (4); temporal sustituido o convertido en reparse point rechazado (7); destino sustituido o convertido en reparse point rechazado (8); eliminación parcial reintentable de forma idempotente (13); handles cerrados en éxito y error (14); varias iteraciones consecutivas de creación, fallo y limpieza dejan cero temporales (15).

## 7. Secuencia de trabajo recomendada

~~1. Corregir **K**.~~ Hecho el 30 jul. Suite en verde: 676 correctas, 0 fallos.

1. Corregir **A y B**. Cambios pequeños de orden de operaciones; son defectos reales aunque no causaran ningún fallo observado.
2. Corregir **C**: derivar la identidad de la raíz del handle y comparar volumen **e** índice.
3. Corregir **D, F, G, L**.
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
