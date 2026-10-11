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
    `core/shared/events.py`, p. ej. `payment.confirmed → reference`), nunca por montos ni
    proveedores. Una tarea correlaciona si un dato fijado por una acción (`source = action:*`) tiene
    el mismo nombre y valor. Un topic sin claves no cierra tareas. Si el evento correlaciona con
    tareas de más de una conversación, no cierra ninguna (log `motor_evento_ambiguo`).
  - El outbox no guarda quién emitió el evento, así que en runtime el dueño del evento es
    documental: cualquier código del binario que emita ese topic para el negocio cierra la tarea.
  - Corre en un savepoint de la transacción del despachador: un fallo del motor se loguea y no
    revierte lo que hicieron los handlers (p. ej. marcar un pago).
  - Las tareas de una habilidad que se deshabilitó o se auto-desactivó para el negocio no las
    gobierna el motor, ni sus resolvers corren.
  - Una etapa final sin `completion.events` completa la tarea al salir. `returnControl` retoma la
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
