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
- **Bandera por negocio, apagada por defecto, que solo enciende el master:** `tenants.settings["opciones_directas"] = {"habilitada": bool}`. Clave propia, no `settings.agent_os`, porque el registro de configuración exige que toda bandera de ese grupo dependa de `aos_runtime = v3` y las opciones no. `core/kernel/opciones/bandera.py` es el único escritor; la ruta master-only llegó en el PR 10a (la entrada en el registro de configuración se descartó, ver su adenda). Límite: a diferencia de las `aos_*` (migración 0097) no hay función ni trigger en la base, y otros caminos que reescriben `settings` entero podrían reponer un `true` apagado; aceptable con la bandera apagada y sin consumidor, y se pasa a columna si hace falta.

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

## Adenda (2026-10-27) · PR 4: el resolver del toque

Decisiones que el ADR dejaba abiertas y cómo quedaron:

- **Entorno fuera del loop (punto frágil, validado).** `core/orchestrator/opciones_toque.py` abre la ranura de la release (`release_vigente.abrir/cerrar`), resuelve `load_active_spec` → `fijar_release_del_turno(texto="")` → `spec_del_turno`, igual que `con_release`, y la cierra al terminar (también si falla). `tests/integration/test_opciones_toque_entorno.py` lo prueba con una tool real registrada en el host: en legacy corre con el binding del AgentSpec; con `aos_runtime = v3` corre con el binding de la versión congelada del agente en foco (`ToolContext.agent_id` y `write_namespaces` presentes) y se deniega si ese binding está deshabilitado. Un toque asigna release y dueño de la conversación (asignación sticky) pero **no enruta**: `fijar_release_del_turno(enrutar=False)` no corre el router ni consume (GETDEL) el payload de botón de procedimiento que espera un turno diferido, ni reanuda pilas; un toque no es un mensaje. Con v3, un binding **ausente** en el AgentSpec no sirve de «sin permiso»: la conversión a v3 (HU-AOS-12) crea un binding por cada acción ofrecida sin binding y la release ya lo trae explícito; el permiso explícito se decide al migrar, no al tocar.
- **Elegibilidad al ejecutar = la del lint.** `lint.motivos_de_rechazo` es la regla única (apta, pack activo, binding explícito y habilitado, no gobernada por procedimiento); la usan el lint al guardar y el resolver al tocar. No se usa `_tool_denial` como única barrera porque deja correr una tool sin binding; se llama además para respetar `maxCallsPerConversation` y los scopes del loop.
- **Confirmación.** `confirmacion_efectiva` decide; el cliente confirma con `optc:` (firmado con el hash de los argumentos que vio) y cancela con `optn:`. La confirmación vence a los 15 minutos. `_execute_tool_safely(..., ya_confirmada=True)` salta solo el bloque de `requiresConfirmation`; la confirmación propia del loop (`pending_confirmation`) nunca se deja en la conversación. Si el canal no puede mostrar los botones (no es WhatsApp o no cabe en los topes de Meta) no se pide confirmar a ciegas: se avisa que no se hizo nada.
- **Reclamo y reintentos.** La exclusividad y el doble toque se resuelven con `UNIQUE(tenant_id, dedupe_key)` (`INSERT … ON CONFLICT DO NOTHING`); cancelar o vencer una confirmación libera la clave. `running` de más de 60 s responde «sin confirmar» y no se reejecuta. Un segundo toque sobre una opción ya hecha solo repite su respuesta si esta no cita `{result.k}` (la tabla no guarda resultados).
- **Respuestas.** Siempre un texto fijo por la cola normal: el de la opción (`respuesta_ok`/`respuesta_error`) o, cuando falta un valor citado, un genérico del playbook del kernel `opciones_directas.md` (invariante 3). Los textos de sistema no los reemplaza ningún negocio.
- **Descartes.** Una firma inválida, un envío inexistente o de otra conversación se descartan con warning y métrica, sin responder (no confirmar al atacante que el envío existe). Las lecturas del envío van con el tenant explícito además de la RLS.
- **Límites conocidos.** Una conversación cerrada descarta el toque (el consumer no reabre conversaciones por un botón). Las tools gobernadas por un procedimiento se detectan con `registro.disponibles` sin mirar las banderas de procedimientos del negocio (fail-closed). Las métricas son OTel sin exportador configurado todavía. La auditoría de la ejecución de la tool conserva sus argumentos como cualquier tool; la auditoría `option_tap` solo lleva ids y claves.
- **Confirmación sin botones.** Si el canal no puede mostrar los botones (no es WhatsApp, ventana cerrada, proveedor que no es Meta, fuera de topes) la confirmación no se pide: `send_interactive_to_conversation(..., sin_degradar=True)` devuelve False en vez de mandar el texto de respaldo (un texto no confirma nada), la activación pendiente se cierra como `cancelled` (`confirmacion_no_disponible`) para no retener la opción —en un envío exclusivo, todo el envío— y el cliente recibe el aviso de que no se hizo nada.
- **El toque no deja decisión en el ledger del kernel.** No llama a `decidir`, así que no escribe `core_decisions` ni `turn_outcomes`; se observa por el log `option_tap`, la métrica y `audit_log` (`option_tap`). Es el mismo límite que la respuesta fija de media entrante. Si el tablero de fiabilidad (HU-AOS-29) los quiere, hace falta un `outcome` nuevo (CHECK de `core_decisions`) y una adenda al ADR-0112: queda fuera de este PR.
- **Qué no llega al resolver.** Si `politicas_de_entrada` corta antes (conversación escalada, bot pausado, fuera de horario), el toque se pierde sin activación; el cliente recibe lo que esa política responda. Un payload con firma inválida, de otro negocio o conversación (incluida una conversación nueva tras cerrarse la anterior, o la rotación de `APP_SECRET_KEY`) se descarta sin responder, por decisión de la HU (warning y métrica `bad_signature`), no con un texto fijo.
- **La ejecución de la tool es una ejecución normal.** Se audita como `tool:<name>`/`execute` (solo claves y hash de argumentos), consume `maxCallsPerConversation` del binding y cuenta para los guardrails que miran las tools ejecutadas. No hay `asyncio.timeout` propio: una tool lenta deja la activación en `running`, y pasados 60 s un nuevo toque responde «sin confirmar» sin reejecutar. `failed` es final (una vez por envío) y no hay barrido de `running` colgados: queda como mejora (métrica por antigüedad).
- **Estático.** `ya_confirmada=True` solo aparece en `opciones_toque.py` (test) y el kernel de opciones no importa `core.orchestrator` (contrato `opciones-sin-orquestador` de import-linter).

## Adenda (2026-10-27) · PR 5: el envío

Decisiones que el ADR dejaba abiertas o que el envío cambia respecto a lo escrito arriba:

- **Cascada sin texto fuera de ventana (cambia §6 y «Modos de falla»).** El ADR decía «si no hay plantilla → texto de respaldo» y «plantilla rechazada o pausada → degrada a texto». En WhatsApp con la ventana de 24 h cerrada (o desconocida, que cuenta como cerrada, ADR-0108) un texto libre lo rechaza Meta (131047) y terminaría en la DLQ, así que NO se intenta: queda un mensaje `failed` con `sin_plantilla` (como los avisos por evento, ADR-0108) y ninguna opción sale. El texto numerado queda solo para canales sin botones (web, WhatsApp Web, Messenger, Instagram, correo, WhatsApp que no es de Meta) y para el interactivo que no cabe en los topes de Meta, y en esos casos sin ejecución directa. Dentro de un turno del agente la ventana está siempre abierta (el inbound de Meta la renueva), así que la rama de «sin plantilla» solo aplica a envíos fuera de un turno (avisos, segmentos).
- **Contraparte utilizable.** La plantilla de Meta de la versión debe estar `APPROVED`, ser de la WABA de la conversación (ADR-0115 §2) y ser **compatible**: mismos botones QUICK_REPLY en el mismo orden y mismos textos, y las mismas variables del cuerpo, porque el payload de cada botón va **por posición**. Si no, `sin_plantilla` con el motivo (`plantilla_no_aprobada`, `plantilla_de_otra_cuenta`, `plantilla_distinta_de_la_version`, `version_sin_plantilla_de_meta`).
- **Payload del QUICK_REPLY: sigue SIN verificar con Meta.** El ADR exigía verificarlo «antes del PR de envío». El PR se mergea sin esa verificación porque nada sale mientras `opciones_directas.habilitada` esté apagada (por defecto, y solo la fija el master). La forma supuesta (`index` como texto, `sub_type: "quick_reply"`, `parameters: [{type: "payload"}]`, igual que el ejemplo de plantillas interactivas de la documentación pública) vive en `envio.componentes_con_payload`, único punto de construcción. La prueba contra el sandbox (`tests/test_opciones_envio_meta_sandbox.py`, solo con `OPCIONES_META_SANDBOX=1`) es la **puerta para encender la bandera en cualquier negocio**: hay que correrla antes y registrarla en el checklist del PR 7. Si el sandbox muestra que el payload se fija al crear la plantilla, hay que reabrir el diseño del §3 (`optv:` sin `send_id`), no solo esa función: de ella dependen el `send_id` dentro del payload y el orden alta-antes-de-encolar.
- **Alta antes de encolar y el huérfano aceptado.** El `option_sends` se crea antes de encolar, para que cada payload lleve un `send_id` existente. `option_sends` es de solo INSERT para `app_runtime` (migración 0112), así que si algo falla DESPUÉS del alta (la cola rechaza por una carrera de ventana o de pausa) queda una fila sin mensaje. Es inerte: nadie recibió el payload, el resolver exige la firma de negocio y conversación y `expires_at` la acota. Se minimiza con las comprobaciones previas al alta (bandera, versión publicada, contexto completo, pausa de la conexión, pausa por uso de la API de Meta, topes del interactivo, plantilla utilizable) y se deja constancia (`enqueue_failed`). Marcarla como vencida exigiría una migración que dé UPDATE: queda como opción del PR 6 si el dueño la quiere.
- **TTL único.** `expires_at` = `ttl_horas` de la definición (1..720 h) para las tres vías. El ADR proponía 24 h en el interactivo y 7 días en la plantilla; la definición no distingue un valor explícito de su default. **Decisión del dueño pendiente:** en la vía de plantilla (ventana cerrada) el cliente suele contestar pasadas las 24 h, y el toque llega como «envío vencido»; si se quiere el default de 7 días para esa vía, se cambia en `envio.expira_en`.
- **Idempotencia.** Con `clave_idempotencia`, `send_id = uuid5(negocio, conversación, plantilla, versión, clave)` y `client_message_id = opt_send:<send_id>`. Un mensaje `failed` no cuenta como enviado (el reintento puede volver a intentarlo); una fila `option_sends` previa solo se reutiliza si es el mismo envío (plantilla, versión, vía y contexto). La tool usa `<conversación>:<tool_call.id>`. Laguna conocida: dos llamadas concurrentes con la misma clave pueden chocar en el índice único de `messages` y la segunda devolver `internal_error`; un proveedor de LLM con ids de tool_call posicionales repetidos entre turnos de la misma conversación daría `already_sent` sin enviar.
- **La tool `enviar_plantilla_opciones(clave)` es una superficie nueva.** El ADR rechazó la tool de creación; esta es solo de envío. Cualquier agente del negocio puede enviar cualquier plantilla **publicada** del negocio a la conversación del turno (la conversación sale del `ToolContext`, no de los argumentos; no recibe `datos`), sin binding por plantilla. Se ofrece solo con la bandera encendida (`connection_predicate`, reevaluado a mitad de turno por `_tool_denial`, y el envío vuelve a comprobar la bandera) y NO lleva `apta_para_opcion`, así que el lint la rechaza como acción de una opción (evita bucles envío→toque→envío). Riesgo aceptado: no hay tope por conversación y plantilla; el LLM podría reenviar la misma plantilla varias veces y cada envío tendría sus propias opciones vivas. Se mitiga con `maxCallsPerConversation` del binding si el negocio lo define.
- **Contexto.** `cliente.nombre` (nombre de perfil del canal, que el agente puede reescribir con `update_customer_profile`) se acota a **60** como en los demás consumidores (ADR-0108). `datos` lo arma el código del servidor que dispara el envío; hoy no tiene llamador y la lista permitida de rutas por origen (como `EVENTOS`) se fija con los PR 6 y 7.
- **Observabilidad.** Log `option_send` por envío, métricas `option_sends_total{canal,via}` y `option_send_failures_total{canal,outcome}` (OTel sin exportador, igual que el toque) y auditoría `option_send` con ids y claves, sin valores; una clave inválida se registra como `invalid`.
- **Lo que queda fuera.** El `sin_plantilla` de una opción cuenta en `GET /settings/message-templates/missing` junto con los de eventos y su remedio (publicar la versión en Meta) no es el de un evento: distinguirlos en el panel es del PR 7. El texto de respaldo numerado sale por `send_to_conversation` y puede partirse en varias burbujas según el `split_style` del negocio. La ventana desconocida cuenta como cerrada aquí, pero `sender.py` la trata como abierta en otros envíos (preexistente): unificar con un helper cuando haya envíos sin inbound. Falta el eval golden de la tool (exige LLM real: espera el OK del dueño).

## Adenda (2026-10-27) · PR 6: la API admin

Decisiones que el ADR dejaba abiertas y cómo quedaron (`core/api/routers/plantillas_opciones.py`, reglas en `core/admin/plantillas_opciones_service.py`):

- **Ruta y autorización.** `/api/v1/plantillas-opciones` (sin `/admin/`, como pide la HU). Lectura con `require("connections")`; escritura con `require("connections", write=True)` y `require_owner_or_master`. El **permiso va primero**: un colaborador sin la sección recibe 403 aunque el negocio no tenga la bandera (no se filtra si el módulo está encendido). Con permiso y **sin la bandera**, TODA ruta responde 404 con código propio `opciones_directas_no_habilitadas` (el panel lo distingue de «no existe» para ocultar el módulo) y no lee ni escribe nada. Una sesión sin negocio activo (cuenta sin negocios) recibe 400 `no_tenant`. La clave de otro negocio es el mismo 404 `not_found` que una inexistente; toda consulta filtra por `tenant_id` además de la RLS. Una clave con forma inválida en la ruta es 404, no 422.
- **El contexto del lint es el del AgentSpec activo, no el del agente que envía (límite conocido).** La API no tiene conversación ni release, así que las fichas de las tools salen de `leer_spec_activo(tenant)` (no `load_active_spec`, que siembra el default: un GET no escribe) con las mismas piezas que el resolver (`lint.fichas_de_tools`, `lint.motivos_de_rechazo`). Con `aos_runtime = v3` los bindings reales son los de la versión congelada del agente en foco, y el AgentSpec v2 puede divergir: la API puede aceptar una opción cuya tool el agente que envía no tiene, o rechazar una que sí tiene. No es un hueco de seguridad (el resolver vuelve a lintar al tocar y deniega sin binding explícito), solo una falsa aceptación o un falso rechazo al guardar. Sin AgentSpec activo el catálogo sale vacío.
- **Catálogo de acciones.** Solo las tools para las que `motivos_de_rechazo` no da motivos (marca de su autor, pack activo, binding explícito y habilitado, sin gobernar por un procedimiento), con `parameters` (JSON Schema), `sensible` y `confirmacion_obligatoria` (`confirmacion_efectiva`). Nunca incluye `enviar_plantilla_opciones` (no es apta). Es por negocio: los packs deshabilitados, el AgentSpec y las declarativas son los de la sesión.
- **Borradores.** El borrador se guarda con la **forma** validada (`Definicion`, y 100 KB como tope del JSON), aunque esté incompleto: el lint completo corre en `validar`, `vista-previa` y `publicar`. Las respuestas con una definición la tipan como `Definicion` en el OpenAPI para que el panel genere el tipo; las peticiones la reciben como objeto libre para devolver hallazgos con `path` en vez de un 422 genérico. Última escritura gana (sin ETag); tope de 100 plantillas vivas por negocio (409 `limite_de_plantillas`). `nombre` ≤ 120.
- **Publicar.** Bajo `SELECT … FOR UPDATE` de la plantilla: lint (errores → 422 `definicion_con_errores` con los hallazgos en `error.details.hallazgos`, sin cambiar nada), `version_n = max + 1`, hash SHA-256 de la forma canónica (`definicion.hash_de`: independiente del orden de las claves y de los valores por defecto). Si el borrador es igual a la versión vigente de una plantilla publicada: 409 `sin_cambios`; dos publicaciones simultáneas del mismo borrador dan una versión y un 409. **No retira la versión anterior**: un botón viejo ejecuta su versión mientras no venza (§3). Los `warning` no impiden publicar y viajan en la respuesta. **Gancho a Meta:** `gancho_tras_publicar` es un no-op documentado, llamado tras el commit; la respuesta lleva `message_template_id: null`. No hay bandera en la petición que prometa algo que no hace: el PR 7 añade el campo y llena el gancho.
- **Retirar y borrar.** `retirar` (solo publicada) marca `retired_at` en **todas** las versiones sin retirar (lo único que deja cambiar el trigger junto con `message_template_id`) y deja la plantilla en `retired`: ya no se envía y el toque de un botón en vuelo responde el texto fijo. Un borrador se puede volver a publicar después (versión `n + 1`). `DELETE` es el borrado lógico y solo vale para lo que nunca se publicó o ya se retiró (409 `plantilla_publicada` si no); libera la clave y conserva la historia.
- **Vista previa.** Sin red ni base de datos de envíos: valores de muestra (`ejemplos` del panel o `Ana`/`Mi negocio`/`ejemplo`), ids de relleno con el largo de un payload real (`cabe_en_whatsapp` es fiel) y los componentes de Meta con los mismos ejemplos. Con errores en la definición: 422 con los hallazgos.
- **`enviar-prueba`: un mensaje REAL a un cliente real.** Solo a una conversación del propio negocio (404 `conversacion_no_encontrada` si es de otro o no existe; un número libre es 422 por no ser un UUID), por `sender.send_options_to_conversation` (bandera, pausa, ventana, plantillas y auditoría `option_send` con actor `admin:<id>`). `clave_idempotencia` es obligatoria (8 a 64 caracteres) y repetirla devuelve 200 `ya_enviado`. Mapeo: no publicada → 409 `no_publicada`; fuera de ventana sin plantilla de Meta utilizable → 409 `sin_plantilla` con el motivo en `details`; pausa → 409 `pausada`; contexto incompleto → 422; falla de la cola o interna → 502 `no_se_pudo_enviar`. **Riesgo aceptado:** la conversación puede ser la de un cliente real y no hay tope por conversación y plantilla (cada clave nueva es otro envío); el panel debe confirmar antes de enviar y el ledger permite rastrear quién lo hizo. Si se quiere restringir a conversaciones de prueba o poner un tope, es una decisión del dueño.
- **Lista permitida de rutas para `datos` (pendiente del PR 5).** `contexto.RUTAS_DE_DATOS`: lista cerrada, **vacía** a propósito. Los llamadores actuales (la tool del agente y `enviar-prueba`) no pasan `datos`, y una ruta fuera de la lista es `DatosInvalidos` al enviar y `variable_fuera_de_catalogo` en el lint, así que ninguna definición puede citar un dato que nadie sepa resolver. Cada origen que los necesite (un aviso por evento, un segmento) agrega SUS rutas en un cambio revisado, como `EVENTOS` lo hace por evento. El catálogo de variables (`catalogo_de_variables`) es la base más esa lista y marca el `origen` (`conversacion` | `servidor`).
- **`/settings/message-templates/missing` (pendiente del PR 5).** El tipo lo da el `client_message_id` del mensaje fallido (`opt_send:<id>:f…`), no su texto: `MissingTemplateItem.tipo` (`evento` | `opcion`) y `clave` (sacada de «opciones:<clave>»), más `total_eventos_7d` y `total_opciones_7d`. Campos aditivos con valor por defecto: el panel actual no se rompe. El remedio de una opción es publicar la versión en Meta (PR 7), no el mapeo de eventos.
- **Prueba del sandbox de Meta.** Documentada en `docs/runbooks/opciones-directas.md` como paso previo a encender la bandera en cualquier negocio, con el paso manual del webhook que la prueba automática no cubre.
- **La ruta master que fija la bandera NO llega con este PR (cambia §5).** §5 decía que llegaba con la API. Se difiere al PR de publicación a Meta (PR 7): este PR solo LEE la bandera (`core/api/routers/plantillas_opciones.py` entra a la lista cerrada de lectores de `test_opciones_esquema_y_bandera.py`) y encender un negocio sigue siendo una operación del master/DBA con el OK del dueño, descrita en el runbook. La entrada en el registro de configuración (CFG-02) va con esa ruta.
- **Renombre.** La clase `Opcion` de la definición pasa a `OpcionDeLaPlantilla`: el esquema OpenAPI `Opcion` ya era del registro de configuración y FastAPI habría renombrado ambos a `core__…__Opcion`, rompiendo los tipos del panel. Los esquemas de la API llevan el prefijo `Opciones…` por la misma razón.
- **Observabilidad.** Cada escritura deja `audit_log` (`option_template_create|draft|publish|retire|delete`, actor `admin:<id>`, solo ids y claves, nunca el contenido de la definición).

## Adenda (2026-10-27) · PR 7: publicar la versión en Meta

Decisiones que el ADR dejaba abiertas y cómo quedaron:

- **Reintento seguro sin tabla ni migración.** `message_templates` ya tiene `UNIQUE (waba_account_id, name, language)` y
  admite `DRAFT`: antes del POST se RESERVA la fila (`DRAFT`, sin `meta_template_id`, con la categoría y los componentes)
  y se liga `option_template_versions.message_template_id` (columna que `prevent_mutation` deja mutar) en la misma
  transacción. Esa fila es la clave de idempotencia y el estado persistido. Una fila `DRAFT` sin id que ya existía al
  llamar es un intento de resultado desconocido (corte, timeout, caída del worker): se pregunta a Meta por ESE nombre
  (`GET …/message_templates?name=`, filtro documentado y no verificado desde aquí; se vuelve a comparar nombre e
  idioma de lo que responde) y, si ya la tiene, se adopta su id y estado sin otro POST. No se usa `sync_templates`
  para conciliar: pagina toda la WABA y reescribe `components` y `category` de todas las filas.
- **El completado no pisa al webhook.** Entre la respuesta de Meta y el guardado local puede llegar el webhook de estado;
  sin `meta_template_id` en la fila se descarta (`template_webhook_sin_fila_del_negocio`). El guardado solo completa una
  fila que sigue sin id y solo avanza el estado si sigue en `DRAFT`. Si el aviso se perdió en esa ventana (milisegundos)
  lo corrige el sync horario.
- **Colisión de nombre.** La clave es única solo entre las vivas: borrar y recrear una clave vuelve a dar `<clave>_v1`.
  Si ya existe en la WABA una plantilla de ese nombre que no está ligada a esta versión, solo se adopta si dice
  EXACTAMENTE lo mismo (cuerpo, pie, botones y variables) y es de la categoría pedida; si no, `name_taken` (con motivo `categoria_distinta` en el segundo caso) y no se toca nada. El índice del vínculo no es único: dos versiones de una clave borrada y recreada pueden quedar ligadas a la misma plantilla de Meta cuando dicen lo mismo (inocuo: es la misma plantilla). La salida del dueño es
  publicar una versión nueva (Meta además no deja reusar un nombre borrado durante unas semanas).
- **Categoría.** La elige el dueño: `UTILITY` o `MARKETING` (`AUTHENTICATION` se rechaza). Queda en la fila reservada
  antes de llamar a Meta y se lee de `message_templates` (no de la publicación) porque `template_category_update`
  puede cambiarla. Se manda `allow_category_change=False`: Meta rechaza en vez de recategorizar en silencio, porque el
  costo cambia con la categoría. Un rechazo por eso llega como `REJECTED` con su motivo.
- **Idioma.** La definición no lo tiene: parámetro con default `es` (el de la creación manual del panel). Los
  `ejemplos` de los parámetros NAMED los da quien publica; sin ellos, `ejemplo` (Meta suele rechazarlo): el panel debe
  pedirlos. Deben ser constantes neutras, nunca datos de un cliente.
- **La bandera.** El servicio `publicar_en_meta` no la mira: publicar en Meta no envía nada a ningún cliente. La puerta es
  la API de HU-OPC-05, que responde 404 `opciones_directas_no_habilitadas` en todas sus rutas (también en las de Meta) con la
  bandera apagada, y los envíos y toques siguen gobernados por `opciones_directas.habilitada`. Quién puede publicar lo decide
  la API: `connections` con escritura y dueño o master (§5).
- **Una versión, una plantilla de Meta, una WABA.** El vínculo es una sola columna y el envío exige que la plantilla sea de
  la WABA de la conversación: un negocio con dos WABAs envía por la de la versión (`plantilla_de_otra_cuenta` en la
  otra) y publicarla en otra conexión da `other_account`. Aceptable con pocos negocios con dos WABAs; si se necesita
  una versión en varias WABAs hace falta una relación N:M (migración) y es otro cambio.
- **Cupo.** Se mira justo antes del POST (una versión que ya está en Meta no gasta cupo y su reintento no se topa con la cuenta llena). Cada versión gasta una plantilla de las que Meta permite por cuenta. Antes de llamar se mira
  `message_template_count`/`message_template_limit` (contadores del último sync): lleno → `template_quota_full`. El panel
  debe mostrar cuáles versiones siguen vigentes para no llenar la WABA (retirar una versión no borra su plantilla).
- **Si Meta falla, la versión publicada no se revierte.** Queda `published` con su vínculo y el error en
  `message_templates.sync_error` (visible en el panel); el reintento completa la misma fila. `network_error`, `rate_limited`,
  `in_progress` e `internal_error` son reintentables (un 5xx de Meta o una respuesta sin id cuentan como `network_error`: Meta pudo
  haberla creado y el reintento concilia por nombre); un rechazo de Meta, una definición inválida, `name_taken`, `other_account` y
  `template_deleted` y `meta_id_taken` (el id que devolvió Meta ya está en otra fila del negocio, p. ej. tras reclamar la WABA, ADR-0122) no.
- **Superficie nueva y lo que NO cambia.** `post_template` se extrae de `create_template` (el panel sigue igual);
  `find_template_by_name` es nueva. No se llama a `update_template` jamás para una versión. No hay tabla ni migración nuevas.
- **API (encadenado).** `POST /plantillas-opciones/{clave}/publicar` acepta un cuerpo opcional `meta` (`connection_id`, `categoria`, `idioma`, `ejemplos`): congela la versión y, solo entonces, la publica en Meta. Si Meta falla la versión sigue publicada y la respuesta trae `meta` con `ok=false`, el `outcome` y `reintentable`. El reintento (o publicar una versión ya congelada) es `POST /plantillas-opciones/{clave}/versiones/{n}/publicar-en-meta`. El detalle de cada versión lleva su `meta` (nombre, categoría y estado leídos de `message_templates`, sin N+1). Una categoría o idioma inválidos se rechazan con 422 ANTES de congelar nada.
- **Lo que queda fuera.** (a) La ruta de plataforma (master) para encender la bandera, que el PR 6 difirió a este PR, **no se incluye**: sigue siendo un seguimiento aparte y la bandera se enciende a mano (runbook). (b) Distinguir en el panel el `sin_plantilla` de
  una opción del de un evento (adenda del PR 5): es del panel (HU-OPC-06). (c) La clave Redis del límite de creación
  (`tpl_create_rl:{waba_id}:{hora}`) no lleva el negocio (preexistente, mismo defecto que ya se corrigió en
  `acquire_sync_lock`, ADR-0122): seguimiento aparte. (d) La prueba real del payload del QUICK_REPLY y de los límites de 25, 10
  y 256 sigue **PENDIENTE** del dueño y es la puerta para encender la bandera.

## Adenda (2026-10-27) · PR 10a: la ruta de plataforma de la bandera y la primera tool apta

Cierra el seguimiento que las adendas de los PR 6 y 7 dejaron abierto («la ruta master que fija la bandera»). **Con este
PR no se enciende nada en ningún negocio**: la bandera sigue apagada y solo cambia que ahora hay quién la fije.

- **Ruta.** `GET|PUT /api/v1/admin/platform/tenants/{tenant_id}/opciones-directas` en `core/api/routers/platform.py`,
  con el mismo patrón que `/colaboracion` y `/gate`: guard `_require_master_agent_os` (solo `role == "master"`; el
  tester, que en test actúa como master para operar negocios, **no** puede: esto decide qué negocios envían mensajes
  reales con botones, como cambiar el Agent OS), negocio objetivo en la ruta, ejecución dentro de
  `tenant_scope(tenant_id)` y `bandera.fijar` como único escritor (la prueba `test_nadie_fuera_del_modulo…` ahora
  admite `platform.py` y ningún router del dueño). Cuerpo `{habilitada: bool}` estricto (`StrictBool`, `extra="forbid"`:
  ni «true» como texto ni ningún otro campo). Idempotente en el estado; cada PUT audita `opciones_directas_bandera`
  (quién pidió qué, aunque no cambie el valor). 404 con el sobre normal si el negocio no existe o está borrado, sin
  datos del negocio. El listado de negocios es el que ya existe (`GET /admin/platform/tenants`): no se duplica.
- **Entrada en el registro de configuración (CFG-02): DESCARTADA** (enmienda a §5, que la prometía «con la API»). El
  registro es un catálogo de lectura; una prueba (`tests/test_cfg02_registro_opciones.py`) exige que toda opción del
  grupo `agent_os` dependa de `aos_runtime = v3` y fija los conteos de opciones, y esta bandera no depende de v3.
  El master la gobierna con la ruta de plataforma y el interruptor del PR 10b, que la consume directamente (precedente:
  el gate de evals, `settings.evals`, también está fuera del registro). Ya no es un seguimiento pendiente.
- **El «límite conocido» de `bandera.py` no se materializa.** Se revisaron todos los escritores de `tenants.settings`:
  los que reescriben el dict completo (`subscriptions.guardar_proveedor`, `presets.agentes._anotar_creado` y
  `_registrar`, `presets.apply`, `provisioning._guardar_origen`) abren la sesión nueva, leen con
  `session.get(Tenant, id, with_for_update=True)` y reemplazan solo su subclave (el gate de evals, ADR-0117 parte 4, ya
  los había obligado a eso); el resto usa `jsonb_set` o `||` atómicos (`bandera`, `banderas_pila`, `coordinacion`, el
  gate, el recorrido de primeros pasos, el interruptor del alta). Como `fijar` es un `UPDATE` sobre la misma fila,
  toma el mismo candado: o espera a que el otro camino confirme y se aplica encima, o el otro camino lee ya el valor
  nuevo. Una prueba de integración lo ejercita con la fila bloqueada a propósito y una unitaria vigila por AST que
  ningún escritor nuevo de `tenants.settings` se olvide del `FOR UPDATE`. **No se pasa la bandera a una columna.**
  Sigue siendo cierto que `app_runtime` PODRÍA reescribir `settings` entero con un `UPDATE` suelto; si algún día un
  camino lo hace sin candado, el remedio es la columna con trigger como en 0097 (migración, PR aparte).
- **Acciones aptas.** Se inventariaron las 54 tools del core y los packs. Una tool solo es elegible si, además de su
  marca, ningún procedimiento de los packs activos la nombra (§5; el lint `tool_en_procedimiento` usa los
  procedimientos DISPONIBLES de los packs habilitados, con o sin `aos_procedures`). Eso deja fuera, aunque sean
  seguras, a las de solo lectura que gobierna un procedimiento de su propio pack (`find_next_available`,
  `list_appointment_types`, `list_calendars` por `agendar_cita`; `sub_consultar` por `cambiar_plan`): marcarlas sería
  letra muerta. La única que cumple todo es `list_client_appointments` (solo lectura, siempre acotada al cliente y a la
  conversación, con resultado escalar `proximas`/`total` citable en la respuesta fija): se marca
  `apta_para_opcion=True`. Una prueba fija la lista de las marcadas. **Seguimientos propuestos (no se hacen aquí):**
  (a) decidir si una tool de solo lectura puede ignorar la regla del procedimiento (haría elegibles las cuatro de
  arriba); (b) una tool mínima de solo lectura del core, p. ej. `consultar_horario_de_atencion`, que devuelva
  `abierto_ahora` y el horario como escalares, sin depender de ningún pack; (c) un `escalar` idempotente si se quiere el
  botón «Hablar con un asesor».
- **Tools declarativas con `option_safe`.** Se pueden marcar hoy por API (`PUT /admin/settings/declarative-tools` y el
  editor crudo del AgentSpec), no desde el panel (no tiene editor de tools declarativas). Esas rutas exigen el permiso
  `settings` de escritura y no `require_owner_or_master`, a diferencia de definir una plantilla: un colaborador con
  escritura en Configuración puede marcar apta una tool declarativa (y la POST nace `sensible`). Se deja registrado como
  hallazgo; endurecerlo es otro cambio.
- **«Procedimiento habilitado» (enmienda a §5).** En la implementación del PR 4 significa «de un pack habilitado y sano
  del negocio» (`registro.disponibles`), sin mirar `tenants.aos_procedures`: así encender el motor no vuelve obsoleta
  una opción ya publicada y no depende de otra bandera por negocio (fail-closed). Costo conocido: las tools de lectura
  de la agenda quedan fuera en todo negocio con el pack de agenda. Los dos seguimientos que lo cambiarían (que las de solo
  lectura ignoren la regla; una tool nueva del core) se deciden cuando un negocio pida una acción concreta; el primero
  es una decisión de ADR. No se crea una tool nueva solo para rodear la regla.
- **Antes del primer `PUT true`.** (1) Este ADR sigue «propuesto»: debe aceptarse (y registrarse la enmienda a ADR-0108)
  antes de encender cualquier negocio. (2) La prueba del sandbox de Meta es una puerta HUMANA (runbook §1): la ruta no
  la verifica, y mientras el master sea el dueño es aceptable. (3) Endurecer `option_safe` a dueño o master antes de
  encender más allá de test.
