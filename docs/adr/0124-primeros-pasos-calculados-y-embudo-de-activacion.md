# ADR-0124 — Primeros pasos calculados del estado real y embudo de activación

- **Estado:** aceptado
- **Fecha:** 2026-10-09
- **Enmienda:** ADR-0121 § 3.8. El canje del enlace (`POST /api/v1/auth/accept-invitation`) **abre la
  sesión** del dueño: devuelve el token como el login, en vez de un 204 seguido de `/auth/login`.
- **Contexto:** HU-SH-06 (SPEC-07). Se apoya en ADR-0110 (presets y `tenants.settings["preset"].pendientes`),
  ADR-0121 (alta automática, `tenants.settings["alta"]` y el enlace de la invitación), ADR-0047/0063 (RBAC por
  sección), ADR-0076/0077 (API `/api/v1`), ADR-0081 (invitaciones en la app) y HU-CONN-04 (salud de la
  conexión). D-SPEC-12-1 (tour) se resuelve con la opción recomendada A.

## Contexto

El alta automática (ADR-0121) deja un negocio creado y una invitación, pero nada guía al dueño hasta su
primer mensaje. El panel no tiene la página `/invitacion` del enlace, ni avisa las invitaciones pendientes
de una cuenta que ya existía. ADR-0110 dejó `pasos_de_activacion` y los packs «pendientes de configurar»
para que el panel los muestre, y nadie los lee. No hay forma de medir cuánto tarda un negocio en activarse.

## Decisión

Los primeros pasos se **calculan siempre del estado real** del negocio; lo único que se guarda es la
**primera vez** que se cumplió cada uno, con los segundos desde el alta.

### 1. Pasos y regla de cada uno (`core/primeros_pasos/reglas.py`, funciones puras)

| Paso | Se cumple cuando | En curso |
|---|---|---|
| `datos_negocio` | `app_settings.business_description` no está vacía | — |
| `whatsapp` | Una conexión de Meta `connected` cuyo último diagnóstico ve un número (`number_registered` en verde, HU-CONN-04), una sesión de WhatsApp Web vinculada o una conexión manual de Meta activa | `connected` sin número verificado, `degraded`/`error`, `pending`/`draft` o QR sin vincular |
| `catalogo` | Alguna habilidad habilitada tiene contenido cargado (§ 2) | — |
| `probar_asistente` | Hay una fila en `usage_ledger`: el asistente respondió un turno (en «Probar asistente» o con un cliente) | — |
| `equipo` | Hay una invitación a un colaborador (no revocada) o un colaborador vinculado; la del dueño no cuenta | — |
| `configurar_<pack>` | Uno por pack de `tenants.settings["preset"].pendientes` (ADR-0110); se cumple cuando el pack queda habilitado | — |
| `activar_preset` | Solo si el preset del alta quedó pendiente (`settings["alta"].preset.estado = pendiente`) **y** el plan no alcanza; se cumple cuando se aplica (HU-SH-04) | — |

Cada paso devuelve un **motivo** (clave estable que el panel traduce, nunca a la vista) y la **sección del
RBAC** con la que se resuelve. La API responde `puede_actuar` con la escritura efectiva del usuario en esa
sección: el panel no muestra acciones que darían 403 (constitución § 1.3).

### 2. Lo que aporta cada habilidad: un hook del pack, no lecturas del core

El core no lee tablas de los packs (misma regla que ADR-0121). `BaseModule` suma un hook opcional:

```python
async def contenido_cargado(self, tenant_id) -> ContenidoCargado | None
```

`ContenidoCargado` (`core/primeros_pasos/contrato.py`, lo único de ese paquete que importa un pack) dice el
tipo (`catalogo | servicios | cartera`), la cantidad, cuándo se cargó el primero y la sección del RBAC. El
core lo pide solo a las habilidades habilitadas en el `AgentSpec`, bajo el `tenant_scope` del negocio; un
pack que falla queda en el log y no tumba el checklist. Lo implementan `pack_commerce` (productos con una
variante activa), `pack_calendar` (tipos de cita activos) y `pack_cobranza` (clientes).

### 3. Hitos: `onboarding_hitos`

- Tabla de negocio (`tenant_id`, RLS forzada, policy de aislamiento), única por `(tenant_id, paso)` mientras
  no esté borrada. Guarda `completado_en`, `segundos_desde_alta` y `fuente`.
- `fuente = dato` cuando la hora sale del propio dato: la conexión, el primer producto, el primer turno, la
  primera invitación. `fuente = deteccion` cuando el dato no la tiene y vale la hora en que el core lo vio.
- El alta es `tenants.created_at`: la misma fila que el alta automática deja en `provisioning_requests` (lado
  del vendedor) y en `audit_log` como `negocio_creado`. `origen` distingue el alta automática de la manual.
- Además de los pasos se registran dos bordes del recorrido: `invitacion_aceptada` (el dueño quedó
  vinculado) y `primer_mensaje` (primer mensaje entrante de un cliente real, sin el playground).
- Se registra al evaluar: cada vez que se abre el Resumen (`GET /api/v1/admin/onboarding`) y en el barrido de
  `agent-core tenant embudo`. `INSERT … ON CONFLICT DO NOTHING`: dos evaluaciones a la vez no duplican.
  Cada hito nuevo deja `primer_paso_completado` en `audit_log` y el evento de log `onboarding_hito`.
- Si un paso se deshace (se cae el WhatsApp), el checklist lo muestra en curso y el hito queda: el embudo
  mide cuándo se llegó.

### 4. Embudo: `agent-core tenant embudo [--dias N] [--solo-automaticas]`

De las altas de la ventana: cuántas llegaron a cada hito, el porcentaje, la mediana desde el alta y
cuántas dentro de las 24 horas (métrica de SPEC-12). Antes evalúa cada negocio, así cuenta también lo que
nadie vio en el Resumen. Recorre los negocios uno por uno bajo su `tenant_scope` (la RLS no se salta).

### 5. Invitación y entrada

- **El canje abre la sesión** (enmienda de ADR-0121 § 3.8). La contraseña, la aceptación, la membresía y la
  `AdminSession` nueva van en la misma transacción. La respuesta trae `token`, `admin_id`, `tenant_id` y
  `expires_in`; el panel guarda la sesión en su cookie `httpOnly` y deja ese negocio activo. Probar el
  enlace equivale a probar el correo, igual que el código de recuperación (ADR-0098); el resto del canje
  (cuenta limpia, uso único, bloqueo progresivo, 409) no cambia.
- `GET /api/v1/admin/me` suma `invitaciones_pendientes`, con los mismos filtros que la lista (vigentes y de
  negocios activos), para avisarlas al entrar.

### 6. Recorrido guiado (D-SPEC-12-1 = A)

Tour propio de 5 pasos con los componentes del panel, sin librerías. Su estado (`completado | saltado`) y la
preferencia de plegar el checklist van en cookies no secretas del navegador: no son datos del negocio. Los
modales de ayuda y las guías por habilidad siguen siendo de SPEC-12.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Guardar el estado de cada paso (casillas que se marcan) | Se desalinea del estado real (una conexión que se cae seguiría «lista») y alguien tiene que marcarlas |
| Leer las tablas de los packs desde el core | Rompe la separación core/packs (ADR-0121); el hook deja a cada pack decidir qué es «contenido» |
| Un registro de fuentes como `fuentes_de_alta` | Con varios packs aportando a la vez, un hook opcional de `BaseModule` (como `seed_data`) es más directo |
| Registrar los hitos en cada módulo, al ocurrir | Invasivo (Meta, packs, playground, invitaciones) y frágil ante caminos nuevos; el cálculo ya sabe cuándo se cumplió |
| Hitos en `tenants.settings` (JSONB) | Tabla de sistema sin RLS; un `UPDATE` del preset o del alta podría pisar los hitos |
| Hitos solo en `audit_log` | Sin restricción de unicidad: dos evaluaciones a la vez duplican; consultarlo en cada Resumen es caro |
| Un cron que barra los negocios nuevos | No hace falta para medir: el CLI barre antes de agregar. Se agrega si el embudo pasa a un tablero |
| Panel que llame a `/auth/login` después del canje | El panel no conoce el correo (el token no lo trae) y serían dos pasos; el canje ya probó la identidad |
| Librería de tours (D-SPEC-12-1 B) | Estilo ajeno a `DESIGN.md` y peso extra |
| Estado del tour en la base | Es una preferencia de la interfaz, no del negocio; sin migración ni endpoint |

## Consecuencias

**Positivas:**
- el checklist no se puede desalinear: lo que ve el dueño es lo que hay;
- el embudo de activación se mide sin instrumentar cada módulo, con la hora del dato cuando existe;
- un pack nuevo aporta a «catálogo o servicios» implementando un hook, sin tocar el core;
- el dueño define su contraseña y cae en su Resumen en un solo paso.

**Negativas / lo que aceptamos pagar:**
- `GET /admin/onboarding` escribe (los hitos nuevos): es idempotente y no falla la lectura, pero un GET con
  efecto secundario es menos obvio. Está documentado en el router.
- La hora de `datos_negocio` es la última edición de la configuración, una cota y no el instante exacto.
- Un paso que nadie evalúa (ni Resumen ni CLI) no tiene hito hasta la próxima evaluación; con `fuente = dato`
  la hora sigue siendo la real.
- Los pasos fijos son cinco; un tipo de negocio con otro recorrido necesita cambiar las reglas del core.
- El estado del tour es por navegador: en otro equipo se vuelve a ofrecer.

**Qué haría que revisáramos esto:**
- que el embudo se quiera en un tablero del panel o con alertas (pasaría a un cron y a una vista agregada);
- que un tipo de negocio necesite pasos propios (pasarían al manifiesto del preset, `pasos_de_activacion`);
- que el enlace de la invitación vaya por otro canal además del correo (habría que revisar abrir la sesión).

## Impacto en el código

- `core/primeros_pasos/` (`contrato`, `reglas`, `calculo`, `hitos`) y `core/api/routers/onboarding.py`
  (`GET /api/v1/admin/onboarding`, sección `dashboard`).
- `core/mcp/types.py`: hook `BaseModule.contenido_cargado`. Packs: `pack_commerce`, `pack_calendar`,
  `pack_cobranza`. `docs/PLUGIN-CONTRACT.md`.
- `core/db/models.py` y `alembic/versions/0062_onboarding_hitos.py`.
- `core/admin/invitations_service.py` (`canjear_invitacion` devuelve la sesión; `contar_pendientes` con los
  filtros de la lista), `core/admin/auth.py` (`nueva_sesion`), `core/api/routers/auth.py` y `me.py`.
- `cli/commands/tenant.py`: `agent-core tenant embudo`. `docs/openapi.json`.
- Panel (`solvershub-panel`): `/invitacion`, `/recuperar`, invitaciones pendientes en el layout, primeros
  pasos y recorrido en el Resumen.
