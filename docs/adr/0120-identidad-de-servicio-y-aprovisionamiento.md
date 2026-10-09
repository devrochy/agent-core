# ADR-0120 — Identidad de servicio y aprovisionamiento automático de negocios

- **Estado:** aceptado
- **Fecha:** 2026-10-09
- **Enmienda:** ADR-0081 § 4 (una invitación `owner` del alta automática puede llevar un enlace de un
  solo uso por correo y WhatsApp cuando la cuenta se creó sin contraseña; el resto de las invitaciones
  sigue siendo solo en la app) y ADR-0106 (`subscription.activated` suma `plan_code` y `variant_id`).
- **Contexto:** HU-SH-03 (SPEC-07, decisiones D-12, D-16, D-SPEC-07-1 = A y D-SPEC-07-2 = A). Se apoya en
  ADR-0107 (outbox `domain_events`), ADR-0106 (suscripciones v1), ADR-0110 (presets), ADR-0045 (planes),
  ADR-0048 y ADR-0081 (cuentas, membresía e invitaciones), ADR-0098 (correo de sistema), ADR-0108 (avisos
  por evento) y ADR-0012 (contexto de tenant).

## Contexto

Crear un negocio solo lo puede hacer el master con sesión humana (`core/api/routers/platform.py:28-30`
y `:114-147`). `pack_subscriptions` ya emite `subscription.activated` por el outbox
(`pack_subscriptions/capability.py:203-221`) pero nadie lo escucha (`core/events/core_handlers.py:188-196`),
el evento no dice qué plan de la plataforma se compró y el pedido no guarda vertical, negocio ni correo
(`pack_commerce/skills.py:1398-1402`). D-12, D-SPEC-07-1 = A y D-SPEC-07-2 = A piden el alta sin humanos.

## Decisión

Un consumidor interno del core sobre `subscription.activated`, habilitado solo para los negocios de una
allowlist, da de alta el negocio del comprador con los datos del pedido, de forma idempotente, y lo firma
con una identidad de servicio que no tiene credenciales.

### 1. Identidad de servicio `provisioning`

Es un principal de tipo `service` que **solo existe dentro del core** (`core/provisioning/__init__.py`):
no es una fila de `admins`, no tiene contraseña, token, sesión de panel ni permisos de RBAC, y ninguna ruta
de la API la acepta. Sirve para una sola cosa: firmar como `actor = "provisioning"` lo que hace el
consumidor en `audit_log` (en el vendedor y en el negocio nuevo). No existe un token de servicio que un
`AgentSpec`, una tool declarativa o una config de pack puedan guardar.

### 2. Quién puede vender negocios: `PROVISIONING_SELLER_TENANTS`

Lista separada por comas de **slugs o UUID** de los negocios vendedores (en la práctica, `solvershub`).
Vacía por defecto: el aprovisionamiento automático queda **apagado** (fail-closed). Un negocio cuenta como
vendedor si su id o su slug están en la lista y está activo y sin borrar. La variable es de entorno (no
del panel) para que ningún admin de negocio pueda darse permiso de crear negocios; tiene que estar en el
**worker**, que es donde corre el despachador del outbox. Se prefiere el UUID: el slug solo lo puede
cambiar el master.

### 3. Consumidor interno sobre `subscription.activated`

`core/provisioning/consumer.py` se suscribe al topic en el despachador del outbox (`ensure_core_handlers`).
Corre bajo el `tenant_scope` del evento, es decir, del vendedor:

1. **Solo vendedores.** Si el tenant del evento no está en la allowlist, no hace nada (ni lee ni escribe).
   El `tenant_id` del evento lo fija la RLS al insertarlo: un negocio no puede emitir eventos a nombre de
   otro.
2. **Registro e idempotencia.** Una fila en `provisioning_requests` (tabla de negocio **del vendedor**, con
   RLS forzada) por alta, única por `(tenant_id, referencia)` y por `(tenant_id, subscription_id)`. La
   referencia es el número público del pedido; una activación sin pedido (`admin-manual`, `journey`) usa
   `sub:<subscription_id>`. Un segundo evento con la misma referencia o suscripción no crea otro negocio.
   Un evento de **cambio de plan** sobre una suscripción ya aprovisionada solo se audita
   (`aprovisionamiento_cambio_de_plan`): sincronizar el plan del negocio es de HU-SH-04.
3. **Concurrencia.** Cada alta toma un candado consultivo de Postgres (`pg_try_advisory_xact_lock`) sobre
   su fila mientras trabaja; si otro worker la tiene, el evento se reintenta más tarde. El id y el slug del
   negocio nuevo son **deterministas** (UUIDv5 de vendedor + referencia; slug con un sufijo derivado del
   mismo par), así que un reintento después de una caída retoma el mismo negocio y nunca crea otro.
4. **Datos del alta: del pedido, no del texto.** El core define el contrato `DatosDeAlta` y un registro
   de fuentes (`core/provisioning/fuentes.py`, mismo patrón que el catálogo de planes de ADR-0111);
   `pack_commerce` lo implementa leyendo **su** pedido por referencia: plan de la plataforma (atributo
   `precio_de_plan` de la variante, HU-SH-01), vertical, nombre del negocio, correo y teléfono del owner, y
   si el pedido está pagado (Free nace pagado, D-16). El vertical se pide en la conversación como campo del
   checkout (`checkout_fields.plan` del negocio con `business_name` y `vertical`) y la tool lo valida contra
   la lista de presets (`enum` en el schema y validación en el servidor): es un dato de un enum, no texto
   libre. Si el negocio no lo pide, un valor fuera de la lista se descarta sin frenar el pedido, porque
   la tool es la misma para todos los negocios. El pedido lo guarda en `pack_commerce_orders.datos_alta`
   (JSONB, solo pedidos de planes). El evento suma `plan_code` y `variant_id` para elegir la línea
   correcta de un pedido con varios planes.
5. **Validación.** Plan de la plataforma conocido, correo válido y pedido pagado. Si el pedido todavía no
   es visible (commerce y el proveedor de suscripciones escriben en transacciones distintas), el evento se
   reintenta; al tercer intento, o si falta un dato que no va a llegar (correo, plan), el alta queda **en
   revisión** con el motivo, se audita y se avisa por push a los dueños del vendedor. Sin nombre de negocio
   se usa el del comprador. Un plan que no es de la plataforma deja la fila en `omitido`.
6. **Alta.** `onboard_tenant` (idempotente) con el id y el slug deterministas, sin packs sueltos. Antes se
   comprueba que el slug no sea de **otro** negocio: el consumidor nunca agrega un owner a un negocio que
   no creó (si pasa, revisión). Plan pago: `tenant_subscriptions` en `active` con el período pagado desde
   hoy (`interval_days` del plan), sin prueba, `provider = "asistente"` y referencia
   `plat-<slug>-<AAAAMM>`. Free: la suscripción `free` en `trialing` con los 30 días de prueba de siempre
   (D-16). `tenants.settings["alta"]` guarda el origen (vendedor, referencia, plan y vertical) para el
   onboarding (HU-SH-06).
7. **Preset.** `apply_preset(vertical, sembrar_ejemplos=False)`: un negocio real no arranca con productos de
   ejemplo que el asistente podría ofrecer a sus clientes. Si el plan no alcanza el `plan_minimo` (hoy todos
   los presets piden Basic o más, así que nunca con Free), si falta el vertical o si no existe, el negocio se
   crea igual y el preset queda **pendiente** con su motivo en el registro y en `tenants.settings["alta"]`.
8. **Owner e invitación** (D-SPEC-07-2, extiende ADR-0081):
   - Correo **sin cuenta**: se crea el `Admin` **sin contraseña** (un hash inválido que `verify_password`
     rechaza: nadie puede entrar) y una invitación `owner` con **enlace de un solo uso** que vence a los
     7 días. Al abrirlo, el owner define su contraseña y la invitación se acepta (`TenantUser` owner).
   - Correo **de una cuenta existente**: no se toca su contraseña ni se la vincula; se crea la invitación
     pendiente de siempre (ADR-0081) y el aviso le pide entrar y aceptarla. Un enlace que llega por
     WhatsApp no prueba que el dueño del correo consienta.
   - Correo de un master, de una cuenta con permisos globales o desactivada: revisión **antes** de crear
     el negocio (ADR-0081 no permite invitarlos), para no dejar un negocio sin dueño.
   - El enlace es `<PANEL_BASE_URL>/invitacion#<token>` (en el fragmento: no llega a los logs del proxy).
     El token es `<id de la invitación>.<HMAC-SHA256(APP_SECRET_KEY, id + nonce)>`; la base guarda solo el
     nonce (`tenant_invitations.token_nonce`). Derivarlo permite reenviar el mismo enlace en un reintento
     sin guardar el token. Canjearlo (`POST /api/v1/auth/accept-invitation`) es atómico (UPDATE
     condicional sobre `estado = 'pendiente'` y el nonce) y solo vale mientras la cuenta no tenga
     contraseña; si ya la tiene (por ejemplo, la recuperó con "olvidé mi contraseña"), entra y acepta en
     la app.
   - Entrega: correo de sistema (ADR-0098; sin SMTP configurado queda registrado como `sin_smtp`) y
     WhatsApp en la conversación del comprador con el vendedor, con el aviso por evento
     `provisioning.invitation` (ADR-0108: texto dentro de la ventana; plantilla aprobada fuera de ella, o
     `sin_plantilla`). Cada canal se marca al enviarse y un reintento no lo repite. Si no llega por
     ningún canal (sin SMTP y sin conversación de WhatsApp), el alta queda completada con ese aviso y
     un push a los dueños del vendedor, que pueden reenviar el acceso con "olvidé mi contraseña".
9. **Cierre y auditoría.** La fila pasa a `completado` con el negocio, el plan, el preset y el estado de
   la invitación, y `audit_log` registra `negocio_aprovisionado` en el vendedor y `negocio_creado` en el
   negocio nuevo, con actor `provisioning`, la referencia del pedido, el plan y el preset.

**Tiempo.** El despachador corre cada 30 s y el alta se completa en la primera entrega: de la activación al
negocio creado pasan menos de 2 minutos si no hay reintentos.

### 4. Sin el LLM

Ninguna tool expuesta al modelo crea negocios: ninguna usa `onboard_tenant` ni importa el consumidor. Lo
verifican un test que recorre todas las tools del core y de los packs (`tests/test_provisioning_sin_llm.py`)
y el contrato `alta-fuera-del-llm` de import-linter. El asistente solo
puede crear **pedidos** con sus guardas de siempre (cotización confirmada, pago real o Free confirmado); el
negocio lo crea el core al ver la suscripción activada. No hay token que guardar.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| B · Token de servicio con scope `platform:provision` usado por una acción configurable (HU-SUB-14) | Un token poderoso guardado en la config de un negocio (D-SPEC-07-1) |
| C · Tool declarativa HTTP con el token del master | El LLM podría crear negocios y el token queda en el `AgentSpec` |
| Que el evento traiga todos los datos del alta | Mezcla el pedido (vertical, negocio, correo) con el contrato de suscripciones de ADR-0106; el pedido sigue siendo la fuente |
| Leer las tablas de `pack_commerce` desde el core | Rompe la separación core/packs; el registro de fuentes mantiene al core sin nombrar packs |
| Contraseña temporal por WhatsApp | Secreto en un canal de mensajería (D-SPEC-07-2) |
| Token aleatorio guardado solo como hash (como ADR-0098) | Un reintento de entrega obligaría a rotarlo y reenviar los dos canales |
| Sembrar los datos de ejemplo del preset | El asistente de un negocio real podría vender productos que no existen |
| Allowlist en `tenants.settings` o en el panel | Un admin con acceso a la config podría convertirse en vendedor |

## Consecuencias

**Positivas:** el alta de un cliente no necesita al master; nadie guarda un token capaz de crear negocios;
un evento repetido, un pedido con dos planes o un worker que se cae no duplican negocios; cada alta queda
en una fila del vendedor (también las que necesitan a una persona) y en `audit_log` de los dos lados.

**Negativas / lo que aceptamos pagar:**
- El enlace por WhatsApp queda en el historial de la conversación del vendedor (y en el contexto del LLM de
  ese turno). Lo acotan el uso único, los 7 días y que deja de valer cuando el owner define su contraseña.
- Si el comprador escribe mal su correo, el enlace por WhatsApp le deja definir la contraseña de una cuenta
  con un correo ajeno, y el dueño de ese correo podría recuperarla después. Verificar el correo es parte
  del antiabuso (HU-SH-07).
- Rotar `APP_SECRET_KEY` invalida los enlaces pendientes (se reenvían a mano o con "olvidé mi contraseña").
- Un cambio de plan desde el asistente no cambia todavía el plan del negocio aprovisionado (HU-SH-04).
- Faltan los controles de HU-SH-07 (un Free por persona, términos, interruptor): hasta entonces la
  allowlist vacía es el interruptor.

**Qué haría que revisáramos esto:** un segundo vendedor con reglas distintas (pasaría a config por
vendedor), que el alta deba esperar a una verificación del correo, o volumen suficiente como para separar
la entrega de la invitación en su propio evento.

## Impacto en el código

- `core/provisioning/` (identidad, allowlist, contrato `DatosDeAlta` y registro de fuentes, consumidor,
  owner e invitación); `core/events/core_handlers.py` (suscripción al topic).
- `core/config.py` (`PROVISIONING_SELLER_TENANTS`), `.env.example`, `docs/despliegue-coolify.md`.
- `core/db/models.py` y `alembic/versions/0059_provisioning_requests.py` (`provisioning_requests` con RLS
  forzada y `tenant_invitations.token_nonce`).
- `core/admin/invitations_service.py` (invitación con enlace y canje), `core/api/routers/auth.py`
  (`POST /auth/accept-invitation`), `docs/openapi.json`.
- `core/gateway/event_notices.py` (aviso `provisioning.invitation` y sus variables).
- `packages/pack-commerce`: campos `business_name` y `vertical` del checkout, `datos_alta` en el pedido
  (`crm_0011`) y la fuente de datos de alta.
- `packages/pack-subscriptions/pack_subscriptions/capability.py` (`plan_code` y `variant_id` en
  `subscription.activated`).
- `.importlinter` (las tools y los packs no importan el alta).
