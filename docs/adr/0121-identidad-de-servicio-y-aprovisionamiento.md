# ADR-0121 — Identidad de servicio y aprovisionamiento automático de negocios

- **Estado:** aceptado
- **Fecha:** 2026-10-09
- **Enmienda:** ADR-0081 § 4. Una invitación `owner` del alta automática puede llevar un enlace de un
  solo uso, que va **solo por correo** y solo cuando esa misma alta creó la cuenta sin contraseña. El
  resto de las invitaciones sigue siendo solo en la app.
- **Contexto:** HU-SH-03 (SPEC-07, decisiones D-12, D-16, D-SPEC-07-1 = A y D-SPEC-07-2 = A). Se apoya en
  ADR-0107 (outbox `domain_events`), ADR-0106 (suscripciones v1), ADR-0110 (presets), ADR-0045 (planes),
  ADR-0048 y ADR-0081 (cuentas, membresía e invitaciones), ADR-0098 (correo de sistema), ADR-0108 (avisos
  por evento) y ADR-0012 (contexto de tenant). Ajustado tras las revisiones de seguridad y de arquitectura
  del PR #345.

## Contexto

Crear un negocio solo lo puede hacer el master con sesión humana (`core/api/routers/platform.py:28-30`
y `:114-147`). `pack_subscriptions` ya emite `subscription.activated` por el outbox, pero nadie lo
escucha (`core/events/core_handlers.py:188-196` en `develop`), y el pedido no guarda vertical, negocio ni
correo (`pack_commerce/skills.py:1398-1402`). D-12, D-SPEC-07-1 = A y D-SPEC-07-2 = A piden el alta sin
humanos.

## Decisión

Un consumidor interno del core sobre `subscription.activated`, habilitado solo para los negocios de una
allowlist, da de alta el negocio del comprador con los datos del pedido, de forma idempotente, y lo firma
con una identidad de servicio que no tiene credenciales.

### 1. Identidad de servicio `provisioning`

Es el actor `provisioning` (`core/provisioning/__init__.py`, una constante): no es una fila de `admins` ni
tiene contraseña, token, sesión de panel o permisos, y ninguna ruta de la API lo acepta. Solo firma lo que
hace el consumidor en `audit_log`, en el vendedor y en el negocio nuevo. No existe un token de servicio
que un `AgentSpec`, una tool declarativa o una config de pack puedan guardar.

### 2. Quién puede vender negocios: `PROVISIONING_SELLER_TENANTS`

Lista separada por comas de **UUID** de los negocios vendedores (en la práctica, el de `solvershub`).
Solo UUID, porque el slug de un negocio borrado podría volver a usarse; una entrada que no es UUID se
ignora y queda en el log. Vacía por defecto: el alta automática queda **apagada** (fail-closed). Además
de estar en la lista, el negocio tiene que estar activo y sin borrar.

La variable es de entorno, no del panel, para que ningún admin de un negocio pueda darse permiso de
crear negocios. Va en el **worker**, que es donde corre el despachador del outbox.

### 3. Consumidor interno sobre `subscription.activated`

`core/provisioning/consumer.py` se suscribe al topic en el despachador del outbox (`ensure_core_handlers`).
Corre bajo el `tenant_scope` del evento, es decir, del vendedor:

1. **Solo vendedores.** Si el tenant del evento no está en la allowlist, no hace nada (ni lee ni escribe).
   El `tenant_id` del evento lo fija la RLS al insertarlo: un negocio no puede emitir eventos a nombre de
   otro.
2. **Registro e idempotencia.**
   - Una fila por alta en `provisioning_requests`, tabla de negocio **del vendedor** con RLS forzada.
     Las lecturas y escrituras filtran también por el vendedor, no solo por la RLS.
   - La fila es única por `(tenant_id, referencia)` y por `(tenant_id, subscription_id)`: un segundo
     evento con la misma referencia o suscripción no crea otro negocio.
   - La referencia es el número público del pedido. El emisor manda `referencia` nula cuando no hubo
     pedido (activación manual, journeys, cargo de cambio de plan), y entonces se usa
     `sub:<subscription_id>`.
   - Un evento de **cambio de plan** sobre una suscripción ya aprovisionada solo se audita
     (`aprovisionamiento_cambio_de_plan`): sincronizar el plan del negocio es de HU-SH-04.
3. **Concurrencia.**
   - Cada alta toma un candado consultivo de Postgres (`pg_try_advisory_xact_lock`) sobre su fila
     mientras trabaja. Si otro worker la tiene, el evento se reintenta más tarde.
   - El id y el slug del negocio nuevo son **deterministas**: UUIDv5 de vendedor + referencia, y slug con
     un sufijo derivado del mismo par. Así, un reintento después de una caída retoma el mismo negocio y
     nunca crea otro.
4. **Datos del alta: del pedido, no del texto.**
   - El core define el contrato `DatosDeAlta` y un registro de fuentes (`core/provisioning/fuentes.py`,
     mismo patrón que el catálogo de planes de ADR-0111).
   - `pack_commerce` lo implementa leyendo **su** pedido por referencia: el plan de la plataforma de la
     línea (atributo `precio_de_plan` de la variante, HU-SH-01), el vertical, el nombre del negocio, el
     correo y el teléfono del owner, el cliente del pedido y si está pagado (Free nace pagado, D-16).
   - Un plan va en su propio pedido: `agregar_item_pedido` no le suma productos ni lo agrega a otro.
     Antes se le podía sumar un plan pago a un Free, que ya había nacido pagado.
   - El vertical y el nombre del negocio no están en el schema de `crear_pedido`, porque la tool es la
     misma para todos los negocios con comercio. Los pide `missing_fields` según `checkout_fields.plan` del
     vendedor, y la respuesta trae las opciones del vertical.
   - El servidor valida el vertical contra la lista de presets: es un dato de un enum, no texto libre. Un
     nombre con un enlace o un dominio se rechaza (sirve para phishing). Si el negocio no pidió el dato,
     un valor que no sirve se descarta sin frenar la venta.
   - El pedido lo guarda en `pack_commerce_orders.datos_alta` (JSONB, solo pedidos de planes). El evento
     trae `variant_id` para elegir la línea correcta de un pedido con varios planes.
5. **Validación.**
   - Hace falta un plan de la plataforma conocido, un correo válido, el pedido pagado y el mismo cliente
     en el pedido y en la suscripción.
   - Si el pedido todavía no es visible (commerce y el proveedor de suscripciones escriben en
     transacciones distintas), el evento se reintenta.
   - Al tercer intento, o si falta un dato que no va a llegar, el alta queda **en revisión**: con el
     motivo, auditada y con un push a los dueños del vendedor.
   - **Un Free por cliente y por correo:** si ya hubo un alta del mismo cliente o del mismo correo (se
     guarda la huella del correo, no el correo), un Free más va a revisión. Así, Free → cancelar → Free
     no crea N negocios ni manda N correos.
   - Sin nombre de negocio válido se usa el del comprador. Un plan que no es de la plataforma deja la fila
     en `omitido`.
6. **Alta.**
   - `onboard_tenant` (idempotente, en `core/alta_negocio.py`) con el id y el slug deterministas, sin packs
     sueltos.
   - Antes se comprueba que el slug no sea de **otro** negocio: el consumidor nunca agrega un owner a un
     negocio que no creó. Si pasa, revisión.
   - Plan pago: `core/billing` (`registrar_periodo_pagado`) deja `tenant_subscriptions` en `active`, con
     el período pagado desde hoy (`interval_days` del plan), sin prueba, `provider = "asistente"` y
     referencia `plat-<slug>-<AAAAMM>`. También deja la fila del pago en `subscription_payments`.
     Es idempotente por esa referencia.
   - Free: la suscripción `free` en `trialing`, con los 30 días de prueba de siempre (D-16).
   - `tenants.settings["alta"]` guarda el origen (vendedor, referencia, plan y vertical) para el onboarding
     (HU-SH-06).
7. **Preset.**
   - Se aplica con `apply_preset(vertical, sembrar_ejemplos=False)`: un negocio real no arranca con
     productos de ejemplo que el asistente podría ofrecer a sus clientes.
   - Si el plan no alcanza el `plan_minimo` (ADR-0110 § 3.1; hoy todos los presets piden Basic o más, así
     que nunca con Free), si falta el vertical o si no existe, el negocio se crea igual. El preset queda
     **pendiente**, con su motivo en el registro y en `tenants.settings["alta"]`.
   - Un preset pendiente se aplica al subir de plan, en HU-SH-04.
8. **Owner e invitación** (D-SPEC-07-2, corregida por seguridad):
   - **Cuenta creada por esta alta:** si el correo no tiene cuenta, se crea el `Admin` **sin contraseña**
     (un hash inválido que `verify_password` rechaza: nadie puede entrar). En la misma transacción se anota
     en `detalle.owner` que esta alta la creó, así un reintento lo sabe. La invitación `owner` lleva un
     **enlace de un solo uso** que vence a los 7 días.
   - **Cuenta que ya existía,** con o sin contraseña: no se toca ni se vincula. Va la invitación de
     siempre en la app (ADR-0081), sin enlace: se entra con la contraseña (o recuperándola por correo) y se
     acepta.
   - **Correo de un master, de una cuenta con permisos globales o desactivada:** revisión **antes** de
     crear el negocio, para no dejar un negocio sin dueño.
   - **El enlace va solo por correo.** D-SPEC-07-2 pedía correo y WhatsApp. Por WhatsApp, quien compra con
     el correo de otro recibía el token, definía la contraseña y se quedaba con toda invitación que llegara
     a ese correo. Mandarlo solo al correo además **verifica el correo**: quien no lo controla no entra.
   - **El enlace:** es `<PANEL_BASE_URL>/invitacion#<token>`, con el token en el fragmento para que no
     llegue a los logs del proxy. El token es `<id de la invitación>.<HMAC-SHA256(APP_SECRET_KEY, id +
     nonce)>`. La base guarda solo el nonce (`tenant_invitations.token_nonce`), y derivar el token permite
     reenviar el mismo enlace en un reintento.
   - **Canje** (`POST /api/v1/auth/accept-invitation`, con el mismo bloqueo progresivo del login):
     - solo vale para una **cuenta limpia**: sin contraseña, sin acceso a ningún negocio y sin otras
       invitaciones pendientes. Si no, 409 y se entra por la app;
     - la contraseña se escribe con un UPDATE condicional sobre la cuenta sin contraseña, así que un reset
       que llegó antes gana;
     - la invitación se acepta con otro UPDATE condicional (uso único). Si alguno de los dos falla, no
       queda nada a medias.
   - **Aceptar en la app** (`aceptar_invitacion`) también valida el vencimiento. Aceptar, rechazar o
     revocar invalida el enlace.
   - **Por WhatsApp sale siempre el mismo aviso,** sin enlace ni token: el acceso llega al correo, que se
     muestra enmascarado (`a***@dominio`). Es igual tenga o no cuenta, sea de un master o no, y aunque el
     alta quede en revisión, así el comprador no puede averiguar nada de una cuenta. Las variables del
     evento `provisioning.invitation` (ADR-0108) no incluyen el enlace, así que una plantilla del vendedor
     tampoco puede pedirlo.
   - **Asunto del correo:** no lleva el nombre del negocio, que lo escribe el comprador.
   - Si el correo no sale (sin SMTP), el alta queda completada con ese aviso y un push a los dueños del
     vendedor.
9. **Cierre y auditoría.**
   - La fila pasa a `completado` con el negocio, el plan, el preset y el estado de la entrega.
   - `audit_log` registra `negocio_aprovisionado` en el vendedor y `negocio_creado` en el negocio nuevo,
     con actor `provisioning`, la referencia del pedido, el plan y el preset.
   - El del negocio nuevo no copia ids internos del vendedor (su `subscription_id`).

**Tiempo.** El despachador corre cada 30 s y el alta se completa en la primera entrega: de la activación al
negocio creado pasan menos de 2 minutos si no hay reintentos.

### 4. Sin el LLM

Ninguna tool expuesta al modelo crea negocios.

- `onboard_tenant` vive en su propio módulo, `core/alta_negocio.py`. `core/onboarding.py` queda con las
  utilidades de artefactos que usan los packs.
- El contrato `alta-fuera-del-llm` de import-linter prohíbe a las tools, al host MCP, al loop del LLM y a
  los packs importar el alta, el consumidor y el owner.
- `tests/test_provisioning_sin_llm.py` recorre todas las tools del core y de los packs: handlers, imports
  dentro de funciones, el símbolo `onboard_tenant` y nombres. También comprueba que la guarda detecta una
  tool con nombre inocente que intenta llegar al alta.

El asistente solo puede crear **pedidos**, con sus guardas de siempre (cotización confirmada, pago real o
Free confirmado). El negocio lo crea el core al ver la suscripción activada. No hay token que guardar.

### 5. Operación: un alta en revisión

La resuelve el master desde el servidor, con el runbook `docs/runbooks/altas-en-revision.md`:

- `agent-core tenant altas <vendedor>` lista las altas.
- `agent-core tenant reintentar-alta <vendedor> <referencia>` la vuelve a correr después de corregir el
  dato y retoma el mismo negocio.
- `agent-core tenant descartar-alta <vendedor> <referencia> --motivo …` la cierra como `omitido`.

Las tres acciones quedan en `audit_log`.

### 6. Despliegue

**No encender `PROVISIONING_SELLER_TENANTS` en producción antes de HU-SH-04 (renovación y cambio de
plan).** Hoy pasan tres cosas:

- el plan pago nace con `period_end = hoy + interval_days` y nada lo renueva;
- el paso de Free a pago desde el asistente solo se audita;
- `past_due` y `expired` bloquean en duro (`core/billing/enforcement.py:30`).

En test se puede encender para verificar.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| B · Token de servicio con scope `platform:provision` usado por una acción configurable (HU-SUB-14) | Un token poderoso guardado en la config de un negocio (D-SPEC-07-1) |
| C · Tool declarativa HTTP con el token del master | El LLM podría crear negocios y el token queda en el `AgentSpec` |
| Enlace también por WhatsApp (D-SPEC-07-2 original) | Quien compra con un correo ajeno recibe el token y toma la cuenta |
| Enlace a toda cuenta sin contraseña | Una cuenta que dejó un alta anterior la toma otro comprador, con las invitaciones pendientes |
| `business_name` y `vertical` en el schema de `crear_pedido` | Unos 65 tokens por turno en la tool de todos los negocios, que cambia con cada preset |
| Que el evento traiga todos los datos del alta | Mezcla el pedido (vertical, negocio, correo) con el contrato de suscripciones de ADR-0106; el pedido sigue siendo la fuente |
| Leer las tablas de `pack_commerce` desde el core | Rompe la separación core/packs; el registro de fuentes mantiene al core sin nombrar packs |
| Contraseña temporal por WhatsApp | Secreto en un canal de mensajería (D-SPEC-07-2) |
| Token aleatorio guardado solo como hash (como ADR-0098) | Un reintento de entrega obligaría a rotarlo y reenviar el correo |
| Sembrar los datos de ejemplo del preset | El asistente de un negocio real podría vender productos que no existen |
| Allowlist en `tenants.settings`, en el panel o por slug | Un admin con acceso a la config podría convertirse en vendedor; un slug se puede volver a usar |

## Consecuencias

**Positivas:**
- el alta de un cliente no necesita al master, y nadie guarda un token capaz de crear negocios;
- un evento repetido, un pedido con dos planes o un worker que se cae no duplican negocios;
- el enlace solo entra con el correo del owner, y el aviso al comprador no revela nada de una cuenta;
- cada alta queda en una fila del vendedor (también las que necesitan a una persona) y en `audit_log` de
  los dos lados.

**Negativas / lo que aceptamos pagar:**
- Sin correo de sistema configurado (`SYSTEM_SMTP_*`), el owner no recibe el enlace: el alta avisa a los
  dueños del vendedor y el owner entra con "olvidé mi contraseña" cuando haya SMTP.
- Si el comprador escribe mal su correo, el acceso le llega a otra persona. Esa persona no compró nada y
  no tiene a qué entrar, pero el comprador se queda sin su negocio hasta que lo corrija una persona.
  Confirmar el correo en el chat es un ajuste del playbook de ventas (`solvershub-comercial`).
- Rotar `APP_SECRET_KEY` invalida los enlaces pendientes (se recuperan con "olvidé mi contraseña").
- Un cambio de plan desde el asistente no cambia todavía el plan del negocio ni aplica su preset
  pendiente (HU-SH-04). Por eso no se enciende en producción antes (§ 6).
- Faltan los controles de HU-SH-07 (términos, interruptor, límites de tasa). Hasta entonces, la allowlist
  vacía es el interruptor.

**Qué haría que revisáramos esto:**
- un segundo vendedor con reglas distintas (pasaría a config por vendedor);
- volumen suficiente como para separar la entrega de la invitación en su propio evento;
- que el onboarding (HU-SH-06) necesite el enlace en otro canal.

## Impacto en el código

- `core/provisioning/`: identidad, allowlist, contrato `DatosDeAlta` con su registro de fuentes,
  consumidor (con reintento y descarte) y owner. `core/events/core_handlers.py`: suscripción al topic.
- `core/alta_negocio.py`: `onboard_tenant` sale de `core/onboarding.py`; se actualizan sus llamadores
  (panel, API de plataforma, CLI y tests).
- `core/billing/service.py`: `registrar_periodo_pagado`.
- `core/shared/texto_seguro.py`: enlaces en nombres y correo enmascarado. `core/gateway/system_email.py`
  ya no deja el correo entero en el log.
- `core/config.py` (`PROVISIONING_SELLER_TENANTS`) y `docs/despliegue-coolify.md`.
- `core/db/models.py` y `alembic/versions/0059_provisioning_requests.py`: `provisioning_requests` con RLS
  forzada y `tenant_invitations.token_nonce`.
- `core/admin/invitations_service.py`: invitación con enlace, canje de cuenta limpia y vencimiento al
  aceptar. `core/api/routers/auth.py`: `POST /auth/accept-invitation` con bloqueo progresivo.
  `docs/openapi.json`.
- `core/gateway/event_notices.py`: aviso `provisioning.invitation`, sin enlace.
- `packages/pack-commerce`:
  - `datos_alta` en el pedido (`crm_0011`) y la fuente de datos del alta;
  - `business_name` y `vertical` por `missing_fields`, con validación y sin enlaces;
  - un plan en su propio pedido.
- `packages/pack-subscriptions`: `variant_id` en `subscription.activated` y `referencia` nula sin pedido.
- `cli/commands/tenant.py` (`altas`, `reintentar-alta`, `descartar-alta`) y `docs/runbooks/altas-en-revision.md`.
- `.importlinter`: las tools y los packs no importan el alta.
