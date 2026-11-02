# ADR-0182 — Asistente por preguntas de la configuración: el guion vive en el registro y aplica lo que el dueño confirma

- **Estado:** aceptado (HU-CFG-10, SPEC-37, D-58 de solvershub-docs). El dueño aprobó el plan el 2026-11-02; el core se mergeó en agent-core#657 y el panel en solvershub-panel#189.
- **Fecha:** 2026-11-02
- **Contexto:** SPEC-37 ampliada (HU-CFG-08 a HU-CFG-11, rediseño de Configuración, ola 1). Hecho con el dueño el 2026-11-02.
- **Usa:**
  - ADR-0166 (registro de opciones: lo que se puede tocar, con rango, permiso y explicación);
  - ADR-0171 de creación de agentes (`0171-asistente-de-creacion-de-agentes.md`: el molde de guion con preguntas y opciones que traducen a configuración);
  - ADR-0117 (releases por conexión: la voz de una conexión `v3` sale de la release congelada);
  - ADR-0110 (presets: `apply_preset` y su regla de no pisar lo editado).
- **No enmienda ADR-0166.** Ese ADR dice que el registro es «una vista, no un almacén» y que una configuración conversacional exige un ADR propio. Este es ese ADR para un caso acotado: un asistente de preguntas fijas, sin LLM, que no guarda estado propio. El registro sigue sin guardar nada.

## Contexto

- El dueño tiene que entender cada campo de Configuración para dejar su negocio bien. Quiere responder pocas preguntas y que quede un ajuste predeterminado que luego pueda editar a mano.
- Ninguna capa traduce respuestas a `behavior_config`, `escalation_config`, columnas de `AppSettings` o la persona del spec. Hoy solo existen `apply_preset` (alta y CLI, silencioso, no expuesto al dueño; `core/presets/apply.py`) y el guion de plantillas de agente (`core/presets/guion.py`, atado a `PlantillaAgente`, que solo cambia procedimientos, largo y nombre).
- `tests/test_cfg02_registro_opciones.py:193-208` deja importar `core.config_registro` solo al router `settings.py` y al propio paquete, para que sus textos de interfaz no lleguen a un prompt (ADR-0166 §4).
- Con `tenants.aos_runtime = v3` el turno usa la voz de la release congelada (`core/agents/spec_de_release.py:112-119`: `ks.voice.*` del `KernelSpec` de la conexión), no la persona del AgentSpec v2 que escribe `PUT /admin/settings/identity`. Para ese negocio, escribir `persona_*` no cambia nada.
- `PUT /identity` hace dos escrituras no atómicas: `AppSettings` y luego `save_spec_if_changed` (`core/api/routers/settings.py:445-520`).

## Decisión

El guion «respuestas → claves del registro» se escribe en `core/config_registro/asistente.py`; el dueño ve una vista previa de todo el cambio y lo que confirma se escribe tal cual.

### 1. Dónde vive el guion

`core/config_registro/asistente.py`, **dentro** del paquete del registro:
- El guion produce claves del registro con valores validados por `config_registro.validar_valor` y defectos de `Opcion.por_defecto`; depende del paquete por construcción.
- Un paquete hermano (`core/config_asistente/`) obligaría a ampliar la lista blanca del test de importación, que existe justo para que haya un solo lugar con textos de interfaz. Dentro del paquete la lista blanca no cambia: el router `settings.py` ya lo importa.
- No se extiende `core/presets/guion.py` (atado a `PlantillaAgente`) ni `PresetAppSettings` (`extra="forbid"`, 4 claves): mezclaría «por agente» con «por negocio».

Forma: un guion declarativo por sección (modelo propio con `extra="forbid"`, como `Opcion`): preguntas con un id, un texto y opciones con su `ayuda` y las claves del registro que cambian. Las preguntas son de «una» opción o de «varias» (listas de textos). Al importar el módulo se valida que cada clave exista, sea editable por el dueño y que cada valor propuesto cumpla su lista cerrada o su rango. Qué claves son de voz no se declara aparte: se deriva del almacén (`spec:persona.*`, ver §6). Una función **pura** `resolver(seccion, respuestas, actual, aos_runtime) -> Propuesta` recibe los valores actuales y devuelve la vista previa completa: cada cambio con *actual → propuesto* y la marca «lo cambiaste a mano», lo omitido con su motivo, y una huella de lo propuesto. No consulta la base ni escribe.

### 2. Vista previa y regla de escritura

- **Todo o nada.** La vista previa muestra todos los cambios y se confirman o se descartan juntos. El dueño afina cada campo después en el formulario manual. No hay confirmación por cambio.
- **El asistente aplica lo que el dueño confirma.** No hereda de `apply_preset` la regla «no pisar lo editado»: esa regla es para presets silenciosos (alta) y aquí el dueño decide con la vista previa a la vista. Por cada cambio se muestra *valor actual → valor propuesto* y, cuando el actual difiere del defecto de la opción, la marca «lo cambiaste a mano».
- **Lo no tocado se conserva.** Las claves que el guion no menciona no se modifican. Una opción con `depende_de` que no se cumpla con el estado resultante (propuesto sobre actual) se omite de la propuesta y la vista previa dice por qué (por ejemplo, el mensaje fuera de horario sin franjas).
- **El servidor recalcula.** `aplicar` recibe las respuestas, no un diff del cliente: recalcula la propuesta y la valida. Si los valores actuales cambiaron desde la vista previa (huella distinta), responde 409 como el `concurrent_edit` de ADR-0173 y no escribe.
- **Auditoría.** Deja una fila en `audit_log` con quién, la sección y el antes/después, como `PUT /bajas`.

### 3. Endpoints

Dos, no tres:
- El guion viaja dentro de `GET /api/v1/admin/settings/catalog` (campo nuevo `asistente`, ya sin las preguntas de voz si el negocio es `v3`). La huella `version` del catálogo cambia, y también con el runtime; los clientes que no conocen el campo lo ignoran. Inferir las respuestas iniciales desde los valores actuales queda para una ola posterior: la vista previa ya muestra *actual → propuesto*.
- `POST /api/v1/admin/settings/asistente/{seccion}/vista-previa` calcula el diff y no escribe; exige `require("settings")`.
- `POST /api/v1/admin/settings/asistente/{seccion}/aplicar` escribe; exige `require("settings", write=True)` (permisos como `settings.py:445-448`).
- Ambos son del negocio del principal (`tenant_scope`), validan la sección y las respuestas (422 por pregunta) y se documentan en `docs/openapi.json`.

### 4. Escritura atómica

`aplicar` **no** reutiliza `save_identity` a ciegas. Escribe en una sola sesión y transacción las columnas de `AppSettings`, `behavior_config`/`escalation_config` y, cuando aplica, la versión del spec con la persona. Si algo falla no queda nada escrito. Los `persona_*` son `almacen="spec:persona.*"`, no columnas: esa parte pasa por `save_spec_version`, que gana un parámetro opcional `session` para guardar dentro de la transacción de quien llama (sin él se comporta como siempre). `aplicar` lee la fila activa del spec antes de calcular y la pasa como `base_id`: si otra edición del agente confirma una versión en medio, responde 409 en vez de pisarla.

### 5. Textos como datos, nunca al prompt

Las preguntas, las ayudas y la explicación del guion son texto de interfaz, como las etiquetas de ADR-0166 §4: **nunca entran a un prompt** (invariante 3). Se mantiene la prueba de que el runtime de turnos no importa `core.config_registro` (que cubre este módulo). Y el asistente **no copia textos por defecto** a los datos del negocio:
- «Avisar fuera de horario» (modo `bloquea`) escribe `out_of_hours_message = ""`. El aviso por defecto vive en el playbook de kernel `fuera_de_horario` (`core/kernel/fuera_de_horario.py`) y viaja con el binario; copiarlo a la base lo congelaría y lo sacaría del control de evals.
- Si el dueño tenía un mensaje propio, la vista previa lo muestra como cambio (*texto actual → vacío*) con la marca «lo cambiaste a mano»: lo pierde solo si confirma.

### 6. Negocios con runtime `v3`

Las claves de persona (tono, calidez, emojis, largo máximo y frases prohibidas) **no se ofrecen** cuando `tenants.aos_runtime = v3`, porque la voz sale de la release congelada y un cambio quedaría inerte mientras la vista previa promete lo contrario. En el guion de la ola 1 eso quita las preguntas de tono, emojis y frases prohibidas y deja la de largo escribiendo solo `split_style`, sin `persona_max_length`. La sección dice «La voz de este negocio se cambia al publicar una versión del agente» y enlaza a Agentes. El resto (horario, modo fuera de horario, escalamiento, `debounce_mode`, `split_style`) sí aplica en caliente. `resolver` recibe el runtime del negocio: el catálogo no ofrece las preguntas solo de voz y, si llega una respuesta a una pregunta mixta, las claves de voz salen de la propuesta como «omitidas» con el motivo (no hay error: lo que se escribiría es lo que muestra la vista previa).

**Pendiente (no se decide aquí):** cómo cambiar la voz de una conexión `v3` desde Configuración. Hoy solo cambia con una versión nueva del agente `core` de la conexión y su release (ADR-0117). Requiere su propio ADR (¿Configuración crea una versión y una release candidata? ¿pasa por el gate pass^k?) y es una ola posterior.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Paquete hermano `core/config_asistente/` | Hay que ampliar la lista blanca del test de importación del registro; el guion depende del registro por construcción. |
| Extender `core/presets/guion.py` o `PresetAppSettings` | Atados a la plantilla de agente y a 4 claves; mezclan «por agente» con «por negocio». |
| Lógica en el panel (`lib/asistente.ts`) | La APK la tendría que duplicar y los textos se desfasarían (ADR-0166 §4). |
| Reusar la regla «no pisar lo editado» de `apply_preset` | El dueño vería una vista previa que no se cumple; para un cambio confirmado, lo no aplicado es una sorpresa. |
| Confirmar cambio por cambio | UI y casos de borde (dependencias entre respuestas) sin valor: el formulario manual ya permite afinar. |
| Llamar a `PUT /identity` desde `aplicar` | Dos escrituras no atómicas: un fallo deja el spec y las columnas en estados distintos. |
| Mostrar las preguntas de persona en `v3` con un aviso | La vista previa prometería cambios que no surten efecto. |
| Que el asistente escriba el texto por defecto fuera de horario | Congela en la base un texto que hoy se cambia con el binario. |

## Consecuencias

**Positivas:** el dueño llega a un ajuste sano con 9 preguntas en dos secciones (cuatro de voz y cinco de atención) y puede editar a mano después; el guion se prueba como función pura; una pregunta nueva nace en el panel y en la APK; el registro sigue siendo una vista (el guion no guarda estado); la lista blanca de importación no cambia.

**Negativas / lo que aceptamos pagar:**
- Hay un escritor más de las mismas claves (`aplicar`) además de los seis que el inventario de SPEC-37 contó; mitiga que valida con el registro y escribe en una transacción.
- El dueño puede pisar con el asistente lo que había afinado a mano; lo acota la vista previa con la marca «lo cambiaste a mano» y que sea todo o nada.
- Los negocios `v3` no pueden cambiar la voz desde Configuración hasta que se resuelva el pendiente de §6.
- Los textos del guion son más texto de interfaz en Python del core; cada cambio de redacción exige un despliegue.
- El onboarding (`lib/onboarding.ts` del panel) mantiene sus propias traducciones de clics a opciones; unificarlas con este guion queda para una ola posterior.

**Qué haría que revisáramos esto:**
- Que aparezca un segundo escritor del guion (por ejemplo un agente que configure) o que `aplicar` necesite reglas distintas por sección: entonces el guion se promueve a su propio paquete con su propia lista blanca.
- Que `v3` deje de ser un runtime que solo enciende el master y pase a ser el del alta: la voz desde Configuración deja de ser una limitación menor y su ADR sube de prioridad.
- Que las preguntas necesiten traducción o variar por negocio.

## Impacto en el código

- `core/config_registro/asistente.py` (nuevo: guion, `resolver`, composición de la vista previa) y `core/config_registro/__init__.py`.
- `core/api/routers/settings.py`: campo `asistente` en `GET /catalog` y los dos `POST /asistente/{seccion}/...`; `docs/openapi.json`.
- `core/agents/spec.py`: `save_spec_version(..., session=)`. La escritura transaccional (`aplicar`) vive en el router de configuración, junto a `_escribir_opcion`.
- Pruebas: unidad de `resolver` (cada respuesta → claves esperadas, valores dentro de `RANGOS`, `depende_de`, `v3`), la de importación del registro sin cambios, escritura atómica, permisos (403 sin `settings`/`write`) y aislamiento multitenant; `auditor-multitenant` al cerrar.
- `changelog.d/2026-11-02-cfg-asistente-configuracion.md` y la fila del ADR en `docs/adr/README.md`.
