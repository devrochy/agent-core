# ADR-0174 — Dispositivos de la cuenta y alertas de acceso en el core

- **Estado:** propuesto (se acepta al mergear el PR de HU-ACC-01; la retención de `access_events` queda «por confirmar», P-36 del repo de docs).
- **Fecha:** 2026-10-27
- **Contexto:** SPEC-42 del repo de docs (D-54, REQ-ACC-01 a REQ-ACC-04; `04-fase-1-competir/SPEC-42-dispositivos-y-alertas-de-acceso/`).
- **Usa:**
  - ADR-0057 (rutas públicas e identidad de pack: la API de campo autentica con su propio JWT firmado por negocio);
  - ADR-0066 (el gestor es un `Admin` del core con perfil del pack);
  - ADR-0097 (el core no importa de los packs: el pack se registra, el core no lo conoce; y el patrón de cursor);
  - ADR-0099 (`push_tokens`: tabla distinta de la de este ADR, no se mezclan);
  - ADR-0105 (bloqueo progresivo de login en Redis);
  - ADR-0167 (`notify()` y la categoría `sistema`, para el gancho de avisos).
- **Enmienda:**
  - **ADR-0068** §1 (la vinculación es `gestor_id`↔equipo en el pack: pasa a `admin_id`↔equipo en el core), §2 (la política «solo pendiente no bloquea» pasa a política por negocio y rol) y §6 («sin infraestructura de alertas nueva»: se reemplaza por la tabla `access_events`).
  - **ADR-0081** §3 («la vinculación de dispositivo no aplica a tokens de acceso total»: la exención de owner y master pasa a política por negocio y rol; solo el master de plataforma queda exento).
  - Ningún ADR aceptado se edita: las enmiendas se registran al aceptarse este (nota de modificación en 0068 y 0081, como hizo 0081 con 0057/0066).

## Contexto

Lo que existe hoy, verificado en el código del backend y de la APK:

- **El vínculo es de la persona equivocada.** `pack_cobranza_devices` liga `gestor_id`↔`imei`, no cuenta↔equipo. El dueño, el master y el colaborador que entran por la APK no pueden vincular ni son chequeados (ADR-0081 §3 los exime).
- **El «IMEI» es el Android ID** (`Application.getAndroidId()`, ADR-0068 §3), un valor que cambia con un reinicio de fábrica o al cambiar la firma de la app. Es un vínculo débil y se asume (ver «Límite honesto»).
- **El chequeo ocurre solo en el login de campo** (`_enforcement_dispositivo` en `pack_cobranza/api_gestor.py`). El refresh de 30 días no revalida y el token no lleva el equipo: bloquear un equipo no corta una sesión ya abierta.
- **Hueco que abre el acceso.** Con un único `Device` en estado `bloqueado`, `activos_imei` queda vacío, no hay legado activo ni pendiente y la función devuelve «sin vínculo, no bloquea»: bloquear **abre** el acceso en vez de cerrarlo.
- **500 por duplicado.** `POST /gestor/dispositivo` busca por `(gestor_id, imei)` pero el único real es `(tenant_id, imei)` (`uq_pack_cobranza_device_imei`): si otro gestor del negocio usa el mismo equipo, el insert viola el único y responde 500.
- **«Alertas de acceso» es solo lectura** de tres escritores (`IntentoAcceso`): sin login fallido, sin bloqueo registrado por negocio (ADR-0105 §5 deja el login de identidad en logs) y sin aviso a nadie.
- **La APK confunde** un 403 por dispositivo con «sin acceso de campo» y manda al usuario al panel.
- **Equipo no muestra nada del gestor** (ADR-0175 lo resuelve aparte).

Decisiones del dueño (2026-10-27, D-54): alcance **solo APK**; política **por negocio** abierta o restringida; alertas en el core **sin push** en esta entrega; el master de plataforma nunca se bloquea.

## Decisión

**El vínculo cuenta↔equipo y el registro de eventos de acceso son capacidades del core, por `admin_id`, para todos los roles que entran por la APK; el pack de cobranza los consume.**

### 1. Dispositivos de la cuenta (`account_devices`)

Tabla de negocio nueva en el core (`tenant_id` + RLS forzada + policy con `nullif`, invariante 1). Una fila es «esta cuenta, en este equipo, en este negocio»:

| Campo | Tipo / regla |
|---|---|
| `id`, `tenant_id` | uuid; RLS forzada |
| `admin_id` | FK a `admins`; la cuenta (no el perfil de gestor) |
| `device_id` | texto; el Android ID en claro (hay que mostrarlo al aprobar; los listados enseñan solo los últimos 6) |
| `device_label`, `plataforma` | etiqueta legible; CHECK `android` \| `ios` |
| `estado` | CHECK `pendiente` \| `activo` \| `bloqueado` |
| `origen` | CHECK `autoregistro` \| `politica_abierta` \| `alta_manual` \| `migrado_pack` |
| `rol_al_registrar` | `owner` \| `colaborador` \| `gestor` (informativo; la política se evalúa con el rol vigente) |
| `bloqueo_equipo` | bool; con `true`, el bloqueo vale para todas las cuentas de ese equipo en el negocio |
| `legacy`, `legacy_pack_device_id` | bool y uuid; filas migradas del pack, ver §7 |
| `ultimo_uso_at`, `ultima_ip` | último check-in |
| `aprobado_por/at`, `bloqueado_por/at`, `motivo` | trazabilidad de las transiciones |
| `deleted_at`, `created_at`, `updated_at` | baja lógica (invariante 8) |

- CHECK `device_id IS NOT NULL OR legacy`: solo una fila heredada del pack puede no tener equipo hasta su primer login.
- Único parcial `(tenant_id, admin_id, device_id) WHERE deleted_at IS NULL AND device_id IS NOT NULL`: es por **cuenta**, así que dos gestores con el mismo teléfono ya no chocan (cierra el 500). Único parcial `(tenant_id, legacy_pack_device_id) WHERE legacy_pack_device_id IS NOT NULL`: la migración es idempotente.
- Índices `(tenant_id, estado)`, `(tenant_id, device_id)`, `(tenant_id, admin_id)`.
- **Ciclo:** `pendiente → activo` (aprobar), `activo → bloqueado` (bloquear), `bloqueado → activo` (reactivar). La baja (`deleted_at`) solo sale de `pendiente` o `activo`; un `bloqueado` responde 409, porque si se pudiera dar de baja, en política abierta el equipo volvería a registrarse y el bloqueo se evaporaría.
- **Equipo compartido:** un mismo `device_id` con varias cuentas del mismo negocio está permitido (evento `dispositivo_compartido`, nivel info). `bloqueo_equipo = true` deniega a todas.

### 2. Política por negocio y rol

`tenants.settings.acceso.dispositivos = {owner, colaborador, gestor}`, cada valor `abierto` o `restringido`. **Por defecto:** owner = `abierto`, colaborador = `abierto`, gestor = `restringido` (el gestor conserva hoy el efecto de ADR-0068; los demás ganan registro y aviso sin que nadie quede fuera).

- `abierto`: el equipo desconocido se **registra** (`activo`, origen `politica_abierta`) y se **permite**, con evento. Una fila `pendiente` se promueve a `activo` al volver a entrar. **Un `bloqueado` siempre deniega.**
- `restringido`: el equipo desconocido se crea `pendiente` y se **deniega** hasta que una persona autorizada lo apruebe.
- **Master de plataforma:** nunca se registra por negocio ni se bloquea; solo deja el evento `acceso_master`.
- **Quién cambia la política:** solo el owner del negocio o el master (`PUT /access/policy`). Si el cambio dejaría a algún owner sin un dispositivo `activo` propio, exige `confirmar: true`. Un owner no puede bloquear el equipo desde el que está llamando (409).
- **Escritura:** `jsonb_set` bajo `SELECT … FOR UPDATE` de la fila del negocio, para no pisar banderas del master que otros caminos reescriben (el candado de `tests/test_settings_del_negocio_candado.py` lo vigila). Cada cambio escribe `politica_cambiada` y una fila en `audit_log`.
- No es una opción del registro de ADR-0166 (cuya prueba de consistencia solo cubre `behavior_config` y `escalation_config`): vive en `tenants.settings.acceso`, como `settings.onboarding`. Si el registro la quiere describir, es una HU de SPEC-37, no de esta.

### 3. Enforcement

- **Servicio `core/access/devices.py`:** `evaluar(tenant_id, admin_id, rol, device_id, label, canal, ip) -> Decision`. El pack de cobranza lo **importa** (dirección permitida: el pack depende del core); el core **nunca** importa del pack (invariante 5 y su lectura en ADR-0097).
- **Regla de decisión,** en este orden:
  1. master de plataforma → permite, evento `acceso_master`;
  2. sin `device_id` → `restringido`: deniega `dispositivo_requerido`; `abierto`: permite con evento `dispositivo_sin_id`;
  3. la fila exacta `(tenant, admin, device_id)` en `bloqueado` (o el equipo con `bloqueo_equipo`) → **deniega en ambas políticas**, `dispositivo_bloqueado` (esto cierra el hueco de hoy);
  4. fila `activo` → permite (actualiza `ultimo_uso_at`);
  5. fila `pendiente` → `restringido`: `dispositivo_pendiente`; `abierto`: la promueve y permite;
  6. sin fila → `restringido`: crea `pendiente`, evento `dispositivo_pendiente`, deniega; `abierto`: crea `activo`, evento `dispositivo_nuevo`, permite.
- **Idempotencia:** el alta es `INSERT … ON CONFLICT` sobre el único parcial (nunca 500 por duplicado); las transiciones llevan el estado esperado y devuelven 409 si cambió (`esperado` en el cuerpo).
- **Cómo llega el equipo:** header `X-Device-Id` en **todas** las peticiones de la APK, incluido el refresh. Durante una release, y solo mientras `access_legacy_param_total` siga arriba de cero, se acepta el `imei` del cuerpo del login de campo (APK vieja).
- **Token de campo:** lleva el claim `did` (el `device_id` evaluado). El refresh **revalida**: vuelve a evaluar el equipo del header contra el claim y contra la fila. `acceso_de_campo` consulta por `did` con **caché Redis de 60 s**, invalidada al escribir en `account_devices`: un bloqueo corta una sesión abierta en menos de un minuto.
- **Dependencia del principal del core:** aplica una evaluación rápida (solo lectura, sin crear filas) **únicamente si llega `X-Device-Id`**. Quedan exentos `POST /access/device-session`, `/auth/*` y `/admin/me`, para que un equipo pendiente pueda consultar su estado y cerrar sesión. Un equipo con header y sin fila: `restringido` deniega `dispositivo_no_autorizado`; `abierto` permite (lo registra `device-session` o el login).
- **Códigos 403 con `code`:** `dispositivo_pendiente`, `dispositivo_bloqueado`, `dispositivo_no_autorizado`, `dispositivo_requerido`.
- **Falla cerrado, a propósito.** Redis caído: se lee la base; nunca se «abre» un negocio `restringido` por una caída de caché. Es **lo contrario** de ADR-0105 (el bloqueo de login falla abierto con Redis caído). Son capas distintas: el lockout es una defensa **complementaria** contra fuerza bruta y no puede tumbar el acceso legítimo; el chequeo de equipo es la **barrera** de la política y, si fallara abierto, la política restringida dejaría de existir justo cuando el sistema está degradado.
- **Compatibilidad:** `_enforcement_dispositivo` pasa a delegar en `evaluar`. La APK vieja (solo `imei` en el cuerpo) mantiene su contrato (tokens + `requiereVinculacion`); la APK nueva (header) recibe el 403 con `code`.

### 4. Eventos de acceso (`access_events`)

Tabla de negocio nueva, **append-only** (solo se permite la baja lógica por retención). Reemplaza «sin infraestructura de alertas nueva» de ADR-0068 §6:

| Campo | Notas |
|---|---|
| `id`, `tenant_id`, `occurred_at`, `request_id` | RLS forzada |
| `admin_id` (null), `device_row_id` (null), `device_id` | quién y qué equipo |
| `rol`, `canal` | `canal` ∈ `campo` \| `panel_apk` \| `campo_v1` |
| `tipo` | `login_fallido`, `login_bloqueado`, `dispositivo_nuevo`, `dispositivo_pendiente`, `dispositivo_aprobado`, `dispositivo_bloqueado_usado`, `acceso_denegado_politica`, `dispositivo_sin_id`, `dispositivo_compartido`, `politica_cambiada`, `acceso_master`, `legado_pack` |
| `severidad` | `info` \| `aviso` \| `critico` |
| `ip`, `detalle` (jsonb) | `detalle` sin contraseñas ni tokens |
| `deleted_at` | solo para la retención |

- Índices `(tenant_id, occurred_at DESC, id)` (keyset), `(tenant_id, severidad, occurred_at DESC)`, `(tenant_id, admin_id, occurred_at DESC)`.
- **Eventos sin negocio resuelto** (un login de identidad con un usuario inexistente): no hay `tenant_id`, así que **no** van a `audit_log` (exige `tenant_id`, ADR-0105 §5) ni a esta tabla: quedan en log estructurado y en métricas, como `auth_login_blocked`.
- `login_fallido` con una cuenta existente: una fila por negocio donde la cuenta tiene membresía (tope 10). Deduplicación de 5 minutos por `(tenant, admin, device, tipo)` para que un ataque no inunde la tabla.
- **Retención propuesta (por confirmar, P-36):** baja lógica a los 180 días en lotes por negocio, igual que ADR-0167 §8. ADR-0097 no define retención para `mobile_audit_events`, así que no es precedente.
- **Gancho para el centro de notificaciones (SPEC-38), sin implementarlo:** cursor `(occurred_at, id)` estable y `severidad` mapeable a la severidad de ADR-0167 (`aviso` → `warning`, `critico` → `critical`, `info` → `info`), categoría `sistema`. Un emisor futuro llamará `notify()` desde `registrar_evento`; **no se abre un canal paralelo de push ni de correo**.

### 5. API del core (`/api/v1/access`)

| Operación | Ruta | Permiso |
|---|---|---|
| Registrar o sondear el equipo de la sesión | `POST /access/device-session` `{deviceId, label, plataforma, appVersion}` → `{estado, politica, permitido, dispositivoId, requiereVinculacion}` | cualquier admin del negocio, solo sobre su propio `admin_id` (el del principal) |
| Listar | `GET /access/devices?estado&adminId&cursor` | `devices:read` |
| Alta manual | `POST /access/devices` | `devices:write` |
| Aprobar o reactivar | `POST /access/devices/{id}/aprobar` `{esperado}` | `devices:write` |
| Bloquear | `POST /access/devices/{id}/bloquear` `{motivo, alcance: cuenta \| equipo, esperado}` | `devices:write` |
| Dar de baja | `POST /access/devices/{id}/dar-de-baja` | `devices:write` |
| Eventos | `GET /access/events?severidad&tipo&adminId&desde&cursor` (keyset) | `access_alerts:read` |
| Leer / cambiar política | `GET /access/policy`, `PUT /access/policy` | lectura: admin del negocio; `PUT`: owner o master |

### 6. Autorización

- **Secciones nuevas del RBAC:** `devices` y `access_alerts`, **delegables** a colaboradores (ADR-0047/0136). La migración copia las concesiones existentes `pack_cobranza:dispositivos → devices` y `pack_cobranza:alertas → access_alerts` para que nadie pierda el acceso.
- Las acciones sobre dispositivos de un owner o del master solo las puede hacer un owner o el master. Un colaborador con `devices:write` no puede bloquear al dueño.
- Cada transición audita en `audit_log` y escribe su evento.

### 7. Transición sin romper test

1. Las rutas del pack `GET /pack/cobranza/devices`, `GET /pack/cobranza/alerts` y `POST /gestor/dispositivo` pasan a **wrappers finos** sobre el servicio del core, con el header `Sunset`, durante una release. El `POST /devices` v1 (código + API key, `api.py`) se queda en el pack. El admin SSR (`admin.py`) no se toca (ADR-0076).
2. Los wrappers aceptan la clave de permiso nueva o la vieja.
3. **Migración de datos del pack** (`pack_cobranza_0018`, idempotente, itera por negocio con `set_config`): copia a `account_devices` los `Device` con `gestor_id` y `Gestor.admin_id` no nulos y no borrados, **conserva el estado, incluido `bloqueado`**; los activos sin `imei` entran como `legacy = true` y se rellenan en el primer login. Copia los últimos 90 días de `intentos_acceso` a `access_events` como `legado_pack`. Emite `RAISE NOTICE` con migrados y omitidos y verifica que la suma cuadre. Tras ella, el pack solo escribe en el core y su tabla queda **congelada, sin borrado físico**.
4. **Red de seguridad si la 0018 falla** (un fallo de migración de un pack no aborta los demás): `adoptar_legado` adopta la fila del pack de forma perezosa al evaluar.
5. Los consumidores se mueven en este orden: backend → panel → OTA de la APK.

### 8. Alcance y límite honesto

- **Solo la APK.** El panel web no envía `X-Device-Id`, no se vincula y no se bloquea. Las peticiones sin header no se evalúan.
- **Para el owner y el colaborador, esto es higiene, no barrera:** sirve para enterarse de un teléfono nuevo, perdido o compartido y cortarlo, pero quien tiene sus credenciales entra igual por el panel web. **Para el gestor sí es una barrera real** sobre la API de campo (su cartera, cobros y rutas pasan por el token con `did`). Si el owner además le concede secciones del panel, esas secciones no quedan cubiertas. La pantalla de política lo dice con esas palabras; no se vende como seguridad que no es.
- **El Android ID es un vínculo débil** (cambia con el reinicio de fábrica o la firma). Lo aceptamos; la atestación (Play Integrity) es la evolución y queda fuera.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Dejar los dispositivos en el pack y ampliar el rol | El core tendría que importar del pack (invariante 5) y un negocio sin cobranza no tendría vínculo ni alertas; el vínculo de seguridad de una cuenta no es de dominio de cobranza. |
| Una columna `device_id` en `admins` o en `tenant_user` | Un teléfono por cuenta (reemplazo y segundo equipo imposibles) y sin estado, historial ni aprobación. |
| Política única para todos los roles | O deja al gestor sin barrera o encierra al dueño en su primer teléfono; el dueño pidió abierta o restringida por rol. |
| Restringido para el owner por defecto | Un dueño que cambia de teléfono quedaría fuera de su APK sin nadie que lo apruebe (el único aprobador es él). |
| Bloquear también el panel web | Fuera de alcance (D-54) y sin identificador de equipo fiable en un navegador. |
| `access_events` en `audit_log` | `audit_log` es de acciones administrativas y exige `tenant_id`; los eventos de acceso tienen otro volumen, otra retención y otro índice (keyset por tiempo). |
| Alertas con push desde el primer día | Abriría un canal paralelo al centro de notificaciones (SPEC-38); el gancho deja el cursor y la severidad listos. |
| Cachear la decisión sin invalidar | Un bloqueo tardaría lo que dure el TTL en cortar una sesión; la invalidación al escribir lo baja a segundos. |
| Fallar abierto con Redis caído (como ADR-0105) | La política restringida dejaría de proteger al gestor justo en una degradación. |

## Consecuencias

**Positivas:** cualquier cuenta de la APK queda registrada y auditable por negocio; un equipo bloqueado deja de entrar en ambas políticas y la sesión abierta se corta en menos de un minuto; se acaban el hueco del bloqueo y el 500 del equipo compartido; el dueño ve y administra todos los equipos y alertas desde una sola sección, no solo los de cobranza; el feed queda listo para el centro de notificaciones.

**Negativas / lo que aceptamos pagar:** dos tablas nuevas con RLS y una migración de datos del pack que debe salir bien una vez (con la adopción perezosa de respaldo); dos caminos conviven más de una release hasta que `access_legacy_param_total` llegue a cero; un lector de Redis más en el camino caliente de la API de campo y del principal (mitigado por el caché y por evaluar solo con el header); el Android ID débil; el panel web sigue abierto; el volumen de `login_fallido` exige la deduplicación y la retención.

**Qué haría que revisáramos esto:** que se exija atestación de hardware (Play Integrity) para algún rol; que el panel web deba vincularse o bloquearse; que un negocio necesite más de dos políticas por rol o por persona; que el volumen de `access_events` justifique particionar o mover a una tabla de plataforma; o que SPEC-38 pida más de un aviso por severidad (preferencias por tipo).

## Impacto en el código

- **Core:** `core/db/models.py` (`AccountDevice`, `AccessEvent`), migración Alembic nueva (el número es **el siguiente libre al abrir el PR**: hoy la última del core es `0112`, así que sería `0113`; no escribir `0104`) que crea ambas tablas y copia las concesiones de permiso; `core/access/` (`devices.py`, `events.py`, `policy.py`); `core/api/routers/access.py`; dependencia del principal en `core/api/deps.py`; secciones `devices` y `access_alerts` en el RBAC; `docs/openapi.json`; `changelog.d/`.
- **Pack de cobranza:** `api_gestor.py` (`_enforcement_dispositivo` y `/gestor/dispositivo` delegan en el core; claim `did`; refresh), `auth.py` (`acceso_de_campo` con `did` y caché), `api.py`/`admin_api.py` (wrappers con `Sunset`), migración `pack_cobranza_0018` (la última hoy es `0017`), permisos `dispositivos` y `alertas` (siguen aceptados una release).
- **Métricas y logs:** `access_device_decisions_total`, `access_events_total`, `access_enforcement_seconds`, `access_legacy_param_total`; structlog por decisión con `tenant_id`, `admin_id`, `decision` y `request_id`, sin el `device_id` completo.
- **Panel y APK:** HU-ACC-04 y HU-ACC-05; `openapi.json` → `schema.d.ts` en cada cliente.
- **Pruebas:** integración con Postgres y Redis (política por rol, bloqueado en ambas, revocación en menos de 60 s, refresh bloqueado, concurrencia sin 500, equipo compartido, PUT que deja al owner sin equipo) y la prueba de la migración (ver HU-ACC-02).

## Adenda — implementación de HU-ACC-02 (2026-10-27)

- **Claim `did`** = id de la fila de `account_devices` (no el id del equipo): opaco, buscable por clave primaria y sirve de clave de caché. Va en el token de acceso y en el de refresh.
- **Vigencia por petición**: `acceso_de_campo` (el único validador de la app de campo) llama a `core.access.sesion.exigir_equipo_vigente` para tokens de gestor. Caché Redis de 60 s por equipo (`acceso:equipo:{tenant}:{fila}`) y por cuenta (`acceso:cuenta:{tenant}:{admin}`); toda transición del servicio invalida las claves afectadas (un bloqueo «de equipo» invalida las de todas las cuentas con ese `device_id`). Redis caído → lectura de la base. La revocación llega en menos de 60 s aunque falle la invalidación.
- **Token sin `did`**: vale si la política del gestor es abierta o si la cuenta ya tiene un equipo activo con id (sesiones anteriores al cambio); en el resto solo llega a las rutas de vinculación (`GET/POST /gestor/dispositivo`).
- **Refresh**: header → decisión nueva sobre ese equipo; sin header → el `did` del token; sin ninguno → se hereda el equipo activo más reciente si la política lo permite.
- **Equipo compartido entre cuentas**: permitido (la segunda cuenta queda pendiente y se registra `dispositivo_compartido`); deja de ser el 409 de la red de seguridad previa.
- **Contrato**: con `X-Device-Id`, 403 `{error, code, estado}`; sin él, contrato anterior (tokens + `requiereVinculacion`), salvo que la cuenta ya tenga un equipo activo con id (403: los tokens abren toda la API).

## Adenda — implementación de HU-ACC-03 (2026-10-27)

- **Todos los roles**: `POST /api/v1/access/device-session` (`{deviceId, label?, plataforma?, appVersion?}` → `{estado, politica, permitido, dispositivoId, requiereVinculacion}`) registra o consulta el equipo de la cuenta que llama (siempre su propio `admin_id`) y aplica la política **de su rol**: `owner`, `colaborador` o `gestor`. El master de plataforma devuelve `politica: "plataforma"`, no crea fila y solo deja el evento `acceso_master`.
- **Rol de acceso**: `owner`/`master`/`colaborador` salen del core; `gestor` lo aporta el pack por el hook opcional `PackModule.rol_de_acceso` (el core no importa del pack). Si un pack falla o pasa de 1 s se asume `gestor`, la política más estricta: ante la duda no se abre el acceso.
- **Dependencia del principal**: si la petición trae `X-Device-Id`, `get_principal` llama a `evaluar_rapido` (solo lectura: no crea filas ni eventos) y corta con 403 `{error:{code,message}}` (`dispositivo_bloqueado`, `dispositivo_pendiente`). Sin header no se consulta nada: el panel web no cambia. Exentas: `/access/device-session`, `/admin/me`, `/auth/*` y el intercambio `…/gestor-token` (es el propio login de campo y decide el equipo él mismo). Caché Redis de 60 s por `(negocio, cuenta, equipo)`, invalidada por las transiciones y por `evaluar`; Redis caído → lectura de la base.
- **Login del propietario/master en el pack**: el token de propietario también lleva `did`, se valida en `acceso_de_campo` y se revalida en el refresh bajo la política `owner`.
- **Cambio de contrato respecto de la adenda de HU-ACC-02**: un login de campo sin `X-Device-Id` ni `imei` en política **restringida** es ahora 403 `dispositivo_requerido`; antes devolvía tokens con `requiereVinculacion`. Ninguna versión de la app entra así (la vieja manda `imei`), y esos tokens sin `did` solo servían para vincular, lo que sin id es imposible. En política abierta entra con el aviso `dispositivo_sin_id`.
- **Medición pendiente**: el coste por petición queda en la métrica `access_enforcement_seconds`; con la caché caliente es una lectura de Redis.
- **Límites conocidos de HU-ACC-03 (revisión de seguridad y de contrato, 2026-10-27)** — decisiones de producto o trabajo para HUs siguientes, no defectos ocultos:
  - *Header omitido*: el control del principal solo corre con `X-Device-Id`. Quien tiene un token válido y lo omite no es evaluado en `/api/v1` (para owner y colaborador es higiene, como dice §8). En la app de campo el token lleva `did`, pero mientras convivan las apps que mandan solo `imei` no se exige que el header coincida con el `did`; hacerlo (y retirar la herencia de equipo en el refresh) es el cierre de la convivencia, cuando `access_legacy_param_total` llegue a 0.
  - *Propietario en `restringido`*: la app vigente no tiene flujo de vinculación para el propietario; si el negocio endurece la política `owner`, un equipo nuevo recibe 403 `dispositivo_pendiente` y un refresh sin `did` 403 `dispositivo_requerido` (la app cierra sesión). El panel (HU-ACC-04) debe avisarlo antes de permitir ese valor.
  - *Dos formatos de 403*: `/api/v1` responde `{error:{code,message}}` y la app de campo del pack `{error, code, estado}`. La app nueva (HU-ACC-05) debe leer ambos.
  - *Caché de 60 s* (resuelto en HU-ACC-06 para la política): `PUT /access/policy` invalida `acceso:req`/`acceso:cuenta` de todo el negocio **después** del commit (`invalidar_negocio`). El cambio de rol de una cuenta sigue llegando en menos de 60 s salvo el paso a gestor, que baja y invalida sus equipos.
  - *Alta de equipos sin tope* (resuelto en HU-ACC-06): tope de 10 equipos vigentes por cuenta y negocio (429 `demasiados_equipos`; los dados de baja no cuentan y un equipo ya registrado sigue entrando) y rate limit de 30 consultas por minuto a `device-session` por cuenta (429 `demasiadas_solicitudes`).
  - *Rol más laxo a más estricto* (resuelto en HU-ACC-06): `on_collaborator_role` (rol `gestor`) llama a `degradar_por_cambio_de_rol`: los equipos `activo` con origen `politica_abierta` vuelven a `pendiente` si la política del gestor es restringida; los aprobados por una persona se respetan.
  - *Cuentas que actúan como master sin serlo* (resuelto en HU-ACC-06): `rol_de_cuenta` trata al tester con el rol habilitado como `master` (no se registra ni se bloquea; solo `acceso_master`).

**Adenda HU-ACC-06 — eventos de login y retención (2026-10-30).**
- `login_fallido` y `login_bloqueado` se escriben en **cada negocio de la cuenta** (hasta 10; si el login era de un negocio concreto, solo en ese). Un identificador que no es de ninguna cuenta no escribe filas —sería un contador que cualquiera puede inflar— y deja solo un log. El `detalle` nunca lleva el identificador (a veces ahí se escribe la contraseña). Escritores: `POST /api/v1/auth/login` (canal `panel_apk`), el login unificado y el login por negocio del pack (canal `campo`) y el login v1 por código + API key (canal `campo_v1`, sin cuenta y sin `detalle`: nada de lo que se escribió en el formulario se guarda).
- El login unificado de campo del pack (`POST /auth/login`) no tenía bloqueo progresivo: ahora usa el mismo (scope `campo`, 5 fallos en 10 min → 15 min).
- **Dedupe**: un mismo evento (negocio, cuenta, equipo, tipo, canal) en 5 min no se repite. La tabla es append-only, así que se guarda en Redis (`acceso:dedupe:*`) y las repeticiones calladas se anotan como `repeticiones_previas` en el siguiente evento. Si Redis cae se escribe de más, nunca de menos (límite aceptado: con Redis caído el bloqueo de login tampoco corre, fail-open por diseño). Si una cuenta tiene más de 10 negocios, el fan-out prioriza aquellos de los que es dueña.
- **Retención**: tarea `core.access_events_retention` (diaria, 04:10) da de baja lógica (`deleted_at`) los eventos de más de 180 días, por negocio y en lotes de 1000. No hay métrica Prometheus nueva: el repo no usa `prometheus_client`; los conteos van por structlog (`access_event`).
