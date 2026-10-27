# ADR-0172 — Opciones directas: definición común, acción sin LLM

- **Estado:** propuesto (se acepta al mergear el PR 3 de SPEC-40, el núcleo del modelo; las seis decisiones abiertas de SPEC-40 se cierran en este ADR con el OK del dueño).
- **Fecha:** 2026-10-27
- **Contexto:** SPEC-40 (D-50, REQ-OPC-01 a REQ-OPC-03; HU-OPC-01 a HU-OPC-06). El borrador vive en `solvershub-docs`, `04-fase-1-competir/SPEC-40-opciones-directas/`.
- **Enmienda:** ADR-0108 §«v1 solo resuelve variables del cuerpo»: las plantillas con opciones llevan botones QUICK_REPLY con payload por envío. Ningún ADR aceptado se edita: la enmienda se registra al aceptarse este.
- **Usa:**
  - ADR-0031 (ciclo de vida de las plantillas de WhatsApp por WABA);
  - ADR-0108 (plantillas por evento: variables que arma el servidor, `clean_param`, ventana desconocida = cerrada);
  - ADR-0112 y su botón firmado `confirmar:` (molde de la firma y de la confirmación);
  - ADR-0114 (tablas de solo avance y `prevent_mutation()`);
  - ADR-0165 (salida estructurada del canal: las listas de salida se comparten);
  - ADR-0150 (envíos a segmentos: no se duplica la regla de consentimiento).

## Contexto

- Las plantillas de Meta (`message_templates`) se gestionan solo desde el panel y ninguna tool del agente las crea ni las envía; los avisos por evento las usan con variables del servidor (ADR-0108).
- Un botón entrante (`button_reply` o `list_reply`) llega al motor de procedimientos, pero no puede ejecutar una tool por sí mismo. Los quick replies de plantilla (`type:"button"`) hoy se descartan en `core/gateway/channels/whatsapp/inbound.py`.
- Cada aviso que ofrece una acción («Pagar», «Reagendar») obliga a pasar por el LLM: costo, latencia y riesgo de improvisar un argumento o un monto.
- El dueño aprobó el diseño (2026-10-27): una definición común que sale como mensaje interactivo dentro de la ventana de 24 h y como plantilla de Meta fuera de ella; la crea él desde el panel, no el agente; y cada opción ejecuta una tool de una lista permitida, sin LLM.

## Decisión

**Una «plantilla con opciones» es una definición versionada y validada del negocio, cuyas opciones ejecutan una tool permitida cuando el cliente las toca, sin LLM, con una respuesta fija.**

### 1. Modelo (migración 0112; `message_templates` no cambia)

Todas llevan `id`, `tenant_id`, RLS con `FORCE`, índices que empiezan por `tenant_id` y FKs **compuestas** `(tenant_id, …)` entre ellas y hacia `conversations`; entran en `purge_tenant` sin tocarla (descubre las tablas por `tenant_id`).

| Tabla | Qué guarda |
|---|---|
| `option_templates` | Clave única por negocio entre las vivas, borrador en `draft_definition`, número de versión vigente, estado (`draft`, `published`, `retired`), borrado lógico |
| `option_template_versions` | Una fila por versión **publicada**, inmutable: `definition` (jsonb validada con pydantic), `definition_hash`, FK compuesta nullable `(tenant_id, message_template_id)` a `message_templates` (que gana `UNIQUE(tenant_id, id)`; `ON DELETE SET NULL (message_template_id)`). Solo `message_template_id` (se fija al publicar en Meta) y `retired_at` pueden cambiar |
| `option_sends` | Un envío (solo INSERT: no se edita): plantilla, versión, conversación, vía (`interactive`, `template`, `text`), contexto que resolvió el servidor y `expires_at` |
| `option_activations` | Un toque: `UNIQUE(tenant_id, dedupe_key)`, `inbound_wamid` único, a lo más una pendiente por (envío, opción) y estados `pending_confirm`, `running`, `done`, `failed`, `cancelled`, `expired` |

`definition` contiene `body`, `footer`, `variables`, `exclusivo`, `ttl_horas` y entre 1 y 10 `opciones[]`. Cada opción lleva `id` estable (`[a-z0-9_]`, hasta 12), `titulo` (20 caracteres o menos), `accion {tool, args_fijos, args_contexto, confirmar}`, `respuesta_ok`, `respuesta_error` y `result_keys` (hasta 5).

**Inmutabilidad.** El borrador no vive en `option_template_versions`: la fila nace al publicar, así que toda fila es inmutable y se reutiliza `prevent_mutation('message_template_id', 'retired_at')` de ADR-0114 en vez de un trigger propio.

**Excepción a la invariante 8.** `option_template_versions`, `option_sends` y `option_activations` no llevan `deleted_at`: son historia de lo publicado y de lo ejecutado, de solo avance. Solo las borra `purge_tenant`. `app_runtime` no recibe `DELETE` en ninguna de las cuatro (el borrado lógico de `option_templates` es un `UPDATE`) ni `UPDATE` en `option_sends`.

**Datos personales.** `option_sends.context` guarda los valores que el servidor usó para armar el cuerpo (el mismo texto ya queda en `messages`) y `option_activations` no guarda argumentos ni resultados, solo `args_hash` y un `error_code`. La retención y la supresión por titular de esas filas siguen a las de la conversación; si el ADR-0164 pasa a aceptado y exige anonimizar columnas, `context` es la única candidata.

### 2. Mapeo a WhatsApp

- 3 opciones o menos: mensaje interactivo `button`. De 4 a 10: `list`.
- Plantilla de Meta: BODY con parámetros NAMED (`{cliente.nombre}` → `{{cliente_nombre}}`), FOOTER y botones QUICK_REPLY, con `event_notices.render_body`/`build_components` y `validate_template_components`. Dos rutas que dan el mismo nombre de parámetro (`a.b_c`, `a_b.c`) se rechazan en el lint.
- Cada versión publicada en Meta crea una plantilla nueva `<clave>_v<n>` (por el límite de ediciones de Meta); nunca se edita una aprobada.
- Sin botones en el canal (chat web, Instagram, correo): texto con la lista de opciones y **sin ejecución directa**.

### 3. Payload firmado

Mismo patrón HMAC de `motor._firma` y `payload_boton_confirmar`:
- `opt:<send_id>:<option_id>:<firma16>` para tocar una opción; `optc:` y `optn:` para confirmar y cancelar. El `send_id` va como uuid hex (32); el payload más largo mide 67 caracteres (tope fijado: 70).
- La firma es `HMAC-SHA256(clave, tipo|negocio|conversación|envío|opción|args_hash)` truncada a 16 hex. El **tipo va dentro del mensaje firmado** (un `optn:` no vale como `optc:`); en `optc:`/`optn:` entra además el `args_hash` de los argumentos resueltos que vio el cliente. La clave se deriva de `APP_SECRET_KEY` con separación de dominio; rotarla invalida los botones en vuelo, como ocurre con `confirmar:` y A2A.
- Como el payload no trae el `args_hash`, el resolver lo toma de la activación pendiente de (envío, opción): el índice parcial único garantiza que hay a lo más una.
- Los prefijos `opt:`, `optc:` y `optn:` se reservan en `core/kernel/procedures/linter.py` (disparador y `setBy: interactive:`), igual que `confirmar:`. El consumer atiende `opt*` antes del router, así que un KernelSpec con ese prefijo nunca lo vería.
- Un botón viejo ejecuta **su** versión mientras no venza. Si la versión está retirada, responde un texto fijo («ya no está disponible»).
- El tope de 256 caracteres del id interactivo de Meta **no está verificado** contra la doc primaria.

**Corrección al requerimiento original (payload de QUICK_REPLY).** El diseño parte de que el payload de un QUICK_REPLY no se fija al crear la plantilla: se manda **al enviar** (`components` con `sub_type: "quick_reply"` y `parameters: [{type: "payload", …}]`) y vuelve en el webhook como `type: "button"`. **Pendiente de verificar con una prueba real en el sandbox de Meta antes del PR de envío.** Si resultara fijo al crear, se reabre este punto (payload `optv:<version_id>:<option_id>` por versión y firma sin `send_id`).

### 4. Resolución del toque (sin LLM) — PR 4

- Se engancha en `core/orchestrator/consumer.py`, **después** de persistir el mensaje entrante y de las políticas de entrada, y **antes** de `entrada_sin_llm`. No depende de las banderas de procedimientos. Con la bandera del negocio apagada no actúa.
- Reclamo por fila: `UPDATE option_activations … WHERE state = 'pending_confirm' RETURNING`; el `dedupe_key` y el `inbound_wamid` evitan la doble ejecución.
- La tool corre con `_execute_tool_safely` y `idempotency_key = activation_id`, con un parámetro `ya_confirmada` para saltar el bloque de `requiresConfirmation`. La confirmación se decide con `confirmacion_efectiva` (definición, tool `sensible` o binding), nunca con el `confirmar` crudo.
- La respuesta es un **texto fijo** (`respuesta_ok` o `respuesta_error`) con `{ctx.x}` y `{result.k}` sanitizados con `clean_param` (el resultado de una tool declarativa viene de una API externa). Nunca silencio ni el LLM improvisando.
- Un worker que muere en `running` **nunca reejecuta solo**: el siguiente toque informa «sin confirmar resultado».
- Se vuelve a lintar al ejecutar: un procedimiento publicado después puede volver obsoleta una opción.

### 5. Autorización (fail-closed)

- Escribir la definición: `require("connections", write=True)` más `require_owner_or_master`. El agente **no** crea ni edita plantillas.
- Una tool es elegible solo si: declara `apta_para_opcion=True` (campo nuevo de `MCPTool`; las declarativas usan `"option_safe": true`, solo con el literal `true`); su pack está activo; su binding es **explícito** y está habilitado; y no pertenece a un procedimiento habilitado. Ninguna tool existente nace apta. Las declarativas que escriben (POST) nacen `sensible`. Las declarativas de un AgentSpec v3 no pueden marcarse aptas hasta que el contrato `declarativeTools` (congelado) lo permita.
- Confirmación obligatoria si la tool es `sensible` (campo nuevo), si el binding tiene `requiresConfirmation` o si la opción lo pide.
- Los argumentos salen **solo** de la definición (`args_fijos`) o del contexto que arma el servidor (`args_contexto`); nunca del texto del cliente ni del LLM (invariante 7, ADR-0108). El lint los valida contra el JSON Schema de la tool con un validador mínimo (`core/kernel/opciones/esquema.py`: el repo no valida hoy los argumentos del `tool_call` y no hay `jsonschema`); el tipo de un `args_contexto` se valida al resolver (`option_args_drift`).
- **Bandera por negocio, apagada por defecto, que solo enciende el master:** `tenants.settings["opciones_directas"] = {"habilitada": bool}`. Clave propia, no `settings.agent_os`, porque el registro de configuración exige que toda bandera de ese grupo dependa de `aos_runtime = v3` y las opciones no. `core/kernel/opciones/bandera.py` es el único escritor; la ruta master-only y la entrada en el registro llegan con la API. Límite: a diferencia de las `aos_*` (migración 0097) no hay función ni trigger en la base, y otros caminos que reescriben `settings` entero podrían reponer un `true` apagado; aceptable con la bandera apagada y sin consumidor, y se pasa a columna si hace falta.

### 6. Piezas de cableado que faltan en el core (PRs 1, 2, 4, 5 y 7)

- Parsear `type: "button"` entrante; listas en la salida; componentes de plantilla con payload; `send_options_to_conversation` (ventana abierta → interactivo; cerrada con plantilla `APPROVED` → plantilla; si no → texto de respaldo; ventana desconocida cuenta como cerrada).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| **Grant explícito por negocio** de cada tool para opciones | Duplica el binding, que ya es explícito por agente y por negocio, y crea una segunda lista que se desincroniza. La seguridad la dan la marca que declara el autor de la tool, el binding explícito y la bandera del master |
| **Entregar el resultado de la tool a un turno LLM** | Reintroduce el LLM que se quiere evitar: costo, latencia, no determinismo y riesgo de improvisar un monto o un link |
| **Extender `message_templates`** con columnas de opciones | La tabla es el espejo del ciclo de vida de Meta (ADR-0031). Una definición común es del negocio, tiene versiones inmutables y existe aunque nunca llegue a Meta |
| Un trigger de inmutabilidad condicional con el borrador como fila de `versions` | Obliga a lógica condicional propia; con el borrador en `option_templates` basta la función genérica de ADR-0114 |
| Payload `optc:<activation_id>` en vez de `optc:<send_id>:<option_id>` | Habría sido más corto, pero el payload del botón se firma al enviar y la activación aún no existe; el índice parcial único resuelve la ambigüedad |
| Fijar el payload al crear la plantilla | Según el diseño y a verificar en el sandbox, Meta no lo permite; además no ata el toque a un envío ni a una conversación |
| Una tool de creación para el agente | El dueño decidió que las crea él desde el panel |
| Confiar en el `id` o el título del botón sin firma | Cualquiera ejecutaría una opción de otra conversación |

## Consecuencias

**Positivas:** un toque ejecuta una tool con permisos, auditoría, idempotencia y confirmación, con cero invocaciones del LLM; una definición sirve las dos vías; el panel genera el formulario desde el JSON Schema del catálogo.

**Negativas:** cuatro tablas, una migración, una bandera y una superficie nueva por probar; cada versión pasa por la aprobación de Meta; los quick replies sin prefijo `opt` llegarán como texto al LLM en vez de descartarse (changelog del PR 1); un procedimiento publicado después puede volver obsoleta una opción; fuera del loop no hay release ni spec de turno (con v3 los bindings son por agente en foco: las fichas de tools del lint salen de UN AgentSpec, el del agente que envía); sin binding `_tool_denial` deja correr la tool, para opciones se exige binding explícito.

**Modos de falla:** firma inválida o conversación ajena → se descarta con log y métrica `bad_signature`; envío vencido o versión retirada → texto fijo; doble toque o reintento de Meta → una sola ejecución; tool denegada o argumentos fuera de schema → `respuesta_error` y log `option_args_drift`; worker muerto en `running` → no reejecuta; plantilla rechazada o pausada → degrada a texto; canal sin botones → texto sin ejecución directa.

**Observabilidad:** un log por toque (`tenant_id`, `send_id`, `option_id`, `tool`, `outcome`, `latency_ms`); métricas `option_taps_total{outcome}`, `option_tap_latency_seconds` y `option_tool_errors_total{tool}`; alerta si crece `bad_signature`.

**Riesgos abiertos:** el payload de QUICK_REPLY sin verificar; los límites de Meta sin verificar contra la doc primaria (25 caracteres del botón de plantilla, 10 botones, 256 del payload); colisión de números de migración con las ramas abiertas; los valores por defecto de reutilización (una vez por envío) y vencimiento (24 h interactivo, 7 días plantilla, hasta 30) son una recomendación hasta el OK del dueño.

**Qué haría que revisáramos esto:** la prueba real muestra que el payload se fija al crear la plantilla o que Meta lo recorta; `bad_signature` u `option_args_drift` crecen sostenidamente en test; tres o más negocios piden opciones con tools financieras o destructivas (hoy fuera de alcance); Meta cambia el modelo de botones.

## Impacto en el código

- `alembic/versions/0112_opciones_directas.py` y los modelos `OptionTemplate`, `OptionTemplateVersion`, `OptionSend`, `OptionActivation` en `core/db/models.py`.
- `core/kernel/opciones/`: `definicion.py`, `lint.py`, `esquema.py`, `mapeo.py`, `payload.py`, `bandera.py`.
- `core/mcp/types.py` (`apta_para_opcion`, `sensible`) y `core/mcp/declarative.py` (`option_safe`).
- `core/kernel/procedures/linter.py`: prefijos `opt:`, `optc:` y `optn:` reservados.
- Siguientes PRs: `inbound.py` (`type:"button"`), `sender.py`/`outbound.py`, `consumer.py` y `loop.py` (`ya_confirmada`), `core/api/routers/plantillas_opciones.py`, `core/gateway/meta_templates.py` y el panel. La APK no cambia.
