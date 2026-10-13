# ADR-0113 — Contratos `procedure/1`, AgentSpec v3 y KernelSpec v1 (major)

- **Estado:** aceptado (2026-10-09, HU-AOS-01)
- **Fecha:** 2026-10-08
- **Contexto:** HU-AOS-03 y HU-AOS-12 (SPEC-26). Enmienda ADR-0050 (AgentSpec v2), ADR-0014 (la allowlist de
  playbooks pasa a ser la de cada agente), ADR-0051 §3 y ADR-0052 (guardrails y tools declarativas salen de
  `extensions`), ADR-0054 y ADR-0109 (flujo de atención). Usa ADR-0119 (verificador y `register_afirmaciones`),
  ADR-0118 (instalaciones con scope) y ADR-0077 (tipos del panel desde el OpenAPI).

## Contexto

- Los contratos solo cambian con un ADR (invariante 4, ADR-0011). El paquete Python `agentcore-contracts` está en v2.0.0 (ADR-0050); el espejo TS de `packages/contracts/src/` quedó en 1.0.0 y nadie lo consume (los tipos del panel salen del OpenAPI, ADR-0077).
- El AgentSpec describe **un** asistente por negocio. `extensions` lleva hoy las reglas declarativas de guardrails (ADR-0051, `core/orchestrator/loop.py`) y las tools declarativas del negocio (ADR-0052, `core/mcp/declarative.py`), estas con `headers` literales que pueden traer secretos, aunque el comentario del contrato aún lo marca como reservado.
- El flujo de atención es un artefacto de packs y negocio que se inyecta como texto del prompt. Sus secciones no restringen qué acción se llama.
- Un playbook sin validar tumbó el primer turno de los negocios (core #305).

## Decisión

Los contratos ganan tres modelos nuevos: `procedure/1`, AgentSpec v3 y KernelSpec v1. `procedure/1` es aditivo y entra como minor (2.1.0, HU-AOS-03): ningún contrato existente cambia de forma (ADR-0011). El major (3.0.0) llega con AgentSpec v3 y KernelSpec (HU-AOS-12).

1. **`procedure/1`** (lo publica la habilidad) define:
   - disparadores: descripción, ejemplos y prefijos de botones;
   - datos: tipo, *resolver* del servidor, obligatoriedad, confirmación, clase de dato y `setBy` (una acción o un payload interactivo, como el `plan:` de comercio o las listas de agenda), condición por tipo de contenido (ubicación, imagen) y candidatos para elegir; el motor los llena sin pasar por el LLM;
   - etapas: objetivo, **acciones permitidas**, condición de salida, confirmación y riesgo;
   - precondiciones, cierre por eventos de dominio y recuperación;
   - afirmaciones que el verificador comprueba y escenarios de prueba. Las afirmaciones se compilan a `Afirmacion` y se registran por `register_afirmaciones` (ADR-0119): hay un solo verificador y una sola API. Mientras un pack no publique procedimientos, sigue registrándolas como hoy.

   Un **linter** exige acciones existentes, etapas alcanzables, confirmación para toda acción `irreversible` y fuente para toda afirmación de dinero. El mismo validador corre en el CLI, en CI y en la API al guardar un borrador o publicar una release: un procedimiento, AgentSpec o KernelSpec inválido no se guarda (el incidente de #305 salió de un playbook de un negocio, no de un pack).

   Un procedimiento usa acciones del core, de su habilidad y, **por nombre**, de otras habilidades que declara como dependencia en su manifiesto (`requires`), como `contratar_plan` con `generar_link_pago` de pack-payments (así ya funciona el flujo de commerce). Nunca importa otra habilidad (invariante 5): la acción se resuelve en el catálogo del binario y, si la habilidad requerida no está instalada en el scope, el procedimiento no se ofrece (fail-closed). Los efectos entre habilidades siguen yendo por eventos de dominio (ADR-0023, ADR-0107) o registros del core (ADR-0067). Los *resolvers* reciben solo `ToolContext` (invariante 6).
2. **AgentSpec v3** (un agente):
   - `description`. `kind` vive en `agents`, no en el contrato: un agente `core` versiona un KernelSpec v1 y un AgentSpec v3 solo describe agentes `specialist` o `backoffice`. La identidad (`id`, `tenantId`, versión) no está en el contrato: la da la fila;
   - `persona` (`inheritConnectionVoice`, `maxLength`, `forbiddenPhrases`), `model` (los mismos campos que en v2: `primary`, `fallback`, `router`, `temperature`) y `playbooks`;
   - `procedures` (id, rango de versión y overrides de frases y textos);
   - `exposes` (procedimientos y consultas tipadas para otros agentes);
   - `memory` (`readScopes`, `writeNamespaces`);
   - `tools` (bindings: consentimiento, límites y acciones fuera de etapa), `guardrails` y `declarativeTools`;
   - `policies` (`maxToolCallsPerTurn`).

   `declarativeTools` **no contiene secretos**: un header sensible se declara por referencia (`{"Authorization": {"secret": "<nombre>"}}`) y su valor vive cifrado en la configuración de la instalación (`config_encrypted`, ADR-0118 §5). El linter y el validador al guardar rechazan valores literales en `Authorization`, `Cookie` y `X-Api-Key`. El lector tolerante mueve los secretos literales de v2 a la configuración cifrada antes de crear la primera `agent_version`, que es inmutable y entra en cada release (ADR-0117).
3. **KernelSpec v1** (el Coordinador):
   - `voice` de la conexión (rol, tono, calidez, emojis) y `episode` (`idleMinutes`, ADR-0117);
   - enrutamiento: agente por defecto, reglas en un DSL **sin regex** (`interactivePayloadPrefix`, `keywordAny`, `customerFact`, `flowStage`, `channel`, `withinHours`), umbrales de disparadores, coprocesador acotado y estabilidad del dueño;
   - procedimientos (`maxStack`, confirmación de lo irreversible), `modelPolicy` (solo lo que cambia en etapas de riesgo alto), límites de colaboración del turno, composición en un mensaje y playbook de respuestas;
   - `policies.budgetUsdPerConversation`: la conversación es de la conexión y tiene N agentes, así que el tope es uno solo;
   - `policies.backofficeBudgetUsdMonthly`: tope mensual del back-office de la conexión (ADR-0116 §5), que solo baja `costo_llm_usd` del plan.
4. **Una sola fuente por campo:**
   - límites de colaboración del turno: KernelSpec; los de cada enlace: `agent_links` (ADR-0116), que solo puede bajarlos;
   - modelo base y de respaldo: el agente;
   - reintentos y acción ante un fallo del verificador: la configuración del negocio de ADR-0092;
   - presupuesto por conversación: KernelSpec;
   - confirmación de una acción: la etapa del procedimiento. El `requiresConfirmation` del binding solo aplica a acciones fuera de etapa. El KernelSpec solo puede exigir confirmación además de esas, nunca quitarla; lo `irreversible` siempre se confirma;
   - voz: la `voice` del KernelSpec manda para rol, tono, calidez y emojis. `AppSettings.brand_voice` sigue siendo dato del negocio y se edita en el panel, pero la release congela su valor (ADR-0117 §2): un cambio entra con la siguiente release. `behavior_config.tono` deja de escribirse y se lee solo como respaldo de v2;
   - packs habilitados: `pack_installations`, leídas solo por `enabled_packs()`, `enabled_business_packs()` y `count_tenant_packs()` (semántica en ADR-0118 §2);
   - textos de un procedimiento: se sobrescriben solo en `procedures[].overrides`; los playbooks, solo en `TENANTS_ROOT` (ADR-0026). La release congela ambos (ADR-0117).
5. **Lector tolerante v2 → v3:** el AgentSpec v2 activo de un negocio se carga como el agente **General** de **cada** conexión conversacional del negocio. Hasta que se construya su release 1 (ADR-0117 §2), el KernelSpec se deriva en memoria de `persona` v2 (rol, tono, calidez, emojis) y de los valores por defecto. Los presets (ADR-0110) y el alta (ADR-0121) siguen escribiendo un AgentSpec v2 que el lector tolerante convierte, hasta HU-AOS-46 (ADR propio). Cada sección del flujo compuesto pasa a un procedimiento "abierto" (una etapa con sus acciones), y todas las acciones que el flujo v2 ofrecía quedan en sus `tools`: nada cambia de conducta aunque la etapa en foco sea otra. Un procedimiento abierto hereda la confirmación del binding v2 (`requiresConfirmation`) de cada acción, y el linter la acepta como confirmación de la etapa; así "lo irreversible siempre se confirma" no obliga a cambiar conducta al convertir. La correspondencia, campo por campo:

| Campo v2 | En v3 |
|---|---|
| `id`, `version`, `tenantId` | Identidad y versión del agente: `agents` y `agent_versions` (`key`, número y hash) |
| `persona.role`, `tone`, `warmth`, `emojis` | **Voz de la conexión**: `voice` del KernelSpec, versionada con la release; el agente la hereda |
| `persona.maxLength`, `forbiddenPhrases` | `persona` del agente. `maxLength` conserva la semántica de tope blando de la adenda de ADR-0050 (2026-10-10): un reintento propio, nunca escala |
| `model` | `model` del agente; el KernelSpec solo dice qué cambia en etapas de riesgo alto |
| `memory` | `memory`, más `readScopes` y `writeNamespaces` |
| `playbooks` | `playbooks` del agente: la allowlist de ADR-0014 pasa a ser por agente |
| `tools` (bindings) | `tools`: consentimiento, límites y acciones fuera de etapa (fail-closed, ADR-0118) |
| `policies` | `maxToolCallsPerTurn` en `policies` del agente; `budgetUsdPerConversation` en el KernelSpec |
| `packs` | La habilitación pasa a `pack_installations` con scope (ADR-0118), leída por `enabled_packs`. Sale del contrato cuando ningún lector lea el campo |
| `extensions.guardrails` (ADR-0051) | `guardrails` de primer nivel |
| `extensions.tools` (ADR-0052) | `declarativeTools` de primer nivel |
| `extensions` | Sigue reservado y sin semántica ("el contrato no miente", ADR-0050) |

Los textos del clasificador y del compositor viven en playbooks del core (invariante 3). Descripciones, ejemplos y frases son datos de la habilidad o del negocio, no código.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Meter la semántica nueva en `extensions` | Viola "el contrato no miente" (ADR-0050) y `extensions` ya mezcla dos usos |
| JSON libre por agente | Sin validación: repite el incidente de #305 |
| Mantener el flujo como guía del prompt | No garantiza el orden ni las acciones de cada etapa |

## Consecuencias

**Positivas:** recorridos verificables antes de llegar a producción; acciones fail-closed por etapa; agentes versionados y reutilizables entre conexiones; cada campo con una sola fuente.

**Negativas / lo que aceptamos pagar:** un major de contrato; migrar en el mismo PR las habilidades, el plugin del negocio y los editores del panel de guardrails y tools declarativas; mantener el lector tolerante hasta retirar v2.

**Qué haría que revisáramos esto:** que el contrato de procedimientos no exprese un caso real de las verticales (comercio, agenda, suscripciones, cobranza) sin recurrir a código.

## Impacto en el código

- `packages/contracts`: `agentcore-contracts` (Python) a v3.0.0; el espejo TS de `packages/contracts/src/` se retira y se corrige `test_version_congelada`.
- `core/agents/spec.py` (lector tolerante, `enabled_packs`) y `core/agents/flow.py` (conversión a procedimientos).
- `core/orchestrator/loop.py` y `core/mcp/declarative.py`: leen `guardrails` y `declarativeTools` en vez de `extensions`; los headers sensibles se resuelven desde la configuración cifrada.
- `core/llm/prompt_builder.py` (lee `playbooks` y `persona`).
- Todos los call-sites de `enabled_packs`, `enabled_business_packs`, `count_tenant_packs` y `spec.packs` en `core/`, `cli/` y `packages/pack-*` (ADR-0118 §2). El CI falla si queda una lectura de `spec.packs` fuera del lector tolerante.
- Editores: `core/admin/advanced_service.py` y `core/api/routers/settings.py` (identidad, modelo, guardrails), más `docs/openapi.json` y `panel-web`. Las vistas SSR de `core/admin/views.py` que editan `extensions` no se migran: se retiran. El editor de tools declarativas hoy solo existe en SSR, así que se crea su ruta en `/api/v1`.
- `packages/pack-*/`: procedimientos en el manifiesto.
- Linter y validador en `cli/`, en CI y en la API.

## Adenda de implementación (2026-10-10, HU-AOS-12): forma concreta de `agentspec/3` y `kernelspec/1`

**Estado:** propuesta; requiere la aprobación del dueño antes del merge (fija la forma de un contrato congelado).

`agentcore-contracts` pasa a **3.0.0**. El espejo TS de `packages/contracts/src/` se retira. Los nombres que quedan congelados:

- **Clases.** `AgentSpecV3` (`agentspec.py`) y `KernelSpec` (`kernelspec.py`). `AgentSpec` sigue siendo el **v2** con el mismo nombre: lo leen el loop legacy, los presets y el alta hasta HU-AOS-46. Se renombra cuando se retire v2.
- **Todos los modelos v3 rechazan campos desconocidos** (`extra="forbid"`), como `procedure/1`. Por eso `id`, `tenantId`, `version`, `kind`, `key` y `packs` no validan dentro de un AgentSpec v3: los da la fila.
- **Rango de versión de un procedimiento:** `^1.0`, `~1.2.3`, `=X.Y.Z` o `X.Y.Z`. `=0.0.0` está reservado al procedimiento abierto que arma el lector tolerante desde el flujo v2.
- **`procedures[].overrides`:** `examples` (frases, como `triggers.examples`) y `description`.
- **`playbooks[]`:** `{name, description, pack?}`. `description` conserva el índice de v2.
- **Headers sensibles:** `Authorization`, `Cookie` y `X-Api-Key`, más las variantes habituales de credencial por header: `Proxy-Authorization`, `Api-Key`, `X-Auth-Token`, `X-Access-Token`, `X-Api-Token` y `Private-Token`. No se distinguen mayúsculas.
  - Solo admiten `{"secret": "<nombre>"}`, con nombre `^[a-z0-9_]{1,60}$`.
  - Una tool con secretos solo llama por `https://`.
  - Ningún valor de header lleva CR, LF ni NUL.
  - El rechazo de esa lista vive en el propio contrato.
  - ~~Los demás headers con pinta de credencial dejan un aviso del lector tolerante. Limitación conocida: un secreto en la URL o en el `body` no está cubierto.~~ Reemplazado por la regla ampliada de abajo (decisión del dueño 2026-10-11).
- **Regla ampliada de credenciales (decisión del dueño, 2026-10-11, opción A).** En v3, toda credencial de una tool declarativa se declara por referencia, no solo la de la lista. Su valor vive cifrado en la instalación `core`, atado a los dominios de la tool. Ajustada tras la revisión de seguridad.
  - **Qué nombre es sensible** (`core/agents/credenciales.py::es_sensible`, sin distinguir mayúsculas ni tildes): los de la lista del contrato y todo nombre con una palabra (separada por guiones, `_`, puntos o camelCase) que:
    - empieza por `auth`, `token`, `secret`, `passw`, `passphrase`, `passcode`, `session`, `cookie`, `signature`, `credential`, `bearer`, `clave`, `contrasen`, `llave`, `firma` o `hmac`;
    - termina en `key`, `keys`, `token`, `secret`, `password`, `passwd`, `pwd`, `cookie`, `auth`, `session`, `sessionid` o `sessid`;
    - o es `key`, `pass`, `pw`, `pwd`, `sig`, `sid`, `jwt`, `pin` u `otp`.
  - **Ambiguos y falsos positivos:** los ambiguos cuentan como sensibles (`Idempotency-Key`, `Author`, `token_type`, `max_tokens`). No lo son `Content-Type`, `Accept`, `X-Request-Id`, `User-Agent` ni `keyword`.
  - **Dónde se declara la referencia:**
    - **headers:** `{"secret": "<nombre>"}`;
    - **query de la URL:** `{secret:<nombre>}` como valor COMPLETO de un parámetro. Es el único lugar de la URL donde se admite esa forma: ni en el host, ni en la ruta, ni mezclada con texto. Se separan parámetros por `&` y `;`;
    - **userinfo** (`usuario:clave@`): no se admite. Pasa a `Authorization: Basic` por referencia, que es lo que hace httpx;
    - **body:** `{"secret": "<nombre>"}` bajo una clave sensible. La sensibilidad se hereda por objetos y listas, salvo en claves neutras (`user`, `type`, `id`…). Una referencia del body solo cuenta si su nombre cumple `^[a-z0-9_]{1,60}$`, así que su texto nunca se trata ni se reporta como nombre;
    - **fragmento** de la URL y **schema de `parameters`** (`default`, `const`, `enum` o `examples` bajo una propiedad sensible): no llevan credenciales.
  - **Clases de valor** (sobre el texto CRUDO, así `%7B` no es una llave):
    - **literal:** texto fijo sin placeholders;
    - **argumento:** solo `{argumento}`, con un esquema delante opcional (`Bearer {token}`);
    - **mixto:** texto fijo junto con placeholders.
  - **Lo que no cuenta:** los números y booleanos, porque pasarlos a un secreto (texto) cambiaría el tipo del JSON. Es un límite.
  - **Linter v3** (CLI, CI y API al guardar o publicar). Rechaza con la ruta, sin el valor:
    - `credencial_literal`;
    - `credencial_mixta`;
    - `referencia_mal_ubicada`;
    - `secreto_sin_https` (una tool con secretos solo por `https://`);
    - `extension_reservada` (`extensions.tools` o `extensions.guardrails` en v3: son de v2).
  - **Lector tolerante y backfill:** mueven las credenciales literales a la instalación `core`, y ninguna versión ni borrador queda con el literal.
    - En v2, `{"secret": ...}` dentro del body es dato, no referencia, así que también se extrae.
    - El fragmento de la URL se quita, porque no viaja.
    - Si una credencial está mezclada con argumentos, o si hay credenciales fijas en `parameters`, la conversión falla con la ruta y el negocio queda en error hasta corregirlo en el editor. No se migra en silencio, porque cambiaría lo que se envía.
    - Una tool sin `allowedDomains` con credenciales en la URL, el body o `parameters` se descarta entera, con aviso: hoy no llama a nadie.
  - **Convención del valor de un secreto:** se guarda sin codificar, venga de un header, la query o el body. El runtime v3 (HU-AOS-14/15) debe:
    - codificarlo al ponerlo en la query (`quote(safe="")`);
    - sustituir argumentos y secretos en una sola pasada;
    - entregar el secreto solo hacia los dominios a los que está atado, con `resolver_secretos(..., host)` también para las referencias de la URL y del body.
  - **Sin cambio de conducta:** el contrato `agentcore-contracts` no cambia, y el loop legacy (`core/mcp/declarative.py`) sigue con los literales del AgentSpec v2. El editor de tools declarativas v2 sigue con la lista del contrato: ampliarlo cambiaría lo que el loop legacy resuelve, así que lo decide el dueño.
  - **Límites conocidos:**
    - un secreto en la ruta de la URL (`/bot<token>/`) o bajo una clave de nombre neutro no se detecta;
    - números y booleanos, como arriba.

El validador y el linter únicos están en `core/agents/contrato_v3.py`. Los usan `agent-core agent lint`, el CI y la API al guardar o publicar. El lector tolerante está en `core/agents/tolerante.py`. Su salida pasa siempre el validador: lo que hoy está muerto se poda con un aviso, y los secretos literales salen como `SecretStr` hacia la instalación `core`.

## Adenda de implementación (2026-10-10, HU-AOS-05): semántica del motor de procedimientos

**Estado:** propuesta; requiere la aprobación del dueño antes del merge. Fija convenciones de
hecho que usan HU-AOS-06, HU-AOS-07 y HU-AOS-09. No cambia la forma de `procedure/1`. La
revisó el agente `arquitecto` antes de escribir código.

- **Dónde vive:**
  - `core/kernel/procedures/motor.py`: reglas puras;
  - `ejecucion.py`: el motor con estado, el cierre por evento y la entrada sin LLM;
  - `registro.py`: los procedimientos disponibles;
  - `sombra.py`: el modo sombra;
  - `textos.py`: los playbooks del kernel `motor` y `confirmacion`.
- **Registro:**
  - Cada habilidad publica sus procedimientos con `MCPHost.register_procedures` y sus
    *resolvers* con `MCPHost.register_resolver("<plugin>.<función>", validar=…, candidatos=…)`.
    Los resolvers reciben solo `ToolContext` y devuelven `ResolverResult(ok, value, error)`.
  - Un procedimiento ajeno, repetido o que no pasa el linter no se usa: queda en el log y el
    pack sigue vivo.
  - **Transitorio** hasta HU-AOS-13/14: se usan los procedimientos del binario de los packs
    habilitados y sanos (con todo su `requires` habilitado), no los `procedures` de la versión
    del agente.
  - Si la versión de una tarea abierta ya no está en el binario, el motor no la gobierna: queda
    el flujo v2 de respaldo, hasta la revalidación de HU-AOS-14.
- **Acciones por etapa:**
  - Con una tarea en foco se ofrecen solo `stage.actions` más las acciones del core (dueño `core`)
    que el turno ya ofrecía. Desaparecen las tools declarativas del negocio (ADR-0052), por
    fail-closed: un negocio que dependa de ellas lo nota al encender.
  - Una llamada fuera de eso se deniega con un error de negocio (`audit_log` `denied`) y no se
    ejecuta.
  - Sin tarea en foco, todo queda como siempre.
- **Confirmación:**
  - Hay una sola puerta por etapa. Si la etapa declara `confirmation` y `exitWhen.action`, la
    puerta es esa acción. Si `exitWhen` no tiene acción, la puerta es la salida de la etapa.
  - `confirmActions` agrega puertas por acción. El `requiresConfirmation` del binding del negocio
    también: en una acción gobernada por la etapa lo aplica la puerta del motor, no la confirmación
    v2. El negocio no puede quitar una confirmación (§4).
  - La confirmación de una acción queda atada al hash de sus argumentos canónicos y se consume al
    ejecutarse. La de la salida de una etapa queda atada al hash de los valores de la tarea: si un
    dato cambia después del sí, se vuelve a pedir.
  - Si una acción está gobernada o no se decide con la proyección leída bajo lock, nunca con la del
    inicio del turno. El resultado se anota solo en la tarea y la etapa que la autorizaron.
- **Evidencia de confirmación:** la pone el kernel, nunca un argumento del LLM.
  - El botón `confirmar:<task_id>:<seq del confirmation_requested>:<firma>` sirve como id de botón
    interactivo y como payload de botón de plantilla (HU-AOS-09; lo arma
    `ejecucion.payload_de_confirmacion`). La firma es un HMAC con `APP_SECRET_KEY` sobre el negocio,
    la tarea, la petición y el hash de los args, así que el cliente no puede fabricar el de otra
    petición.
  - Si no, un "sí" escrito de confianza alta que contesta la petición del turno **inmediatamente
    anterior** (posterior al último mensaje del cliente que ya tuvo respuesta). El clasificador es
    conservador: coincidencia exacta con el léxico del playbook del kernel `confirmacion`; "dale" y
    "sí" a secas no confirman. HU-AOS-09 lo reemplaza.
  - Un turno que empezó con un botón no confirma por texto, porque el título del botón no lo
    escribió el cliente. La marca vive en Redis 15 min y, si Redis falla, no se confirma.
  - El linter reserva el prefijo `confirmar:`.
- **Datos:**
  - `setBy: action:<A>` toma `resultado[<nombre del dato>]`.
  - Los candidatos `action:<A>.<campo>` guardan solo los ids ofrecidos (hasta `max`) en
    `action_succeeded`. Un botón `<prefijo>:<valor>` llena el dato `setBy: interactive:<prefijo>:`
    solo si el valor está entre los últimos ofrecidos (o entre los que devuelve un resolver de
    candidatos) y pasa el resolver.
  - `content:image|audio|document` se llena desde la media del mensaje (`{media_id, mime}`). El
    video se ignora.
  - `actualizar_tarea` es todo o nada: tipo, opciones, formato y el resolver, con un presupuesto
    total de 5 s porque corre con la conversación bloqueada. El modelo nunca escribe un dato con
    `setBy`.
  - El linter exige que un dato `setBy: interactive:` declare `candidates`, `enum` o `resolver`,
    porque el id del botón lo controla el cliente.
- **Precondiciones:**
  - Corren solo dentro del turno, con el ejecutor que inyecta el loop (permisos, salud y
    auditoría de siempre).
  - Los args son los datos de la tarea cuyo nombre es una propiedad del input schema. Si falta
    uno obligatorio, `precondition_failed` con `precondicion_sin_datos`.
  - "Se cumple" = resultado sin `error`, sin `ok: false` y sin `confirmation_required`; el pack
    niega con `BusinessError`. Una acción con `requiresConfirmation` en el binding no corre como
    precondición.
  - Una precondición debe ser de solo lectura. Hasta HU-AOS-34 el linter no lo puede comprobar,
    así que la clave de idempotencia es estable por (tarea, seq, acción).
- **Transacciones:**
  - Antes del efecto es fail-closed: etapa, confirmación y `action_requested` bajo el lock de la
    conversación.
  - Después del efecto, el resultado llega siempre al modelo y un fallo al registrar solo se
    loguea.
  - Nunca hay un lock abierto mientras corre una acción.
  - El orden de bloqueo es conversación → tareas, también en el consumer (que abre el episodio
    antes de tocar la tarea) y en el worker.
- **Cierre por evento:**
  - Un evento `<dueño>.<topic>` de `completion.events` coincide con el topic del outbox (p. ej.
    `core.payment.confirmed` ↔ `payment.confirmed`). El linter exige que el dueño sea el core, la
    habilidad o una de `requires`.
  - Correlación: solo por las claves de identidad de cada topic (`CLAVES_DE_CORRELACION` en
    `core/shared/events.py`, p. ej. `payment.confirmed → reference`, `order.paid → order_id`), nunca
    por montos ni proveedores. Una tarea correlaciona si un dato fijado por una acción
    (`source = action:*`) tiene el mismo nombre y valor. Un topic sin claves no cierra tareas. Si el
    evento correlaciona con tareas de más de una conversación, no cierra ninguna (log
    `motor_evento_ambiguo`). Las candidatas se buscan por el valor de la clave en SQL (HU-AOS-07): las
    tareas que esperan un pago viven horas y muchas abiertas del negocio no tapan la del evento.
  - El outbox no guarda quién emitió el evento, así que en runtime el dueño del evento es
    documental: cualquier código del binario que emita ese topic para el negocio cierra la tarea.
  - Corre en un savepoint de la transacción del despachador: un fallo del motor se loguea y no
    revierte lo que hicieron los handlers (p. ej. marcar un pago).
  - Las tareas de una habilidad que se deshabilitó o se auto-desactivó para el negocio no las
    gobierna el motor, ni sus resolvers corren.
  - Al salir de la última etapa (se cumple su `exitWhen`) la tarea se completa, declare o no
    `completion.events`; una última etapa **sin** `exitWhen` espera el evento. Con `completion.events`
    el evento la completa desde cualquier etapa. (Ajustado por HU-AOS-07, cierre por pago: antes una
    última etapa con `exitWhen` y eventos de cierre también esperaba.) `returnControl` retoma la
    primera tarea en pausa.
- **Recuperación:** `actionError = relevo_humano` escala la tarea y el turno. `timeout` y
  `explicar_y_reintentar_una_vez` quedan para una HU siguiente.
- **Pendiente para HU-AOS-09:** el texto que el cliente confirma hoy lo redacta el modelo. HU-AOS-09
  debe armar el resumen del botón desde los argumentos del servidor.

## Adenda de implementación (2026-10-10, HU-AOS-13): backfill B2

- **Los secretos se copian, no se mueven.** El backfill (`agent-core db backfill-agentes`) copia los
  literales de las `declarativeTools` v2 a la instalación `core` antes de la primera `agent_version`:
  ninguna versión lleva literales, pero `agent_specs` (la fila activa y las inactivas) los conserva
  porque el loop legacy los sigue usando. Su limpieza queda para HU-AOS-46. El backfill nunca pisa un
  secreto existente (lo comprueba con la fila bloqueada) ni lo ata solo a dominios nuevos: un rollback
  del v2 podría traer dominios que alguien quitó a propósito. En ese caso el negocio queda en error
  y hay que reescribir el secreto en el editor.
- **Header sensible de una tool sin `allowedDomains`:** el lector tolerante lo descarta con aviso y no
  extrae el secreto. Esa tool hoy no llama a nadie (el loop arma un `ScopedHttp` por tool con su
  allowlist), así que no cambia la conducta, y el mismo v2 da el mismo contenido para el backfill y
  para el build de releases (HU-AOS-14).
- **Idempotencia por `legacy_agent_spec_id`:** con el dedupe por hash, el legacy id de una versión es
  "la primera fila v2 que produjo este contenido". Una re-corrida con el mismo v2 activo no publica
  nada, aunque el binario haya cambiado: refrescar la versión es decisión del build de HU-AOS-14.

## Adenda de implementación (2026-10-11, HU-AOS-13 parte 2): escrituras de instancias y altas

Revisada por el agente `arquitecto` antes de escribir código. No cambia contratos ni esquema (sin migración).

- **Inicializar solo lo que no tiene Coordinador.** `asegurar_instancias_base` crea el Coordinador y el General
  solo en una conexión **sin Coordinador vivo**. Una conexión que ya lo tiene está configurada y no se toca: lo que
  el dueño quitó o cambió (el General, el agente por defecto) no vuelve en un religar del número, en otra corrida
  del backfill (`configurada` en el reporte) ni en el build de releases.
- **El Coordinador lo gestiona el sistema** (ADR-0112 §1: exactamente uno por conexión). La API de instancias no lo
  agrega, no lo deshabilita ni lo quita; para que el asistente no responda está la pausa (ADR-0122). Los triggers
  diferidos de 0070 quedan como red en la base. Solo se agregan especialistas (el back-office corre en el worker,
  ADR-0116 §5), con una versión publicada y en una conexión que ya tiene Coordinador.
- **Siempre un agente por defecto, habilitado.** Se cambia eligiendo otro (el anterior deja de serlo en la misma
  transacción); no se deshabilita ni se quita mientras lo sea. Las escrituras de una conexión se serializan con
  `FOR NO KEY UPDATE` sobre su fila: esa regla no la garantiza ningún constraint.
- **Permisos:** las escrituras piden la sección `settings` con escritura (cambian lo que dice el asistente, como
  editar un agente), poder ver `connections` y, con `aos_runtime = legacy`, ser master; el GET sigue en
  `connections`. Tope de 50 instancias vivas por conexión.
- **Altas posteriores** (ADR-0115 §6): `numero_para_conexion` llama a `instancias_al_alta` en la misma transacción,
  dentro de un SAVEPOINT. Si el negocio ya tiene su Coordinador y su General, la conexión nueva nace inicializada; si
  no, no se hace nada (lo hará el backfill). Un error se registra (`instancias_alta_fallo`; a nivel error con la
  traza si es de RLS o de FK compuesta) y el alta sigue: con `legacy` las instancias no hacen nada y un bug del
  Agent OS no debe tumbar las altas de WhatsApp. Un `tenant_scope` ajeno (`PermissionError`) sí se propaga. Fallar
  cerrado para negocios v3 se decide con HU-AOS-14.
- **Promoción** (ADR-0127 §3): la conexión del número real clona las instancias de la del número de prueba
  (`clonar_instancias`) antes de lo base; si la cuenta de prueba tiene varios números vivos no se adivina y entra
  solo lo base. El mapa que devuelve (instancia de origen → de destino) solo lleva pares
  con el mismo agente: es lo que necesita HU-AOS-14 para clonar `release_components` (FK por agente e instancia).
  Los triggers diferidos se evalúan al COMMIT, fuera del savepoint.
- **Pendiente para HU-AOS-14:** qué de una instancia (`enabled`, `priority`, `is_default`) congela la release y qué
  sigue en caliente no lo clasifica ADR-0117 §2; la API lo documenta como "aplica con la próxima release".

## Adenda de implementación (2026-10-11, HU-AOS-06): comandos del hilo

Revisada por el agente `arquitecto` antes de escribir código. No cambia contratos: los comandos
son tools del core que arma el motor por turno (`core/kernel/procedures/comandos.py`).

- **Solo con el motor activo** (`aos_procedures`). En sombra o con la bandera apagada no aparece
  ninguna tool nueva y el prompt es idéntico (el gate en replay pasa sin regrabar).
- **Se ofrecen solo los que aplican al estado:**
  - `iniciar_procedimiento(procedure_id)`: sin tarea en foco y con candidatos (los disponibles sin
    tarea abierta; sin router hasta HU-AOS-17). `procedure_id` es un enum de esos candidatos, con
    título y descripción de cada uno escapados en la descripción. Un id fuera de la lista se
    rechaza sin crear la tarea;
  - `actualizar_tarea(slots)`: con tarea en foco y datos que el modelo puede escribir (sin `setBy`,
    ni ubicación ni archivo, ni `candidates`: esos se eligen con un botón o una lista). El motor
    rechaza con la misma lista lo que llegue fuera del schema. Un objeto con una propiedad por dato (tipo, `enum`, `format`) y
    `additionalProperties: false`;
  - `confirmar_tarea()`: con algo pendiente de confirmar (`comandos.confirmable_por_comando`, el
    punto que puede ajustar HU-AOS-09);
  - `cancelar_tarea()`: con tarea en foco.
- **Sin `task_id` ni `motivo`** (la tabla del design §5.2 los listaba): actúan sobre la tarea en
  foco. El modelo no ve UUIDs y no hay dos tareas abiertas del mismo procedimiento; un motivo libre
  sería PII en `task_events`, que no se redacta.
- **Pausar y retomar** quedan para HU-AOS-20 (dueña de la pila y de `maxStack`); retomar al
  completar ya existe (`returnControl`).
- **Resultado** (`CommandResult` del design, como dict): `{ok, state_card, errors,
  allowed_actions}`. `state_card` es la tarjeta re-renderizada tras el comando y manda sobre la del
  inicio del turno (el prompt del sistema no se reconstruye). `allowed_actions` son las acciones de
  la etapa vigente que de verdad se ofrecen. La vuelta siguiente ofrece las tools de la etapa nueva.
- **Despacho:** el loop los manda al motor antes de `_tool_denial`. Un comando que no aplica al
  estado vigente (no se ofreció o el lote lo cambió) o con argumentos fuera del schema no se aplica.
  En un mismo lote, `iniciar` + `actualizar` funciona: cada comando se decide con el estado que dejó
  el anterior. Los nombres están reservados: ninguna acción del binario puede usarlos (test) y una
  tool declarativa del negocio con ese nombre no se ofrece mientras el motor esté activo.
- **Un comando aplicado resuelve el turno** (`turnos_sin_resolver`), aunque no sea una acción: sin
  eso, un procedimiento de cuatro datos llegaría al autoescalado de ADR-0051/0104. Límite conocido:
  un modelo que repite un comando aplicable cada turno esquiva el autoescalado; se mira con el
  tablero de fiabilidad (HU-AOS-29) antes de encender un negocio.
- **Salud del pack en vivo:** iniciar, actualizar y confirmar vuelven a comprobar que la habilidad
  sigue sana para el negocio (un pack que se auto-desactivó a mitad del turno no abre tareas ni
  llama sus resolvers). Si la relectura del estado falla, el resultado va sin tarjeta (nunca con la
  de antes del cambio). La descripción de `iniciar_procedimiento` lista hasta 12 candidatos.
- **Textos:** playbook del kernel `comandos` (invariante 3), sin override por negocio y fail-closed;
  los schemas son estructura y viven en código.

## Adenda de implementación (2026-10-11, HU-AOS-09): confirmación con botón y clasificador

**Estado:** aceptada (dueño, 2026-10-11). Cierra el "Pendiente para
HU-AOS-09" de la adenda de HU-AOS-05. No cambia la forma de ningún contrato. La revisó el agente
`arquitecto` antes de escribir código. La política de modelo por riesgo está en ADR-0132.

- **Un solo mensaje.** Si el turno pidió una confirmación (la tarea en foco quedó con
  `pending_confirmation` registrada en este turno), el mensaje del turno es el que arma el kernel y
  **reemplaza** el texto del modelo (`composition.style = single_message`). Así el cliente no ve dos
  versiones que se contradigan, y `TurnResult.text`, la memoria y `messages` quedan coherentes. Lo
  que el modelo haya agregado de paso se pierde: es aceptable con `maxQuestions = 1` y es lo único
  determinista.
- **Resumen del servidor, nunca del modelo** (`core/kernel/confirmacion.py`, rótulos en el playbook
  del kernel `confirmacion_boton`):
  - acción: los argumentos exactos de la llamada que pidió confirmación (los que ata el hash y firma
    el botón), solo las propiedades que declara el `input_schema` de la acción (una clave inventada
    por el modelo no se muestra);
  - etapa: los datos de `exitWhen.slots` y `exitWhen.confirmed`; dato: ese dato;
  - más el monto que devolvió en el turno una acción de la etapa declarada como fuente de un `claim`
    de tipo `amount` (`action:<acción>.<campo>`). Va primero y nunca se recorta por largo.
  - **Fail-closed:** si la petición es de este turno pero no hay un resumen fiel (faltan los args, la
    fuente devolvió dos montos distintos en el turno, un argumento con el mismo nombre dice otra cosa
    que el servidor, o algo falla), sale el texto fijo `sin_resumen`, sin datos ni botón: nunca
    vuelve el texto del modelo pidiendo confirmar.
  - El core muestra el nombre del dato con espacios y oculta identificadores (`id`, `*_id`, valores
    con forma de uuid), valores no escalares, credenciales (`es_sensible`), datos `payment` (de la
    tarea y argumentos con su nombre) y valores con forma de tarjeta (13 a 19 dígitos que pasan
    Luhn). Todo valor va saneado: una línea, sin controles ni formato Unicode (Cc, Cf, Zl, Zp: bidi,
    ancho cero), sin las marcas de formato de WhatsApp y acotado. El cuerpo respeta los topes de
    WhatsApp.
  - **El resumen propio de una acción de un pack lo provee el pack**, con un punto de extensión al
    estilo de `register_afirmaciones` (recibe `ToolContext` y los args, solo lectura, con timeout).
    Llega con HU-AOS-07, su primer cliente: hoy ningún pack publica procedimientos, así que nadie ve
    el resumen genérico antes. Ejemplo del caso que lo exige: `crear_pedido` recibe `quote_id` y
    `customer_data`, y el total no está en sus argumentos.
- **Botón.** Con `stage.confirmation = interactive_button`, el sender lo envía como mensaje
  interactivo (un botón "Confirmar" con `confirmar:<tarea>:<seq>:<firma>`) solo en WhatsApp Cloud
  (`meta`) con la ventana de 24 h abierta y con el payload dentro de los topes (cuerpo ≤ 1024,
  título ≤ 20, id ≤ 256). En cualquier otro caso sale el mismo cuerpo como texto, con la instrucción
  de responder «confirmo». `explicit` (o una confirmación de dato o del binding) sale siempre como
  texto. La fila de `messages` guarda el payload en `interactive` (migración 0082) y la cola de
  salida lo envía en orden con el resto. Un fallo del envío sigue el camino de siempre (reintentos,
  DLQ y escalamiento), porque es el mensaje del turno; un `ChannelError` del adaptador (la conexión
  dejó de ser de Meta) es permanente, no se reintenta como error de red.
- **Clasificador de confirmación escrita** (`core/kernel/procedures/clasificador.py`, léxico en el
  playbook `confirmacion`): determinista y conservador. Una pregunta (cualquier signo de pregunta,
  sobre el texto en NFKC) nunca confirma, ni un texto con símbolos o letras no latinas que la
  normalización borraría ("confirmo ❌", "confirmo нет"; solo se toleran emojis de aprobación); confirma una
  frase exacta de `si` o una frase de hasta 8 palabras hecha solo de `nucleo`, `prefijos` y
  `cortesia`, con al menos una de `nucleo` y ninguna de `negaciones`. "Sí", "dale", "ok" o "listo"
  solos no confirman (decisión del dueño). Se mide con `tests/fixtures/confirmacion/etiquetado.jsonl`
  sin LLM: precisión 1.0 obligatoria y recall reportado (98 % al 2026-10-11, 194 frases armadas para
  el set: falta sumar respuestas reales anonimizadas).
- **Fuera de esta adenda (pendiente):**
  - Confirmación fuera de la ventana con una plantilla aprobada con botón quick-reply: exige una
    plantilla aprobada por WABA y ampliar ADR-0108 (parámetro `payload` del botón). En un turno la
    ventana está abierta, así que solo afecta confirmaciones pedidas por el back-office.
  - Entrada del botón de plantilla (`type: button`, hoy se descarta en el parser): va con la
    plantilla, filtrada en `_dispatch_entry_messages` (antes del claim) cuando el negocio no tiene
    el motor activo.
  - Después del toque, el modelo tiene que repetir la acción con los mismos argumentos (el hash) y el
    historial es solo texto: lo cubre un test de integración con modelo guionado, y queda como riesgo
    para HU-AOS-06 (comandos como tools).

## Adenda de implementación (2026-10-11, HU-AOS-08): procedimientos de agenda, cambio de plan y promesa

**Estado:** propuesta; requiere la aprobación del dueño antes del merge. La revisó el agente `arquitecto`
antes de escribir código. No cambia la forma de ningún contrato ni el motor.

- **Dónde viven:** cada pack trae sus procedimientos como JSON en `packages/pack-<x>/procedures/` (junto a
  `flow.json` y `playbooks/`) y los registra en `on_activate` con `core/kernel/procedures/de_pack.cargar`
  (resuelve la carpeta como la del `flow.json`, orden por nombre de archivo, solo valida la forma; un JSON
  roto se loguea y se salta). El linter contra el binario corre en runtime (`registro.py`) y en CI
  (`tests/test_procedimientos_de_packs.py`, que cubre todo `packages/pack-*/procedures/*.json`).
- **Publicados:** `pack_calendar.agendar_cita` (el `agendar_demo` del Gherkin, con textos genéricos: una
  demo es un tipo de cita; los textos propios de un negocio llegan como `overrides` con HU-AOS-13/14),
  `pack_subscriptions.cambiar_plan` y `pack_cobranza.promesa_de_pago`.
- **Acciones solo de procedimiento:** `sub_cotizar_cambio_plan`, `sub_aplicar_cambio_plan` (core, adenda de
  ADR-0111), `cob_registrar_promesa_saldo` y `cob_opciones_promesa` (pack-cobranza; la segunda, desde la
  decisión del dueño del 2026-10-11, ver abajo). Ningún `flow.json` las nombra y no son
  `always_available`: el loop legacy no las ofrece (siempre hay flujo: `core/flow.json`), no entran en los
  bindings del General v3 y solo aparecen cuando el motor las pone en `stage.actions` de la tarea en foco.
- **Convención del monto esperado:** el motor no ata los argumentos de una acción a los datos de la tarea.
  En lo que mueve dinero o compromete un monto, la acción que compromete recibe el monto como argumento,
  **con el mismo nombre que el campo fuente de la afirmación `amount`** (`monto_a_pagar`; en la promesa,
  `monto`, que desde el 2026-10-11 es un dato de la tarea y además tiene resumen del pack, ver abajo): así el
  resumen del botón lo muestra como monto del servidor y el kernel ya rechaza el resumen si el argumento
  difiere de la fuente del turno. El servidor recalcula y no hace nada si no coincide; el valor del modelo
  nunca se usa como monto.
- **Salida atada a un efecto real:** las etapas que comprometen salen con `exitWhen.action` **y** un dato
  que fija esa acción (`event_id`, `cambio`, `promesa_id`), y las acciones nuevas devuelven `error` en todo
  camino sin efecto (`create_calendar_event` devuelve `status: rejected` sin `error` en un choque).
- **"Ofrecido" (agenda):** un horario es ofrecido si está entre los libres que el servidor ofrece ese día,
  con la misma cuenta que `check_availability` (`_free_slots`, cuadrícula de `slot_minutes`, duración del
  tipo de cita). El resolver no ve los eventos de la tarea ni recorre varios días (corre bajo el lock con 5
  s): si el día no tiene cupo, remite a `find_next_available`.
- **Reglas de cobranza en el resolver** (configuración del pack, nunca del prompt): fecha no pasada, a lo
  sumo `max_dias_prorroga` días, no domingo si la cartera lo marca no laborable y no más de
  `max_reprogramaciones` promesas pendientes. ~~El monto es el saldo de `cob_consultar_saldo`.~~ Enmendado
  el 2026-10-11 (decisión del dueño, abajo): el monto es el de la opción elegida, lo vencido o el saldo total.
- **Afirmaciones compiladas: no se registran todavía.** `register_afirmaciones` no va detrás de la bandera:
  compilar "promesa registrada" con respaldo en `cob_registrar_promesa_saldo` daría falsos positivos en
  legacy. Límite: con el motor activo, las frases de los `claims` de estos procedimientos no las verifica
  el verificador (sí los montos y links genéricos, y el `promesa_guardrail` acepta las dos tools). HU-AOS-07
  debe registrarlas detrás de `aos_procedures`.
- **Límites conocidos:**
  - En la agenda, `create_calendar_event` recibe `start_datetime` del modelo: puede no ser el `horario`
    validado. El botón muestra los argumentos exactos y, en modo interno, la tool revalida (ocupado, fuera de
    horario, pasado); **en modo Google no revalida**. Atar argumentos ↔ datos es trabajo del motor (HU siguiente).
  - **Oferta según el motor** (`core/kernel/oferta.py`, predicados de oferta de la tool): con
    `aos_procedures` encendida, las tools de siempre que un procedimiento reemplaza (`sub_cambiar_plan`,
    `cob_registrar_promesa_pago`) no se ofrecen, para que el modelo no se salte el botón; si la bandera no
    se puede leer se ofrecen como siempre. Las solo de procedimiento no se ofrecen sin la bandera, aunque un
    flujo del negocio las nombre; si la bandera no se puede leer, no se ofrecen. `create_calendar_event` no
    se oculta: es la acción de la etapa `agendar` (el predicado filtra también las candidatas del motor) y
    sirve fuera del procedimiento; con el motor activo y sin tarea en foco se sigue ofreciendo.
  - Con el motor activo y una tarea en foco de OTRO procedimiento, las acciones solo de procedimiento no
    se ofrecen (no están en la etapa). Sin foco, solo si un flujo del negocio las nombra; ahí el monto sigue
    atado pero no hay botón. Negarlas en `antes_de_accion` sin foco es un cambio del motor que queda para
    una HU siguiente.
  - Costo: cada predicado lee la bandera del negocio (una consulta por tool reemplazada o nueva y por
    turno, hasta cinco; seis con `cob_opciones_promesa`). Si se nota en la latencia, se cachea por turno.
- **Decisión del dueño (2026-10-11): la promesa puede ser por lo vencido o por el saldo total**, para dar
  flexibilidad. Enmienda las dos frases tachadas o anotadas arriba. La revisó el agente `arquitecto`.
  - **Montos del servidor.** Lo vencido es la suma de las cuotas abiertas (`pendiente`/`atrasada`) que
    vencieron antes de hoy, la misma cuenta que la mora; el saldo total, la de `cob_consultar_saldo`. Las
    opciones salen de una función pura (`pack_cobranza.procedimientos.opciones_de_promesa`): lo vencido se
    ofrece aparte solo si es mayor que 0 y menor que el saldo; si es 0 o igual al saldo hay una sola opción
    (el saldo total). Un valor de configuración desconocido se trata como `saldo_total` (lo de antes).
  - **Configuración del negocio** junto a las demás reglas de negociación:
    `pack_cobranza_reglas_negociacion.alcance_promesa` (`ambos` por defecto, `vencido` o `saldo_total`;
    migración del pack `pack_cobranza_0017`, con CHECK). Se lee y se escribe por `GET/PUT
    /api/v1/pack/cobranza/rules`; en el PUT, omitir el campo conserva el valor (el panel de hoy no lo manda
    y el PUT reemplaza el resto). Con solo `vencido` y nada vencido no hay promesa que registrar.
  - **El alcance lo fija la acción con la elección del cliente.** `cob_opciones_promesa(eleccion?)` (solo de
    procedimiento, `solo_con_motor_activo`) devuelve `saldo_total`, `vencido` y las `opciones` con sus montos
    (y las agrega tal cual al final de la respuesta con `_safe_append`); con una `eleccion` ofrecida, o si hay
    una sola opción, devuelve además `alcance` y `monto`. Los cuatro son datos `setBy:
    action:cob_opciones_promesa`, así `alcance` y `monto` se escriben en el mismo lote y nunca quedan
    desparejos; cambiar de opción es volver a llamarla. **`eleccion` no se llama como el dato `alcance` a
    propósito:** es una elección que el servidor valida contra lo que ofrece en ese momento, no el id de un
    objeto que fija el servidor; con el mismo nombre, la ligadura de HU-AOS-07 la negaría (un dato
    `setBy: action:` que la tarea aún no tiene, o uno que cambia sin resolver).
  - **Monto esperado, ahora ligado:** `cob_registrar_promesa_saldo(fecha, alcance, monto)` (cambia su schema;
    es solo de procedimiento) tiene los tres argumentos obligatorios con el nombre de datos de la tarea: la
    ligadura los completa o niega uno distinto (`argumento_ligado`). En la transacción, con el préstamo
    bloqueado, recalcula las opciones con la regla del negocio; si el alcance no está ofrecido o el `monto`
    no es el del servidor (redondeo de `mismo_saldo`), devuelve `error` y no registra. La promesa guarda
    `valor_prometido` = monto del servidor y `alcance`.
  - **Resumen del pack** (`register_resumen`, HU-AOS-07): el botón muestra el monto del servidor para el
    alcance elegido, el alcance ("lo vencido" / "el saldo total") y la fecha. Si el alcance ya no se ofrece o
    el monto firmado no es el del servidor (un pago entre la elección y el sí), no hay resumen: sale el texto
    fijo sin botón; al reconsultar las opciones, el cambio de `saldo_total`/`vencido` invalida `alcance` y
    `monto` y la tarea vuelve a `opciones`.
  - **Seguimiento (cumplida/incumplida):** hoy no hay job automático: lo marca el gestor o el panel
    (`POST /promises/{id}/status`) contra `valor_prometido` y la fecha. Con alcance `vencido`, la promesa es
    por lo vencido al registrarla; las cuotas que venzan después no forman parte de ella. La columna
    `pack_cobranza_promesas.alcance` (NULL en las de antes, las del gestor y las de la tool de siempre) sale
    en `GET /promises` para que ese seguimiento sepa por qué monto fue. `max_reprogramaciones` cuenta las
    pendientes sin importar el alcance. La APK (sync) no la recibe todavía.
  - **Sin cambio con la bandera apagada:** `cob_registrar_promesa_pago` (la de siempre) no cambia de schema
    ni de conducta, `cob_consultar_saldo` devuelve lo mismo y las dos tools nuevas no se ofrecen; el gate en
    replay pasa sin regrabar. El procedimiento sube a 2.0.0 y pack-cobranza a 0.6.0 (lock regenerado): una
    tarea abierta en 1.0.0 deja de estar gobernada, y hoy ningún negocio tiene `aos_procedures` encendida.
  - **Límites:** `_hoy()` usa la fecha del servidor, no la zona del negocio (ADR-0091): entre la medianoche
    UTC y la del negocio, una cuota que vence hoy puede contar como vencida. Ya pasaba con la mora y las
    reglas de fecha; ahora mueve un monto, pero el monto esperado lo protege (si cambia, no se registra). Queda
    para una HU. El panel no tiene todavía el campo: hasta su PR, el valor se cambia solo por la API.

## Adenda de implementación (2026-10-11, HU-AOS-07): procedimientos de comercio

**Estado:** aceptada (dueño, 2026-10-11), con el cierre por pago que pidió el dueño ese día (sección
"Cierre"). No cambia la forma de ningún contrato ni el esquema. La revisó el agente `arquitecto` antes
de escribir código. Todo rige solo con `tenants.aos_procedures`; con la bandera apagada el modelo ve
lo mismo (el gate en replay pasa sin regrabar).

- **Procedimientos como datos de la habilidad.** Como en la adenda de HU-AOS-08: JSON en
  `packages/pack-*/procedures/`, registrados con el cargador único `core/kernel/procedures/de_pack.py`.
  El manifiesto de la versión del pack incluye ahora `provides.procedures` (ADR-0118 §1) con
  `de_pack.crudos`, que lee los mismos archivos que registra `de_pack.cargar`: lo que se congela en
  `pack_versions` es lo que se registra. Por eso cambian de contenido, y suben de versión, todos los
  packs con procedimientos: pack-commerce 2.1.0 (publica `pack_commerce.contratar_plan` y
  `pack_commerce.tomar_pedido`), pack-calendar 2.2.0, pack-subscriptions 0.2.0 y pack-cobranza 0.5.0.
- **Corrige el ejemplo de §1:** `contratar_plan` no usa `generar_link_pago` de pack-payments (la
  retiró la adenda del 2026-10-10 de ADR-0087): el link sale de `confirmar_pago_pedido` de commerce,
  que llega a los pagos por `payment_registry`. No declara `requires`.
- **Argumentos ligados a los datos de la tarea** (`motor.ligar_argumentos`, en `antes_de_accion` bajo
  el lock). En una acción gobernada, un argumento con el nombre de un dato de la tarea que tiene
  valor (en el primer nivel del input schema o dentro de un objeto de primer nivel, p. ej.
  `customer_data.email`) queda atado a ese dato. Es la misma convención "por nombre" de las
  precondiciones.
  - Igual (normalizado como el dato) → se usa el valor del dato, así el hash de la confirmación no
    depende de cómo lo escribió el modelo.
  - Distinto → si el dato tiene resolver y lo lleva al mismo valor (un SKU por el uuid de la
    variante), vale; si no, se deniega con `argumento_ligado` y no se ejecuta.
  - Ausente → el kernel lo completa solo si es obligatorio (`required` de primer nivel, o dentro de
    un objeto de primer nivel obligatorio o presente). Un argumento opcional no se completa: puede
    ser una alternativa (`informar_plan(sku | variant_id)`) y completarlo cambiaría la consulta.
  - Los argumentos efectivos viajan en `Previo.args`: el loop ejecuta, audita y registra esos; el
    historial del modelo no se toca.
  - Garantiza que la cotización es del plan elegido, que confirmar y crear el pedido usan la
    cotización vigente, que el link es el del pedido de la tarea y que la dirección del pedido es la
    confirmada.
- **Un dato que cambia invalida lo que dependía de él** (`motor.dependientes`,
  `motor.etapa_de_retroceso`; un solo punto para los tres caminos de escritura: comando, botón y
  resultado de una acción). Cuando un dato con valor recibe otro:
  - se borran (`slot_cleared`, actor `kernel`, motivo `dato_cambiado`) los datos que fijó una acción
    después del valor viejo. Es una heurística de causalidad por `seq` que invalida de más, hacia el
    lado seguro: la acción se vuelve a llamar;
  - si una etapa anterior del camino ya no cumple sus datos (`exitWhen.slots` y `confirmed`; las
    salidas por acción ya pasadas cuentan como cumplidas), la tarea vuelve a ella (`stage_entered`
    hacia atrás, que también descarta la confirmación pendiente).
  - Así, cambiar de plan en `confirmar` borra la cotización y vuelve a `cotizar`: la acción que
    confirma queda fuera de etapa y, con la ligadura, la cotización vieja no se puede usar.
- **Datos congelados tras un efecto** (`motor.datos_congelados`). Desde la primera acción que terminó
  bien en una visita a una etapa `risk: high` (el pedido existe), cambiar un dato con valor se rechaza
  (`dato_bloqueado`) por el resto de la tarea, también en la espera del pago que viene después; la
  salida es `cancelar_tarea`. Antes del efecto sí se puede (pasar de pago en línea a transferencia
  tras el rechazo por el mínimo). Solo frena lo que escriben el modelo (`actualizar_tarea`) o un
  botón: el resultado de una acción sí se anota (el link nuevo cuando venció el anterior). Le da a `risk` un segundo significado,
  además de la política de modelo de ADR-0132: se migra a los metadatos de efecto de las acciones
  con HU-AOS-34.
- **Resumen de confirmación que provee el pack** (cierra el pendiente de la adenda de HU-AOS-09).
  `MCPHost.register_resumen(accion, fn)` con `DatoDeResumen(nombre, valor, es_monto)` en
  `core.mcp.types` (API de plugins, no contrato). Solo para una tool del mismo plugin; se limpia en
  `remove_plugin`. El motor lo llama al cerrar el turno (sin lock), con los argumentos ligados que
  firma el botón y un tope de 2 s. Si falla, tarda, viene vacío o trae un monto que no es número,
  sale `sin_resumen`. Con resumen del pack no se mezclan los montos de los `claims` del turno: el pack
  ya leyó el estado vigente. pack-commerce lo da para `confirmar_cotizacion` desde la cotización del
  cliente (plan y periodicidad o productos y cantidades, descuento y total).
  - Por eso en la etapa de confirmación no puede haber una acción que cambie el total sin cambiar el
    id que firma el botón (`aplicar_promocion`): un test lo vigila en los procedimientos del pack.
- **Revisión de seguridad (2026-10-11), lo que se ajustó:**
  - Una confirmación de acción vale solo en la visita vigente a la etapa: un cambio de etapa
    (también el retroceso) la vence (`accion_confirmada` corta en `stage_entered`).
  - Con resumen del pack, la petición guarda la huella del resumen (`confirmation_requested.resumen`)
    y antes de ejecutar se vuelve a armar: si la misma cotización cambió de total, el sí anterior no
    vale y se pide otro; al cerrar el turno, un resumen que ya no coincide sale como `sin_resumen`.
  - Un argumento con el nombre de un dato que fija una acción (`setBy: action:`) y la tarea todavía
    no tiene se niega: el modelo no puede poner el pedido de otra compra antes de crear el de esta.
  - Los datos de un resultado no se toman si ese resultado trae otro valor para un dato de la tarea
    que no fija esa acción (el link de otro pedido).
  - La auditoría de la ejecución guarda los argumentos que mandó el modelo, no los datos ligados
    (PII de la tarea); los resolvers de la ligadura comparten un presupuesto de 5 s; un resumen de
    más de 20 líneas sale `sin_resumen` (los montos van primero) y las opciones del catálogo en los
    errores de un resolver van escapadas como dato.
  - Aceptado y anotado: en `tomar_pedido` el resumen muestra productos y total, no la entrega ni el
    método de pago, y cambiarlos en `confirmar` o `pagar` antes del efecto no vuelve a pedir el sí
    (no cambian el monto). Los datos que llegan como contenido (media) no pasan por el congelado ni
    por la invalidación (ningún procedimiento de esta HU los usa).
- **Monto (alineado con el "monto esperado" de HU-AOS-08):** `confirmar_cotizacion` recibe solo el
  `quote_id`, y agregarle el monto cambiaría su schema con la bandera apagada. En commerce el monto que
  el cliente confirma sale del resumen del pack (leído de la cotización) y queda atado por su huella;
  la ligadura de argumentos también cubre los argumentos de monto de HU-AOS-08 cuando un dato de la
  tarea lleva su nombre.
- **Lo que pide el negocio, antes de confirmar.** `contratar_plan` no fija los datos del cliente:
  `cotizar_plan` (tool nueva de commerce, que ninguna sección del flujo nombra, con el predicado
  `solo_con_motor_activo` de `core/kernel/oferta.py`: sin la bandera ni se ofrece ni se ejecuta)
  devuelve `pide_<campo>` desde `checkout_fields["plan"]` del negocio, y cada dato es `requiredWhen`
  de su `pide_<campo>`. Así un mismo procedimiento sirve a cualquier configuración (sin fork) y los
  datos se piden en `datos`, antes del sí. Los términos del alta (ADR-0125) siguen en `crear_pedido`,
  en `pagar`, como sin procedimientos.
- **Afirmaciones compiladas detrás de la bandera** (cierra el pendiente de la adenda de HU-AOS-08):
  con el motor activo, el loop suma a las de los packs las `claims` con frases de los procedimientos
  disponibles del turno (`ContextoMotor.afirmaciones`, `compilar_afirmaciones`), de todas las
  habilidades. No van por `register_afirmaciones`, que no sabe de la bandera: con ella apagada no se
  verifica nada nuevo. Se respaldan con las acciones (del turno y anteriores, como las de los packs) y
  con el estado: el dato que declara la `claim` (`order_id` para "pedido creado"). Las
  `COMPLETION_CLAIMS` de commerce siguen como siempre.
- **Cierre (decisión del dueño, 2026-10-11): el procedimiento se completa cuando el pago se confirma,
  no al crear el pedido y el link.** `contratar_plan` y `tomar_pedido` (v1.1.0, pack-commerce 2.2.0)
  declaran `completion.events: ["pack_commerce.order.paid"]`.
  - **Evento y correlación, sin cambiar lo que ve el modelo.** pack-commerce escribe `order.paid`
    (`{order_id, public_order_number}`) en el outbox (ADR-0107) en la MISMA transacción en que su
    handler de `payment.confirmed` marca el pedido `pagado`: solo en esa transición, con el monto ya
    validado contra el total (ADR-0087 §3) y con `dedupe_key` por pedido (un webhook repetido o un
    reintento del despachador no lo duplica). `CLAVES_DE_CORRELACION[order.paid] = ("order_id",)`: la
    tarea correlaciona por el `order_id` que fijó `crear_pedido`. Ninguna acción cambia su resultado
    ni su schema, así que con la bandera apagada el modelo ve lo mismo; lo único nuevo en un negocio
    sin motor es una fila de `domain_events` por pedido pagado, que se entrega sin handlers (el aviso
    `domain_event_sin_handlers` no sale si un procedimiento declara el topic).
  - **Descartado:** agregar `reference` al resultado de `confirmar_pago_pedido` (cambia lo que ve el
    modelo) y un correlador del core que resuelva la referencia de pago contra los pedidos (la misma
    forma que descartó la adenda 2026-10-10 de ADR-0087; duplicaría la regla `-rN` y el chequeo de
    monto en otra sesión del despachador).
  - **Espera** (etapa `esperar_pago`, última, `risk: low`: sin thinking de ADR-0132). Sale con el dato
    `pago_confirmado` (`setBy: action:verificar_pago_pedido`, `enum: ["confirmado"]` para que ningún
    otro valor lo llene, `requiredWhen` pago en línea): con
    transferencia, contraentrega o un plan Free el dato no aplica, la etapa sale enseguida y la tarea
    se completa al crear el pedido, como antes. Acciones: `verificar_pago_pedido` (tool nueva de
    solo lectura, con `solo_con_motor_activo`: sin la bandera ni se ofrece ni se ejecuta; devuelve
    `pago_confirmado` con el pedido pagado y si no `pago_pendiente`/`pago_rechazado`),
    `confirmar_pago_pedido` (reenvía el link o genera uno nuevo si venció) y `consultar_inventario`.
    Si el cliente escribe mientras espera, el modelo ve la tarjeta (`pago_confirmado: FALTA`) y solo
    esas acciones, las del core y los comandos; para otra compra, `cancelar_tarea`. Una tarea en
    espera no bloquea otra conversación: el lock es por conversación y el cierre busca por valor.
  - **Al salir de la última etapa la tarea se completa** aunque declare `completion.events` (ajuste de
    la adenda de HU-AOS-05); una última etapa sin `exitWhen` espera el evento.
  - **Pago rechazado:** no cierra ni emite nada. El cliente puede reintentar en el mismo link mientras
    siga vigente o pedir otro; `verificar_pago_pedido` le dice al modelo que fue rechazado. Si después
    se aprueba, `order.paid` cierra la tarea.
  - **Link vencido:** no hay evento (pack-payments calcula la vigencia al pedir el link).
    `confirmar_pago_pedido` en la espera genera uno nuevo y el dato `link` se actualiza; el pago en el
    link nuevo cierra la tarea por el pedido.
  - **Que no quede abierta para siempre:** el episodio siguiente ya la cancela si el cliente vuelve a
    escribir (ADR-0114). Si no vuelve, el cron `core.kernel_esperas_vencidas` (cada hora,
    `core/kernel/procedures/esperas.py`) cancela con motivo `espera_vencida` (actor `kernel`) las
    tareas de procedimientos con `completion.events` que siguen en su última etapa sin actividad hace
    más de 72 h (el link vence a las 24 h por defecto). No le escribe al cliente ni escala; recorre
    solo negocios con `aos_procedures`, una transacción por tarea y `FOR UPDATE SKIP LOCKED` sobre la
    conversación (nunca espera a un turno). No es `recovery.timeout` del contrato, que sigue
    pendiente: cuando llegue, la duración debe pasar a ser dato del procedimiento y este cron, usarla.
    Si el cliente paga después de la cancelación, el pedido se marca pagado igual. Filtra la última
    etapa en SQL, así las tareas abandonadas en etapas anteriores no tapan las que esperan.
  - **Sin `claim` nueva de "pago confirmado":** respaldarla con `slot:pago_confirmado` daría falsos
    positivos cuando el cierre llegó por `order.paid` (el dato no se fija) y el modelo lo dice bien
    tras `consultar_estado_pedido`. El "nunca des un pago por confirmado sin esta respuesta" queda en
    la descripción de `verificar_pago_pedido`; revisable con el verificador sobre estado.
  - El dueño de un evento de cierre sigue siendo documental (adenda de HU-AOS-05): cualquier código
    del binario que emita `order.paid` para el negocio cierra la tarea del pedido.
- **Política de modelo (ADR-0132):** `pagar` es `risk: high`, pero el turno en que el cliente toca
  «Confirmar» empieza en `confirmar` y corre `crear_pedido` sin thinking. Se deja así por costo: el
  thinking aplica a los turnos que empiezan en `pagar` (términos, datos faltantes).
- **Pendientes antes de encender `aos_procedures` en un negocio:**
  - La espera del pago dura horas: durante ella la conversación queda limitada a las acciones de
    `esperar_pago` ("agrégame una gaseosa" u otra compra exigen `cancelar_tarea`, y si el cliente paga
    después la tarea queda `cancelled` aunque la venta ocurrió). La pila de tareas (HU-AOS-20) lo
    resuelve. También agranda la ventana del pendiente siguiente: si la release cambia mientras espera,
    `revalidar_tareas` escala la tarea (`crear_pedido` ya corrió y no declara efecto).
  - `ReleaseCongelada.procedimientos()` solo deriva los abiertos del flujo: un cambio de release
    escala la tarea de un procedimiento publicado al primer `action_succeeded` (la acción no declara
    efecto). Lo resuelve HU-AOS-16, junto con los procedimientos por agente: hasta entonces todo
    negocio con commerce ve `contratar_plan` y `tomar_pedido` como candidatos.
  - Con el motor activo y sin tarea en foco, `confirmar_cotizacion` y `crear_pedido` se siguen
    ofreciendo (son las acciones de las etapas; el predicado de oferta filtra también las candidatas
    del motor): el modelo puede cerrar una venta por el camino de siempre, sin botón, con las guardas
    de siempre de la tool. Negarlas sin foco es el mismo pendiente que anota HU-AOS-08.
  - Desde que hay procedimientos registrados, un negocio con `aos_runtime = v3` y el motor apagado
    escribe la sombra (`audit_log`, Redis).
  - Un proveedor que devuelva un QR en vez de URL no llena `link` (`format: uri`) y la tarea queda en
    `pagar`; la salida es `cancelar_tarea`.

## Adenda (2026-10-12): sin foco no hay camino de siempre

**Estado:** propuesta; requiere la aprobación del dueño antes del merge (cambia la semántica del motor sin
foco). La revisó el agente `arquitecto` antes de escribir código. No cambia la forma de ningún contrato, ni
el esquema, ni lo que ve el modelo con `tenants.aos_procedures` apagada (el gate en replay pasa sin
regrabar). Origen: prueba real en test del 2026-10-12. Con el motor activo y sin tarea en foco, el modelo
cerró una venta por `confirmar_cotizacion` → `crear_pedido` sin llamar `iniciar_procedimiento`. Las tareas
quedaron vacías y no hubo botón.

- **Resuelve los dos pendientes anotados:**
  - ~~(HU-AOS-07) "Con el motor activo y sin tarea en foco, `confirmar_cotizacion` y `crear_pedido` se siguen
    ofreciendo…"~~;
  - ~~(HU-AOS-08) "Negarlas en `antes_de_accion` sin foco es un cambio del motor que queda para una HU
    siguiente."~~
- **Enmienda texto aceptado:**
  - adenda HU-AOS-05, "Acciones por etapa": ~~"Sin tarea en foco, todo queda como siempre."~~ Sin foco,
    todo queda como siempre **salvo las puertas** (abajo);
  - adenda HU-AOS-08: ~~"`create_calendar_event` no se oculta: … con el motor activo y sin tarea en foco se
    sigue ofreciendo."~~ Ahora sin foco no se ofrece ni se ejecuta. El reagendado usa
    `update_calendar_event`, que no es puerta;
  - ADR-0114 §2: con el motor activo, sin foco y con procedimientos que iniciar, la tarjeta ya no es "",
    aunque no haya tareas: lleva la pista de iniciar (abajo).
- **Qué se reserva: las puertas de confirmación.** `motor.acciones_de_puerta(proc)` = las `confirmActions` y
  la `exitWhen.action` de una etapa con `confirmation` (lo mismo que `requiere_confirmacion`). Se toma la
  unión sobre los procedimientos disponibles del negocio (`registro.disponibles`), no solo los candidatos, así
  una tarea en pausa (HU-AOS-20) no abre el camino de siempre por otro lado. Con los procedimientos
  publicados hoy son `confirmar_cotizacion`, `create_calendar_event`, `cob_registrar_promesa_saldo` y
  `sub_aplicar_cambio_plan` (un test fija el conjunto derivado de `packages/pack-*/procedures/*.json`).
  - **No se reservan las acciones de las etapas `risk: high`**: le daría a `risk` un tercer significado (el
    segundo, los datos congelados, ya es deuda de HU-AOS-34). Además metería `confirmar_pago_pedido`: el
    cliente que vuelve con la tarea vencida (episodio de 12 h, cron de 72 h, tarea completada o cancelada)
    no podría pedir otra vez el link, y la pista lo llevaría a armar otro pedido de la misma compra.
  - **`crear_pedido` sigue ofrecida sin foco y no compromete nada nuevo:** exige una cotización `aceptada`, y
    con el motor activo esa solo sale de `confirmar_cotizacion` dentro de una tarea, con botón. Si no la
    hay, la tool devuelve su error de siempre, que remite a `confirmar_cotizacion`, y esa se niega con la
    pista. Límite aceptado: si una tarea se cancela después de confirmar la cotización, `crear_pedido` sin
    foco puede usar esa cotización ya confirmada por el cliente.
  - Es una regla estructural hasta que el manifiesto de acciones v2 (HU-AOS-34) declare el efecto de cada
    acción. Las consultas y lo que no se confirma con el cliente se siguen ofreciendo (`informar_plan`,
    `consultar_inventario`, `check_availability`, `cob_consultar_saldo`, `sub_consultar`,
    `generar_cotizacion`…).
- **Oferta** (`ContextoMotor.herramientas`, motor activo): sin foco, las puertas no se ofrecen. Con foco
  se ofrecen solo si son de la etapa, como siempre. Endurecimiento de la revisión de seguridad: una puerta
  del core que el turno ya ofrecía (`sub_aplicar_cambio_plan` cuando un flujo la nombra) tampoco entra
  por el atajo de las acciones del core.
- **Negación** (`ContextoMotor.antes_de_accion`, bajo el lock de la conversación). Si no hay foco y el modelo
  llama una puerta, se niega con `audit_log denied`. Vale también para la del core, p. ej.
  `sub_aplicar_cambio_plan`. Una puerta se decide siempre bajo el lock, con el foco y el respaldo releídos,
  nunca con lo del inicio del turno. Con foco, una puerta del core sigue la etapa: fuera de ella,
  `fuera_de_etapa`; en ella, la puerta de confirmación del motor. El texto `sin_recorrido` del playbook del kernel `motor` nombra
  los procedimientos candidatos (disponibles sin tarea abierta, releídos bajo el lock) que la tienen como
  puerta, para que el modelo llame `iniciar_procedimiento` con ese id. Sin candidatos, el texto es
  `tarea_en_curso`. La garantía es la negación; ocultarla solo ahorra vueltas. `_tool_denial` deja pasar una
  tool registrada aunque no se ofrezca, y el resumen del flujo puede seguir nombrándola.
- **Respaldo v2:** hay una tarea activa en la posición 0 que el motor no gobierna (versión fuera del
  binario o habilidad deshabilitada, `_foco` = None).
  - Las puertas **de su procedimiento** (según la definición disponible con ese id) no se ocultan ni se
    niegan: queda el flujo v2 de respaldo de la adenda HU-AOS-05.
  - Las de los demás procedimientos siguen reservadas.
  - No hay pista.
  - Para la oferta sale de las mismas filas que ya lee el motor; para la negación, de la relectura bajo el
    lock.
  - Si la relectura falla, se ocultan todas (fail-closed).
- **Pista en el prompt, sin depender del playbook del negocio.** Con el motor activo, sin foco, sin tarea sin
  gobierno y con candidatos, la tarjeta de estado lleva:
  - el encabezado (o lo que ya mostraba, sin repetirlo);
  - `sin_foco`: inicia el recorrido de la lista de `iniciar_procedimiento` antes de actuar; para
    consultas, responde como siempre;
  - `sin_foco_acciones`: las puertas que el negocio tiene como tools "solo funcionan dentro de un
    recorrido, aunque otra instrucción diga usarlas directo".

  Los textos viven en el playbook del kernel `estado-conversacion` (invariante 3) y no nombran verticales.
  La descripción de `iniciar_procedimiento` (playbook `comandos`) también dice que se inicia antes de
  cualquier otra acción. Un playbook de negocio que diga usar las puertas directo (override total, como el
  de ventas de solvershub) cuesta una vuelta de negación por venta hasta que se corrija en su repo
  (HU-AOS-22).
- **Sin cambio:** sombra, bandera apagada, verificador y afirmaciones (una negación es un `error` y no
  respalda nada), política de modelo (ADR-0132, solo con foco) y autoescalado (una llamada negada no
  resuelve el turno; si el modelo insiste turno tras turno, escala).

## Adenda (2026-10-12): confirmaciones de datos visibles y sí a secas para datos

**Estado:** aceptada (dueño, 2026-10-13). El punto 3 revierte para los DATOS la decisión aceptada
de HU-AOS-09 "el sí a secas no confirma"; para acciones con monto o efecto sigue igual. La revisó el agente `arquitecto`
antes de escribir código. No cambia la forma de ningún contrato, ni el esquema, ni el reductor de
`state.py`, ni lo que ve el modelo con `tenants.aos_procedures` apagada (el gate en replay pasa sin
regrabar). Origen: prueba real en test del 2026-10-12 (23:45–23:52 UTC y 02:43 del 13),
`pack_commerce.contratar_plan` v1.1.0 en WhatsApp: la tarea quedó atascada en `datos` y el cliente nunca
pudo pagar.

- **Causa raíz.** `actualizar_tarea` pidió la confirmación del correo al fijarlo en `elegir_plan`. En el
  mismo comando la tarea avanzó a `cotizar` y el `stage_entered` vació la pendiente (regla del reductor y
  de HU-AOS-07: un cambio de etapa vence la confirmación). Al cerrar el turno no había nada pendiente, así
  que salió el texto del modelo. Después nadie la volvió a pedir: solo `actualizar_tarea` la pedía, al
  fijar el dato. `confirmar_tarea` no se ofrece sin pendiente, `datos` exige `confirmed: ["email"]` y la
  tarjeta no decía qué faltaba. El modelo terminó diciendo "no tengo la herramienta para generar el
  pedido" y, horas después, tomó un "Si, confirmo" como un sí a escalar.
- **1. La confirmación de un dato se pide en la etapa que la exige, cuando es lo único que falta**
  (`motor.dato_por_confirmar`; `ContextoMotor._pedir_dato_debido`, al final de cada avance). Es debido
  el primer dato de `exitWhen.confirmed` de la etapa vigente, con valor, sin confirmar y que aplica
  (`requiredWhen`), si es lo único que bloquea la salida y no hay otra confirmación pendiente. Es una
  pregunta a la vez: el cliente confirma el correo al final de dar sus datos, no a mitad. Un dato
  `confirm: true` que ninguna etapa exige se sigue pidiendo al fijarlo, como antes. Como la petición
  queda registrada en el turno, sale el mensaje del kernel (adenda HU-AOS-09, reemplaza el texto del
  modelo).
  - Un dato sale siempre como texto, sin botón y sin montos de `claims`, con la instrucción propia
    `instruccion_dato` del playbook `confirmacion_boton` ("responde «sí»"). Esto corrige un desvío:
    antes tomaba el modo de la etapa.
- **2. Al empezar el turno** (`ContextoMotor.al_empezar_turno`, una línea en `loop.py` después de fijar
  el ejecutor y antes de la tarjeta y las tools). Solo con el motor activo y una tarea en foco. Si la
  proyección leída no tiene un dato pendiente ni uno debido, no toca la base. Bajo el lock y releyendo:
  - **Dato pendiente y el cliente asiente contestando DIRECTO el mensaje del kernel:** lo confirma el
    kernel (`confirmed`, actor `kernel`), igual que el botón del consumer, y la tarea avanza. No hace
    falta que el modelo llame `confirmar_tarea`. "Directo" (revisión de seguridad,
    `_contesta_el_mensaje_del_kernel`) quiere decir que después de la petición salió un solo mensaje
    no fallido y que el mensaje del cliente es posterior a él. Así no confirman un aviso proactivo
    ("¿confirmas tu cita? responde sí"), un mensaje de una persona del equipo en el medio ni un "sí"
    escrito antes de ver la pregunta. Un turno que empezó con un botón no confirma por texto (marca
    de HU-AOS-05).
  - **Dato pendiente vencido y el cliente asiente:** se vuelve a pedir y sale el mensaje del kernel,
    porque un sí a una pregunta vieja no vale. Si el cliente pregunta otra cosa, no se re-pide nada y la
    respuesta del modelo llega (revisión del arquitecto: re-pedir sin intención de confirmar tapaba una
    respuesta cada dos turnos).
  - **Sin pendiente y con un dato debido** (p. ej. una tarea atascada antes de este cambio, o un dato
    que llegó con un botón en el consumer): se pide.
- **3. "Sí" a secas para un DATO** (`textos.confirmacion_suficiente`, único criterio para el hook y para
  `confirmar_tarea`).
  - Un dato `pii` se confirma con un asentimiento (`clasificador.es_asentimiento`): "sí", "ok",
    "dale", "listo", "correcto", "sí, el correo está bien". Es una lista permitida: un dato `public`
    puede llevar monto o efecto (cantidad, plan), y uno `payment`, o uno que ya no está en el
    procedimiento, sigue con el criterio estricto. Usa las secciones `asiente_dato` y
    `objetos_de_dato` del playbook `confirmacion`, que `es_confirmacion` no lee: "confirmo el correo"
    nunca confirma una acción con monto. Las guardas son las mismas: sin preguntas, sin símbolos raros,
    8 palabras o menos y ninguna negación. El léxico falla al cargar si una frase permitida trae una
    negación adentro.
  - Acciones y etapas, lo que tiene monto o efecto, siguen con el sí explícito de HU-AOS-09.
  - Set propio `tests/fixtures/confirmacion/dato.jsonl` (48 frases): precisión 1.0 y recall 100 %
    (22/22).
  - Recall del clasificador estricto: regla 4 (una frase exacta de `si` con cortesía intercalada, p. ej.
    "sí, está bien, gracias") y "con el pago/la compra/el pedido" en `cortesia`. Set etiquetado: 194 →
    213 frases (66 positivas, 147 negativas, con los objetos de dato como negativos). Antes: precisión 1.0
    y recall 98,4 % (60/61). Ahora: precisión 1.0 y recall 100 % (66/66).
- **4. La tarjeta y los errores dicen qué bloquea la etapa** (`motor.faltas_de_salida`,
  `tarjeta.describir_faltas`). En foco, la línea `para_avanzar` del playbook `estado-conversacion` lista,
  en palabras del sistema:
  - datos del cliente, de un botón, de contenido o de una acción;
  - datos por confirmar;
  - la confirmación de la etapa o la acción de salida.

  Le pide al modelo explicar qué falta y no decir que le falta una herramienta. El error de una acción
  fuera de etapa trae lo mismo (`fuera_de_etapa_falta`, playbook `motor`).
- **5. Un dato personal que el cliente no escribió no se acepta** (decisión conservadora, revisable;
  `motor.dicho_por_el_cliente`).
  - **Regla:** en `actualizar_tarea`, un dato `pii`/`payment` tiene que aparecer en lo que escribió el
    cliente en la conversación: los últimos 100 entrantes, con la transcripción de audio o imagen, más
    el texto del turno. Si no, se rechaza todo el comando (`dato_no_dicho`) y el modelo lo pide. Caso
    real: `full_name = "Rob"`, el nombre del perfil de WhatsApp.
  - **Comparación** (sobre el valor del modelo y el normalizado; un objeto o una lista nunca cuenta):
    - números: un número entero que escribió el cliente, con separadores y, en uno de 10 dígitos o
      más, el indicativo de país (un fragmento no cuenta);
    - correo: secuencia contigua;
    - nombres y direcciones: por palabras, con regla de subconjunto (el modelo puede quitar palabras,
      no agregarlas).

    El teléfono que verifica el canal (`contact_phone`/`external_id`) vale solo para un dato con
    `format: phone`. Es una sola consulta por comando, solo si hay un dato personal, con cada texto
    acotado a 2000 caracteres.
  - **Límites aceptados:**
    - una abreviatura que el modelo expande ("Cra" → "Carrera") se rechaza y cuesta una vuelta;
    - una palabra suelta de otro mensaje cuenta (el nombre "Rob" pasaría si el cliente escribió "Tienda
      Rob").
  - **Tensión con ADR-0114** ("los datos no se piden dos veces"): con un lector v3 de `memory_facts`, los
    hechos con procedencia `tool`/`human` deberían aceptarse sin que el cliente los repita. Hoy no hay
    ese camino; cuando llegue, se amplía esta regla.
- **6. Una confirmación de acción vencida no atasca.**
  - **En `antes_de_accion`:** si el modelo vuelve a llamar la acción y la petición pendiente es de un
    turno anterior que el cliente ya tuvo para contestar, se registra otra. Así sale el mensaje del
    kernel con resumen fiel y botón nuevo. La del turno inmediatamente anterior sigue valiendo (acción
    → `confirmar_tarea` → acción, con test).
  - **En `confirmar_tarea`:** con intención de confirmar y la petición vencida, una de dato o de etapa
    se vuelve a pedir (`confirmacion_repedida`). Para una de acción, el texto `confirmacion_vencida`
    pide volver a llamar la acción.
  - **En `_avanzar`:** no se re-pide nada vencido (ahí se tragaba la respuesta tras cualquier
    consulta).
- **Datos de la misma conversación (02:43 del 13), anotados:**
  - `core_decisions.focus_task_id` no lo escribe ningún código: la columna de 0069 no tiene escritor y
    la cabecera se escribe antes de que el motor lea las tareas. NULL no significa "sin foco". El
    episodio (720 min) no había vencido a las 3 h y la tarea seguía en foco. El cron de 72 h solo
    cancela tareas en su última etapa, así que una tarea atascada en una etapa intermedia vive hasta
    que el cliente vuelve tras 12 h. El hook de arriba la desatasca. Queda un issue aparte para
    escribir `focus_task_id`.
  - "Ya te comuniqué con una persona" sin fila en `escalations`: por código, `escalate()` solo audita
    `escalate` después del commit de la escalación. No se pudo reproducir sin leer test. Una afirmación
    del core sobre escalamiento para el verificador va en su propio PR y con su adenda de ADR-0119
    (medida con el set del verificador), no aquí.
- **Sin cambio:** sombra, bandera apagada, reductor, contratos, esquema, botones y la confirmación
  estricta de acciones y etapas.
