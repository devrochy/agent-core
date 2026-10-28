# ADR-0179 — Datos del cliente por módulo: política de requisitos, ficha de relación y aviso de tratamiento

- **Estado:** propuesto (falta que lo acepte el dueño; activar el aviso en un negocio real además exige P-37).
- **Fecha:** 2026-10-28
- **Contexto:** SPEC-33 «CRM» (HU-CRM-12 a HU-CRM-16) y D-56 de solvershub-docs.
  **Enmienda:** ADR-0153 (la vista por módulo gana el filtro `agente` y una definición explícita de
  «tiene datos»), ADR-0114 (nuevo bloque dinámico «relación» y `guardar()` como única puerta del perfil
  para las tools del agente) y ADR-0171 *resumen por agente* (comparte el patrón de registro por pack).
  **Generaliza:** ADR-0161 (autorización de tratamiento de datos, hoy solo para la vertical inmobiliaria)
  y **enmienda su §6**: allí revocar `tratamiento_datos` dispara la supresión del titular en los packs;
  aquí esa supresión queda diferida hasta que ADR-0164 exista en el código (decisión 11).
  **Usa:** ADR-0022 (el cliente es `Customer`), ADR-0053 (PII), ADR-0056 (`lead_registry`), ADR-0150
  (`customer_consents`), ADR-0164 (supresión del titular), ADR-0166 (registro de opciones) y ADR-0173
  (`AgentSpec.packs` filtra lo que corre).
  **Nota de numeración:** hay dos ADR-0171 (asistente de creación de agentes y resumen por agente);
  aquí «ADR-0171» es el de resumen por agente.

## Contexto

El dueño quiere que los datos del cliente convivan en varias secciones sin duplicarse: los básicos en
Operación → Clientes (los gestiona el agente General) y, por módulo (Ventas, Cobranza, Agenda,
Suscripciones), una lista de los clientes de ese módulo con lo que el módulo generó. También quiere
que el agente guarde lo que va aprendiendo, reconozca al cliente que ya existe y actúe según su
historial, y que cada negocio pueda decidir qué datos exige cada acción (el documento puede ser
opcional para pedir un producto y obligatorio para suscribirse).

Lo que hay hoy, verificado en `develop`:

- **La columna Agentes miente.** `_agentes_de_la_fila` (`core/api/routers/leads.py`) cuenta como «con
  datos» cualquier valor que no sea `None`, `""`, `[]` o `{}`. El `_resolver` de commerce devuelve
  `clasificacion: "Nuevo"` y `pedidos: 0` para todo cliente, así que el icono de Ventas sale en todos.
  Además el panel pinta atenuado, no oculta, lo que no tiene datos.
- **Cuatro mecanismos sueltos de «datos requeridos»:** `checkout_fields` (commerce, claves no
  canónicas), `campos_de_inscripcion` (suscripciones), `Slot.required` (procedimientos) y `needs_data`
  (agenda). Cada uno responde distinto cuando faltan datos.
- **El agente no ve el historial.** Recibe el perfil (`entity_profile`), las notas del equipo y los
  recuerdos; los pedidos, citas o saldos solo llegan si llama una tool.
- **Cobranza no reconoce al cliente que escribe.** `Cliente.customer_id` solo se lee
  (`_cliente_de_customer`, `providers.py`); ningún código lo asigna por teléfono. Un deudor cargado a
  mano no se enlaza con su chat.
- **No hay aviso de tratamiento de datos.** `customer_consents` solo admite `promociones`
  (`ck_customer_consents_proposito`, migración 0100) y `core/consentimiento.py` no existe.

ADR-0153 ya decidió que el perfil canónico vive en `memory_facts` y rechazó columnas o tablas de perfil
por agente. Este ADR no lo revierte: segmenta por módulo como **vista**, no como almacenamiento.

## Decisión

1. **Registro de requisitos por módulo y acción** (`core/datos_cliente/`). Cada pack registra
   acciones (`ventas.pedido_domicilio`, `ventas.pedido_retiro`, `ventas.plan_legado`,
   `suscripciones.inscribirse`, `agenda.agendar_cita`, `cobranza.*`) con claves **canónicas** de
   `core/memory/perfil.py`, un nivel por clave (`requerido`, `recomendado`, `no_pedir`), una condición
   opcional (`cuando`) y un piso técnico (`ajustable=False`, p. ej. el correo de la cita). El core no
   importa tablas de packs: el pack registra, el core evalúa.
2. **Precedencia y almacenamiento.** override del negocio > configuración del pack > default
   declarado. `checkout_fields` y `campos_de_inscripcion` pasan a ser *fuente de defaults* y siguen
   vigentes. Los overrides viven en `AppSettings.behavior_config["datos_cliente"]`: **sin tabla
   nueva**. Un único escritor los cambia con `SELECT … FOR UPDATE` y deja una fila en `AuditLog` con el
   antes y el después de los niveles. El aliasing de claves no canónicas lo hace el core con
   `perfil.normalizar_clave`.
3. **Respuesta única de «faltan datos».** Toda tool que valide datos del cliente devuelve, de forma
   aditiva, `faltan_datos {accion, faltan[], recomendados_pendientes[], ya_registrados[], guardar}`.
   `ya_registrados` lleva solo claves, nunca valores. Cada tool conserva su `status` y sus campos
   legados (`missing_fields`, `needs_data`, `faltan_datos`) durante un release. La política llega al
   agente por tools (`consultar_datos_cliente(accion?)` y la respuesta de cada tool de acción), no por
   el prefijo cacheado (ADR-0117, HU-AOS-35), que se versiona por agente compilado y quedaría obsoleto
   al editar un override.
4. **`LeadRelacion` en el `lead_registry`.** Campo opcional del `LeadProvider` con tres funciones:
   `con_datos(ids)` (SQL que define «tiene datos reales»), `ficha(ids)` (líneas cortas para el prompt) y
   `detalle(cid, limite)` (para la tool de historial). Definiciones de `con_datos`: commerce = tiene
   `Order`, `Quote` o un `Lead` **con contenido** (nombre, teléfono, correo o interés); calendar =
   `Appointment` vigente; suscripciones = `Subscription`; cobranza = `Cliente` con `customer_id`. El
   borrador que crea `auto_create_lead` en cada conversación nace vacío y no cuenta, pero uno ya
   completado sí: se mira el contenido, no el prefijo `auto:` de la clave. `auto_create_lead` y
   `completar_lead` no cambian (los recorridos guiados necesitan un lead existente que completar).
   Si un provider no la implementa, se conserva la heurística actual.
5. **Iconos y listas por módulo.** La columna Agentes muestra solo agentes con datos; el General se ve
   siempre (ADR-0153 §4). `GET /admin/leads` y su CSV aceptan `agente=<key>` (aditivo; una clave
   desconocida es 400 `filtro_invalido`). Los agentes publicados sin proveedor de datos dejan de
   aparecer en la columna. `agents[]` no cambia de forma, para no romper la APK. Ventas y Agenda ganan
   su lista de clientes sobre ese filtro; Suscripciones y Cobranza conservan la suya.
6. **Ficha de relación en el prompt.** `PromptContext.relacion` se emite en `bloque_dinamico`, después
   de `entity_profile` y nunca en los bloques fijos. Topes duros: ≤60 tokens por módulo, ≤400 en total
   y ≤4 módulos. Solo entran los packs de `AgentSpec.packs` (ADR-0173). **Sin montos, documento, correo
   ni dirección:** los importes solo salen por `consultar_historial_cliente(modulo, limite ≤ 10)`,
   porque el verificador solo reconoce montos que vienen de resultados de tools. El texto externo va
   escapado y delimitado (ADR-0114). Cada provider tiene un timeout corto; si falla o expira, se omite
   su línea y el turno continúa. La tool no es `apta_para_opcion` (ADR-0172).
7. **Identidad: mismo canal y enlace por teléfono.** No se unifican canales distintos (D-SPEC-33-5
   sigue vigente). Cobranza enlaza `Cliente.customer_id` con el `Customer` del chat comparando los
   dígitos de `telefono_whatsapp` con `contact_phone`: una coincidencia enlaza con `UPDATE … WHERE
   customer_id IS NULL` (idempotente); dos o más no enlazan y se cuentan como ambiguas. También se
   intenta al crear el `Cliente`. Todo queda dentro de `pack-cobranza`, sin hook nuevo en el core.
   `contact_phone` solo existe en WhatsApp, así que los clientes de otros canales no se enlazan. El
   backfill de los clientes existentes es un CLI aparte, con `--dry-run` y resumible.
8. **Aviso de tratamiento de datos.** Cuando el negocio lo activa (apagado por defecto), el core
   anexa el aviso como `SafeExtras.safe_appends`: texto determinista y configurable del negocio, nunca
   del modelo, después de los guardrails, **sin bloquear la respuesta**, en la primera respuesta útil,
   una vez por cliente y por `version_aviso` (`sha256(texto)[:12]`; el texto completo queda en
   `AuditLog`). No se anexa en el canal de prueba. La marca «ya se avisó» es una fila
   `estado='informado'` en `customer_consents` con `origen='chat'` (ADR-0161 §3), insertada bajo el mismo candado por cliente que usa
   `facts._bloquear_cliente`, para que una ráfaga de mensajes no lo duplique. Si no se puede escribir
   esa fila, no se anexa el aviso.
9. **`customer_consents` generalizada (ADR-0161).** Una migración del core amplía
   `ck_customer_consents_proposito` a `('promociones','tratamiento_datos')`, `ck_customer_consents_estado`
   a `('otorgado','revocado','informado')` y agrega `version_aviso text`, exigida por CHECK cuando el
   propósito es `tratamiento_datos`. `core/consentimiento.py` es la única escritora (`registrar`,
   `vigente`, `estado_aviso`); `vigente()` ignora las filas `informado`. Estados efectivos: `sin_aviso`,
   `informado`, `otorgado`, `revocado`.
10. **`guardar()` como única puerta del perfil para el agente.** `datos_cliente.guardar(ctx, valores,
    accion)` envuelve a `escribir_hechos`, que sigue siendo el único escritor de `memory_facts`:
    - Las claves `requerido` de la acción en curso se guardan siempre (lo operativo).
    - Las demás exigen consentimiento `otorgado`; si no, la tool responde `requiere_aceptacion` y no
      guarda.
    - Un test estático exige que las tools del agente escriban perfil solo por `guardar()`.
    - Las ediciones del operador desde el panel (`PATCH /perfil`) no se bloquean.
    - La aceptación la registra la tool `responder_aviso_datos(decision)`, que solo procede si existe
      un `informado` de la versión vigente en esa conversación; la evidencia es el id del último
      mensaje entrante, leído de la base de datos y no del modelo, y el origen es `chat`.
11. **Revocar corta, no borra los packs.** Revocar `tratamiento_datos` impide guardar extras y retira
    los hechos `pii` con `facts.retirar_hechos` (nueva, dentro de `facts.py`). La supresión dentro de
    los packs depende de ADR-0164 (`BaseModule.suprimir_titular`), que **todavía no existe en el
    código**: mientras tanto queda un evento en `AuditLog` y un aviso al dueño de supresión pendiente.
    No se borran pedidos, contratos ni cartera, que la ley puede obligar a conservar.
12. **Este ADR no afirma cumplimiento de la Ley 1581.** La validación legal sigue siendo la compuerta:
    P-29, HU-GTM-02 y la nueva P-37. Activar el aviso en un negocio real queda condicionado a ella; el
    código puede mergearse apagado.

## Riesgos asumidos

- **Aceptación alucinada.** El modelo podría llamar a `responder_aviso_datos` sin que el cliente haya
  aceptado. Mitigaciones: solo procede con un `informado` vigente en la conversación, la evidencia sale
  de la base de datos y el playbook exige una afirmación explícita. Antes de activarlo hace falta un
  caso de evals con respuestas ambiguas («ok», «dale»). El riesgo residual existe.
- **Documento en suscripciones.** Hoy solo alimenta la huella antiabuso y no se guarda
  (`inscripcion.py`). Si la política lo marca requerido, se pide pero no se persiste (`persistir=False`);
  guardarlo en el perfil sería otra decisión.
- **Defaults de commerce.** Los tres tipos de entrega piden `id_document`. Se respeta para no cambiar
  producción; «opcional para pedir un producto» se logra con el override del negocio.
- **Falsos negativos del enlace.** Un número local sin prefijo contra un `wa_id` internacional depende
  del país por defecto. Se prefiere no enlazar a enlazar mal; dos deudores con el mismo teléfono no se
  enlazan.
- **Escrituras concurrentes de `behavior_config`.** Los writers actuales hacen read-modify-write sin
  candado (riesgo que ya existe). El nuevo escritor sí bloquea; no empeora el estado actual.
- **Costo por turno.** La ficha suma hasta ~400 tokens de entrada en el segmento no cacheado (≈US$0,001
  al peor caso, típico 100–200 o cero). Se confirma contra el ledger.
- **Gobierno.** ADR-0150, ADR-0161 y ADR-0164 siguen *propuestos*; este ADR los usa y generaliza sin
  aceptarlos. Hay que decidir cuál se firma primero.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Tabla de overrides de la política | Un registro por clave y acción para algo que cabe en `behavior_config`; el plan B es una columna JSONB propia si los writers concurrentes causan pérdidas reales |
| Columnas o tablas de perfil por módulo | ADR-0153 ya lo rechazó: duplica la fuente y crea valores contradictorios |
| Política dentro del `PluginConfig` cifrado de cada pack | Obliga a una pantalla por pack y el core no puede leerla sin importar packs |
| Un cuarto mecanismo de «requeridos» | Hay cuatro; se unifica la respuesta y se reaprovechan como defaults |
| Política en el prefijo cacheado | El prefijo se versiona por agente compilado, no por la configuración del negocio: quedaría obsoleta al editar un override |
| Unificar personas entre canales | Revierte D-SPEC-33-5, arriesga fusionar a dos personas distintas y exige un ADR propio |
| Aviso como paso bloqueante previo a responder | Frena la atención del negocio; el dueño pidió responder y avisar en la misma respuesta |
| Marca «informado» como hecho de memoria | Los hechos se reescriben y `purgar_de_cliente` los borraría; la prueba legal necesita historia append-only |
| Hook en la creación de `Customer` para enlazar | `BaseModule` no tiene ese hook y se pagaría por cada cliente nuevo de cualquier negocio |
| Aceptación por palabras clave | Frágil con «ok» o «dale»; se prefiere la tool más un caso de evals de ambigüedad |

## Consecuencias

**Positivas:** una forma única de pedir datos, que el negocio ajusta sin tocar código; iconos que dicen
la verdad; listas por módulo sobre un endpoint existente; el agente reconoce al cliente y su historial;
la prueba del aviso es append-only y reutilizable por cualquier habilidad.

**Negativas / lo que aceptamos pagar:** una migración del core que solo amplía CHECKs y agrega una
columna; el test AST de ADR-0166 necesita una exclusión explícita para la clave no escalar
`datos_cliente`; cada pack mantiene tres funciones más; la ficha cuesta tokens por turno; la política
convive un release con los campos legados de cada tool.

**Qué haría que revisáramos esto:** que el abogado (HU-GTM-02, P-37) exija guardar el texto completo
aceptado por fila (pide una tabla de versiones); que la edición concurrente de `behavior_config` cause
pérdidas reales (pide una columna propia); que el costo de la ficha supere el 5 % del gasto por turno;
o que el dueño necesite unificar personas entre canales.

## Impacto en el código

- `core/datos_cliente/` (nuevo: modelo, registro, `evaluar()`, `guardar()` y lector/escritor de
  overrides) y `core/tools/datos_cliente_skill.py` (nuevo: `consultar_datos_cliente`,
  `consultar_historial_cliente`, `responder_aviso_datos`).
- `core/admin/lead_registry.py` (`LeadRelacion`), `core/api/routers/leads.py` (`con_datos`, filtro
  `agente`, `LeadCampoPerfil.required` desde la política) y los `providers.py` de commerce, calendar,
  suscripciones y cobranza.
- `core/llm/prompt_builder.py` (`PromptContext.relacion`), `core/orchestrator/loop.py` (ficha y aviso) y
  `core/tools/core_skill.py` (`update_customer_profile` con `para` y `guardar()`).
- `core/consentimiento.py` (nuevo, ADR-0161 §5), `core/memory/facts.py` (`retirar_hechos`),
  `core/db/models.py` (`CustomerConsent.version_aviso`) y una migración del core. El número se reserva al
  abrir el PR: `origin/feat/datos-prueba-por-modulo` ya usa 0115, así que probablemente sea 0116 con el
  `down_revision` ajustado para no crear dos heads.
- `core/config_registro/` y `tests/test_cfg02_registro_opciones.py` (`LECTORES` y exclusión motivada);
  `GET/PUT /admin/datos-cliente/politica` (sección `settings`).
- `packages/pack-commerce`, `pack-calendar`, `pack-subscriptions` (delegan a `evaluar()` y mantienen los
  campos legados) y `pack-cobranza` (enlace por teléfono y CLI de backfill).
- `docs/openapi.json` regenerado; tipos del panel y de la APK (`npm run api:types`).
- Playbook del kernel `datos_cliente` y el playbook de ventas (replicado en `solvershub-comercial`).
