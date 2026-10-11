# Multi-tenant en agent-core

## Misión / visión

**Un solo código base sirve a todos los clientes.** agent-core nació como un
framework single-tenant ("un asistente = un despliegue", cada cliente con su
código); hoy la misión es la inversa: **cada cliente es un tenant** dentro del
mismo despliegue, aislado por `tenant_id` + RLS (ADR-0001). No se escribe código
por cliente: la personalización se resuelve en este orden — configuración →
pack → plugin del tenant → topología de despliegue. Mismo binario siempre.

Consecuencias:

- **Alta de un cliente = configuración.** `agent-core tenant create` (o el panel
  `/admin/tenants/nuevo`) deja listo a un tenant: AgentSpec, canales, packs,
  admin. Cero código, cero scripts por cliente.
- **El framework es neutro.** No trae datos ni documentación de clientes reales.
  Todo lo específico de un tenant vive en `TENANTS_ROOT/<slug>/` (gitignored).
- **Escalar = subir de tenant**, no de repos. Los tests de aislamiento son
  genéricos: crecer en tenants nunca exige añadir tests.

## Qué es un tenant

Un tenant es un cliente con su asistente. En la BD:

| Qué | Tabla | Notas |
|---|---|---|
| El cliente | `tenants` | sistema, sin RLS |
| Su asistente | `agent_specs` | persona, tono, playbooks, packs (un activo por tenant) |
| Su identidad | `app_settings` | nombre, negocio, idioma, BYOK, kill-switch |
| Su canal | `channel_connections` | credenciales cifradas (Fernet) |
| Su vertical | `plugin_configs` + enablement | config del pack por tenant |
| Su plan | `tenant_subscriptions` | plan + período + trial (ADR-0045) |
| Acceso del admin | `tenant_user` | quién opera el tenant en el panel |

Aislamiento: **toda** tabla de negocio lleva `tenant_id uuid not null`, RLS con
política de aislamiento e índice compuesto que empieza por `tenant_id`
(invariante #1 de `CLAUDE.md`). Un tenant nunca ve filas de otro.

Las tablas del Agent OS (SPEC-26, migraciones `0067`–`0071`) suman una garantía más: cada
tabla padre tiene `UNIQUE (tenant_id, id)` y **toda** referencia a otra entidad del negocio es
una FK compuesta `(tenant_id, …)`, así que una fila de un negocio no puede apuntar a una de otro
aunque el código se equivoque (ADR-0116 §4). Las append-only (versiones, releases, eventos,
ledgers) usan el trigger `prevent_mutation()` y `app_runtime` solo tiene `SELECT`/`INSERT` en
ellas; ninguna tabla nueva le da `DELETE` (invariante 8).

### Tablas de sistema (sin `tenant_id` ni RLS)

Excepciones documentadas a la invariante #1: catálogos globales o datos que se leen antes de
tener un negocio en contexto. La migración que las crea lleva la marca "Tabla de SISTEMA" (la
reconoce `.claude/hooks/guards.py`) y explica el motivo.

| Tabla | Por qué es de sistema |
|---|---|
| `tenants` | el propio negocio. Las banderas del Agent OS (`aos_runtime`, `aos_procedures`, ADR-0112) viven aquí y solo las edita el master |
| `plans` | catálogo de planes (ADR-0045) |
| `connection_types` | catálogo de tipos de conexión (ADR-0069) |
| `pack_versions` (+ vista `agent_templates`) | contenido versionado de las habilidades de la plataforma (ADR-0118 §1); los plugins del negocio no entran. Inmutable salvo `yanked`; `app_runtime` solo tiene `SELECT` |
| `webhook_events`, `meta_app_subscriptions`, `admin_permissions`, `tenant_invitations`, `platform_meta_app_requirements` | se leen antes de resolver el negocio o son de la plataforma |

Ninguna **vista** de `public` puede tener columna `tenant_id`: `purge_tenant` recorre
`information_schema.columns` y la trataría como tabla (lo verifica
`tests/integration/test_agent_os_esquema.py`).

## `TENANTS_ROOT` — los artefactos de cada tenant viven fuera del código

Los datos y la documentación específica de cada cliente no van al repo del
framework. Viven en `TENANTS_ROOT` (default `./tenants`, gitignored):

```
tenants/
  <slug>/
    catalog.json     # datos del tenant importables a la BD (seed) — formato del pack
    docs/            # documentación específica del tenant (README, config, changelog)
    evals/           # golden set del tenant (spec.json + scenarios/) para `eval-runner --tenant`
    playbooks/       # override de instrucciones por tenant (ADR-0026), ver abajo
```

- `TENANTS_ROOT` se configura en `.env` (`core/config.py`). Por defecto es
  relativo al repo; puede apuntar a un directorio compartido.
- La documentación del **framework** vive en `docs/`; la de cada **tenant**, en
  `TENANTS_ROOT/<slug>/docs/`. No se mezclan.
- `TENANTS_ROOT/<slug>/playbooks/<nombre>.md` (ADR-0026): si existe, gana
  sobre el `.md` default del pack/core para ESE tenant — `<nombre>` tiene
  que ser uno ya permitido en su `AgentSpec.playbooks` (el override no
  agrega playbooks nuevos, solo reemplaza el contenido de uno existente).
  Es 100% filesystem, gestionado por quien tenga acceso al despliegue
  (mismo nivel que colocar `catalog.json` antes de `tenant seed`) — no hay
  editor en el panel admin (ADR-0017 sigue vigente: sin autoservicio de
  instrucciones ahí).

## Flujo

```
agent-core init              # bootstrap del despliegue (una vez por repo/BD)
  └─ agent-core tenant create <cliente>   # alta (CLI o panel)
       └─ agent-core tenant seed <slug>   # importa catalog.json del tenant
            └─ evals --tenant <slug>      # golden set del tenant antes de publicar
```

Un despliegue nuevo = `git clone` + `init` (BD vacía) + `tenant create` por cada
cliente. No hay "otro repo por cliente": hay otro **tenant**.

## Ciclo de vida de un tenant

Un tenant nace `active` y puede transitar a `suspended` y a `eliminado`:

| Estado | Efecto | Cómo |
|---|---|---|
| `active` | Procesa mensajes normalmente | alta (`tenant create`) o reactivación |
| `suspended` | Deja de procesar mensajes (webhook y consumer lo rechazan), **conserva sus datos** y sigue visible en el panel | `tenant deactivate` (CLI) o botón en `/admin/tenants` |
| eliminado | Borrado físico **irreversible** de todo su dato (core + packs + fila `tenants`; después, su estado Redis) | solo desde `suspended`: `tenant purge` o `/admin/tenants` (modal) |

**Suspensión** (`tenant deactivate`): reversible (`tenant activate`). Útil para
bajas temporales, impago, o incidentes. La enforcement es defensa en profundidad:
el webhook no encola (`core/gateway/webhooks.py`) y el consumer descarta eventos
ya encolados (`core/orchestrator/consumer.py`) vía `core.tenants.is_tenant_active`.

**Billing: operativo vs. no operativo (ADR-0045).** Independiente de `status`
(`active|suspended`), un tenant tiene un estado de billing dado por su plan y su
suscripción (`tenant_subscriptions`): todo tenant nuevo arranca en `free` con 30
días de trial. Si el trial/período vence o el pago no está activo, el tenant deja
de procesar mensajes (mismo gate webhook/consumer, vía `is_tenant_operational`),
sin borrar datos. Un tenant `active` puede estar así no operativo por falta de
pago. Ver `docs/BILLING.md`. El backfill de la migración `0034` dio a todos los
tenants existentes una suscripción `free` con 30 días desde la migración (no
desde `created_at`), para no cortarles el servicio de golpe.

**Eliminación** (`tenant purge`): exige que el tenant esté **suspendido**
(`tenants.status = 'suspended'`, con `tenant deactivate`), confirmar exactamente la
frase `ELIMINAR <slug>` y rol `master`. Un tenant activo no se purga: la CLI lo
rechaza antes de pedir la frase, el panel solo habilita "Eliminar" en los
suspendidos, la API responde 409 `tenant_not_suspended` y la función SQL lo vuelve
a exigir (defensa en profundidad: falla sin borrar nada). Cuenta la suspensión del
ciclo de vida, no la falta de pago (ADR-0045): un tenant `active` no operativo por
billing tampoco se purga. Es la **excepción deliberada a la invariante #8**
(sin borrado físico): el ciclo de vida del tenant la requiere (tipo GDPR /
right-to-erasure). La función SQL `purge_tenant(tid)` (SECURITY DEFINER, dueño,
transaccional) descubre dinámicamente todas las tablas con `tenant_id` — core,
packs y `meta_private` — y las vacía. Destruye también el
historial inmutable (`usage_ledger`, `audit_log`) del tenant. Mientras corre, la
función fija `session_replication_role = replica` (cláusula `SET`, se restaura al
salir), así que no disparan los triggers de las FKs ni los de inmutabilidad; la fila
`tenants` se borra con las FKs encendidas, para que las referencias desde otros
negocios (`provisioning_requests.negocio_tenant_id`, `ON DELETE SET NULL`) se
apliquen. El dueño no es superusuario: necesita `GRANT SET ON PARAMETER
session_replication_role` (migración `0072`, runbook de producción 4.3). `EXECUTE`
solo para `app_runtime`.

**Estado Redis en la purga.** Redis no entra en la transacción, así que se limpia
**después** del commit de la función SQL (y de verificar que la fila `tenants` ya no
existe): si la purga SQL falla y hace rollback, el negocio conserva intacto su estado
Redis (memoria de trabajo, debounce, marcadores de términos...). Si la limpieza de
Redis falla, la purga no se revierte: se registra `tenant_purge_redis_cleanup_failed`
(nivel error, con `tenant_id` y el comando de reintento; sin el slug), se manda una
alerta `critical`, `purge_tenant` devuelve `False`, la CLI sale con código 2 y el panel
SSR muestra un aviso (la API sigue respondiendo 204). El reintento idempotente es
`agent-core tenant purge-redis <tenant_id>` (solo UUID): se niega si el negocio todavía
existe o si el UUID aparece en Redis como id de un hijo de otro negocio (error de copiar
y pegar el id de una conversación, conexión o mensaje).

La limpieza borra las claves por conversación (ids leídos antes de purgar) y hace
`SCAN MATCH *<tenant_id>*` + `UNLINK` de las claves que llevan el UUID **en posición de
tenant** (el primer UUID del nombre). Las que lo llevan en otra posición no son suyas y no
se tocan (p. ej. `webhook_firma_ajena:{a}:{b}` de varios negocios). En los streams el
tenant va en los campos, no en el nombre: de `agent_core:inbound:dlq` la limpieza borra
(`XDEL`) las entradas cuyo campo `tenant_id` es el del negocio purgado; `agent_core:inbound`
no se toca, porque lo procesado ya se borró al ackear y lo pendiente lo descarta (y borra) el
guard del consumer, que no procesa negocios que ya no existen. Queda una ventana mínima: un
turno que ya estaba en curso al suspender y que falla después de la limpieza deja su entrada en
la DLQ hasta que vence su retención (no la ve otro negocio: el panel filtra por la sesión).

**Retención de los streams de entrada (ADR-0053).** `agent_core:inbound` y su DLQ llevan
el texto y el teléfono de los clientes, así que no se guardan más de lo necesario
(`core/orchestrator/retencion_streams.py`):

- **Lo procesado se borra al ackear.** Tras un `XACK` exitoso el consumer hace `XDEL` de
  la entrada (también al mandarla a la DLQ, que guarda su propia copia). Un `XDEL` fallido
  no cuenta como fallo del turno: lo recoge el recorte periódico.
- **Recorte periódico** (cada 10 min, en cada réplica; es idempotente): `XTRIM MINID`
  exacto sobre `agent_core:inbound` con corte = el menor entre *ahora −
  `INBOUND_STREAM_RETENTION_HOURS`* (24 h por defecto), el pendiente más viejo del PEL de
  cada grupo y el siguiente al último entregado de cada grupo. Lo que queda antes del
  corte está ackeado por todos los grupos: **nunca se recorta lo pendiente (PEL) ni lo no
  leído**, aunque el consumer lleve horas caído. Sin grupos no se recorta nada. El precio
  es conservador: un pendiente viejo retiene también lo ackeado posterior a él hasta que
  se resuelva (el reclamo lo lleva a la DLQ en minutos); si el corte queda más de dos
  retenciones atrás se registra `inbound_trim_blocked_by_pending` (solo IDs). La hora es
  la de Redis (`TIME`), la misma que genera los IDs, no la de la réplica.
- **Primer despliegue:** el primer recorte borra de una vez lo acumulado. Es `XTRIM`
  exacto dentro del bucle del consumer; mirar antes `XLEN agent_core:inbound` en
  producción (con decenas de miles de entradas es rápido: libera nodos enteros).
- **No se usa `XADD MAXLEN`/`MINID`**: en Redis 7 el recorte al escribir no mira el PEL y,
  con atraso, borraría mensajes sin procesar.
- **DLQ:** `XTRIM MINID` por antigüedad, `INBOUND_DLQ_RETENTION_HOURS` (168 h = 7 días por
  defecto, mínimo 24 h): tiempo para revisar y reintentar desde el panel; pasada la ventana de 24 h de
  WhatsApp, reintentar un mensaje tan viejo ya no sirve para responderle al cliente.
- **Si se agrega otro consumer group** al stream: el corte ya lo respeta, pero el `XDEL`
  al ackear del grupo del orquestador le borraría lo que aún no leyó; habría que mover el
  borrado al recorte (o a Redis 8.2+, `XACKDEL`).

**Convención de claves Redis** (de ella depende que la purga sea completa y no toque a
otro negocio): todo estado Redis de un tenant lleva su UUID como **primer** UUID del nombre
(`<familia>:{tenant_id}:...`); una clave que solo lleva el id de la conversación debe tener
TTL de 24 h o menos (hoy: `debounce:payload|messages:{conv}`, 5 min; `wm:{conv}` legado,
24 h); y ningún nombre de clave lleva texto libre de usuarios.

## Referencias

- ADR-0001 multi-tenant pooled + RLS (`docs/adr/0001-multitenant-pooled-rls.md`)
- ADR-0012 contexto de tenant (auth/RLS) · ADR-0014 AgentSpec/playbooks
- ADR-0019 enablement por packs · ADR-0018 numeración por tenant
- ADR-0045 planes/cuotas/enforcement · ADR-0046 cobro de suscripción (Wompi)
- `docs/BILLING.md` — planes, cuotas y facturación
- `core/onboarding.py` — el servicio de alta (usado por CLI y panel)
- `cli/commands/tenant.py` — comandos `tenant create|list|seed|docs`