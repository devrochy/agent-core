# ADR-0152 — Messenger e Instagram con salida: se rescatan portando el código viejo sobre la conexión canónica

- **Estado:** aceptado el 2026-10-23 por el dueño al mergear #520 (HU-CHAN-01 Messenger y HU-CHAN-04 Instagram, SPEC-17 en `solvershub-docs`)
- **Fecha:** 2026-10-23
- **Contexto:** los PRs #45 (Messenger) y #46 (Instagram) figuran como mergeados, pero estaban apilados sobre otras
  ramas (`feat/messenger-connection`, `feat/instagram-connection`) y se cerraron segundos después de #44: su código
  nunca llegó a `develop`. El 2026-10-23 el dueño decidió rescatarlo, con los ajustes que pida el código de hoy.
- **Enmienda / usa:** ADR-0094 (credenciales por conexión), ADR-0095 (propiedad única de los activos), ADR-0100 y
  ADR-0126 (firma por grupo), ADR-0115 (conexión canónica), ADR-0122 (ciclo de vida y pausa), ADR-0076/0077 (panel
  nuevo y API `/api/v1`). Los ADR-0031 y ADR-0032 de aquellas ramas **no se rescatan con esos números**: hoy son
  otros temas.

## Contexto

### Qué hay hoy en `develop` (verificado el 2026-10-23)

- **Entrada ya existe.** `webhook_flatten.py` y `webhooks.py` aplanan y procesan los objetos `page` e `instagram`;
  `WhatsAppMetaInbound` los parsea (`messenger`, `instagram`); `webhook_route_tenant.py` resuelve el negocio con
  `resolve_tenant_for_page` y `resolve_tenant_for_ig_user`.
- **Activos descubiertos ya existen.** Las tablas `fb_pages` y `ig_accounts` cuelgan de `meta_connections`.
- **Los tipos de conexión existen pero apagados.** `messenger_meta` (`external_id_key = page_id`) e `instagram_meta`
  (`account_id`) están con `available = False` en `connection_types.py`.
- **El plan ya promete Instagram.** `core/billing/plans.py` lista `instagram` en `features.canales` de Premium y
  Empresarial; Messenger no aparece.
- **Falta la salida y el alta.** No hay adaptador de salida (el registro está indexado solo por proveedor y `meta` es
  WhatsApp), ni alta con Facebook Login / Instagram Login, ni token de Página, ni ventana de servicio por canal.

### Qué cambió desde que se escribió ese código (2026-09-19)

1. **Conexión canónica y conversación por conexión** (ADR-0115): la conversación lleva `connection_id`. El diseño
   viejo agregaba un puente propio (`conversations.fb_page_id`, `conversations.ig_account_id`).
2. **Credenciales por conexión** (ADR-0094) y **firma por grupo** (ADR-0100, ADR-0126): un evento solo actúa sobre
   los activos de la conexión que lo firma.
3. **Propiedad única de los activos** (ADR-0095). Hoy `resolve_tenant_for_page` e `…_for_ig_user` hacen
   `SELECT tenant_id … LIMIT 1` sin orden sobre tablas cuya única unicidad es `(page_id, connection_id)`: dos
   negocios pueden guardar la misma Página y el mensaje iría a uno cualquiera. Es el mismo defecto que ADR-0095
   corrigió para WABA y número.
4. **Envío por cola** (`outbound_send_task`), pausa y borrado de conexión (ADR-0122), y pausa por uso de la API.
5. **RLS habilitada y forzada** en toda tabla de negocio (migración `0029`) y migraciones hasta la `0103`
   (la `0026`–`0028` de la rama vieja ya están ocupadas).
6. **Panel nuevo en Next.js.** Las plantillas Jinja del diseño viejo (`meta_paginas.html`, `meta_wizard_*.html`,
   `facebook_login_*.js`) están en retiro (ADR-0076): la UI vive en `solvershub-panel` y consume `/api/v1`.

Una fusión de prueba el 2026-10-23 reaplicando los 11 commits (5 de Messenger y 6 de Instagram) sobre `develop` dio
conflictos en 9 de 11, todos en el camino caliente: `registry.py`, `sender.py`, `webhooks.py`, `consumer.py`,
`inbound.py`, `views.py`, `models.py`, `config.py`, `celery_app.py`.

## Decisión

1. **Portar, no mergear.** Se reaplica la lógica de dominio (descubrimiento de Páginas, suscripción con verificación
   inmediata, envío, ventana, refresco de token de Instagram, prueba de humo) sobre el código de hoy, en dos
   entregas: **Messenger** (HU-CHAN-01) y después **Instagram** (HU-CHAN-04), que reutiliza el token y la
   infraestructura de Páginas. Las ramas viejas se conservan como referencia hasta cerrar ambas y se archivan con tag.
2. **Una conexión canónica por Página o cuenta.** Se activan los tipos `messenger_meta` e `instagram_meta`; la
   conversación usa su `connection_id` (ADR-0115) y **no se agregan** `fb_page_id` ni `ig_account_id` a
   `conversations`. La Página o la cuenta de la conversación sale de la conexión. `fb_pages` e `ig_accounts` siguen
   siendo los activos descubiertos.
3. **Propiedad única, antes de habilitar la salida.** Un `page_id` y un `ig_user_id` tienen un solo dueño vivo: el
   resolver devuelve conflicto si hay dos (como ADR-0095 para número y WABA), el alta verifica contra Graph que la
   Página pertenece al token y rechaza un activo de otro negocio, y la migración informa los activos con dos dueños
   en vez de elegir uno.
4. **Registro de adaptadores por `(proveedor, canal)`.** Hoy `meta` es WhatsApp; Messenger e Instagram comparten el
   proveedor `meta` y se distinguen por canal. El cambio mantiene los adaptadores de entry points
   (`agent_core.channels`, ADR-0052) y no cambia a WhatsApp.
5. **Credenciales en la custodia de la conexión.** El token de Página (y, en Instagram, el de Instagram Login con su
   refresco) va a `meta_private.meta_tokens` con un `kind` nuevo y el AAD tenant + conexión + kind (ADR-0094). Instagram
   Login usa credenciales de app propias y un `state` firmado que **no cae a un secreto compartido o débil** si falta
   la configuración (se conserva esa decisión de seguridad del diseño viejo, que el auditor había exigido).
6. **Webhook.** Los grupos `page` e `instagram` se autorizan con la conexión dueña de sus activos (ADR-0126). Un
   grupo de otra conexión del mismo negocio se rechaza sin alerta y uno de otro negocio, con alerta.
7. **Envío.** Sale por `outbound_send_task`, con pausa y borrado de conexión (ADR-0122). La ventana de servicio se
   valida **antes de encolar** y nunca llama a Meta si ya se sabe que va a fallar. Messenger e Instagram no tienen
   plantillas: fuera de ventana el mensaje queda `failed` con su motivo y la conversación se ofrece a un humano. Para
   Messenger, la etiqueta `HUMAN_AGENT` solo con una acción humana confirmada. **Las reglas de ventana y etiquetas se
   re-verifican contra la documentación primaria de Meta al implementar:** los datos del diseño viejo son del
   2026-09-19 y Meta los cambia (la skill `meta-whatsapp` los fecha y cita la fuente).
8. **Plan.** Se usa `features.canales` de `plans.py`: un negocio sin el canal en su plan ve por qué no puede
   conectarlo. **Pendiente de confirmar por el dueño:** hoy Instagram está en Premium y Empresarial y Messenger no
   aparece; la propuesta es incluir Messenger donde esté Instagram.
9. **UI en el panel nuevo.** El alta (`FB.login` con `config_id`, descubrimiento, suscripción y verificación) se
   expone como API `/api/v1` con el contrato en `docs/openapi.json` y se construye en `solvershub-panel`, dentro de
   la ficha de la conexión (SPEC-12). No se porta ninguna plantilla Jinja.
10. **Migraciones nuevas, con la numeración de hoy.** Cada tabla o columna nueva lleva `tenant_id`, RLS habilitada y
    forzada, política de aislamiento e índice compuesto que empieza por `tenant_id`; sin borrado físico (`deleted_at`).
11. **Producción depende de Meta.** Los permisos de mensajería de Páginas y de Instagram exigen App Review y acceso de
    Tech Provider (trámite C del dueño). Mientras tanto se prueba con roles de desarrollo de la app en test.

## Adenda del 2026-10-23: el alta de Messenger (entrega 3)

Decisiones tomadas al construir `core/gateway/meta_messenger_alta.py` y `core/api/routers/meta_messenger.py`:

1. **El negocio elige las Páginas.** El código viejo conectaba todas las que el usuario administraba. Cada Página es
   una conexión canónica y consume plan, así que el alta es en tres llamadas: `attempt` (un valor de un solo uso,
   atado al negocio y al admin, de 15 min), `discover` (canjea el `code` y lista las Páginas con su elegibilidad;
   **los tokens de las Páginas nunca salen del servidor**) y `connect` (las elegidas).
2. **El token del usuario es de una hora y se revoca al terminar.** Se guarda cifrado (`user_access`) solo porque el
   paso 3 lo necesita; el de cada Página queda en la custodia de la conexión (`page_access`, ADR-0094).
3. **Una Página que el usuario no administra no se conecta aunque conozca su ID**: `connect` vuelve a pedir
   `/me/accounts` y solo conecta lo que aparece ahí.
4. **Roles.** `subscribed_apps` pide `CREATE_CONTENT`, `MANAGE` o `MODERATE`; la Messenger Platform pide `MESSAGING`.
   Las dos páginas de Meta discrepan (2026-10-23): se exigen `MESSAGING` **y** uno de los otros tres. Un administrador
   de la Página los tiene todos. **A validar con una Página real** en test.
5. **Configuración propia: `META_MESSENGER_CONFIG_ID`.** La Login Configuration de Embedded Signup de WhatsApp
   (`META_CONFIG_ID`) no concede `pages_messaging` ni `pages_manage_metadata`; Messenger necesita la suya. Sin ella el
   alta no está disponible (no cae a `META_CONFIG_ID`: abriría el registro de WhatsApp); hay que crearla en Meta.
6. **Plan (decisión del dueño, 2026-10-23).** Messenger **e Instagram** quedan en todos los planes de pago (Basic,
   Premium y Empresarial); Free solo WhatsApp. Se aplica con `features.canales` de `plans.py` y el alta responde
   `409 plan_channel_not_included`. Basic hoy prometía solo WhatsApp: cambia lo que promete ese plan, a reflejar en
   `modelo-de-precios.md` y en la landing.
7. **Atomicidad.** La cuenta de Meta nace en `draft` y sale de ahí en la misma transacción que escribe la primera
   Página; la Página, su conexión canónica y sus instancias de agentes se escriben juntas, después del reclamo con
   candado por activo. Si Meta no confirma la suscripción, la Página queda guardada con `subscribed = false` y la
   cuenta en `pending`: no se da por conectada una cuenta sin ninguna Página suscrita.
8. **El tipo `messenger_meta` sigue `available = false`** hasta que exista la UI en el panel (siguiente entrega): sin
   ella, el listado ofrecería un botón que no lleva a ningún lado.

## Alternativas descartadas

| Alternativa | Por qué no |
|---|---|
| Mergear las ramas viejas | 9 de 11 commits conflictúan en el camino caliente; traen migraciones (`0026`–`0028`) y ADR (`0031`, `0032`) con números ya usados, un puente por conversación que contradice ADR-0115 y UI en Jinja en retiro |
| Reescribir desde cero | Pierde lo ya revisado: dos pasadas del `auditor-multitenant` (hallazgos de enrutamiento entre Páginas, CSRF de Instagram y diagnóstico por tenant) y la cobertura de tests de cada commit |
| Un solo PR con Messenger e Instagram | ~45 archivos y 4.000 líneas: difícil de revisar y de revertir; Instagram depende del token y las Páginas de Messenger |
| Dejarlos sin salida | El plan Premium ya promete Instagram y hay negocios cuyo cliente llega por ahí |

## Consecuencias

- **Positivas:** el plan deja de prometer un canal sin salida; el defecto de propiedad de Páginas y cuentas (mismo
  tipo que ADR-0095) se corrige antes de abrir la salida; no se agrega estado nuevo a `conversations`.
- **Negativas:** es una portación, no un merge: cuesta más que cherry-pick; la verificación contra Meta real queda
  sujeta a App Review; el panel nuevo exige trabajo en otro repo.
- **Qué haría que revisáramos esto:** que Meta cambie las reglas de ventana o de etiquetas de mensajería, o que la
  propiedad única de Páginas rompa un caso real de agencias que administran la misma Página para dos negocios.

## Verificación

- Tests de aislamiento entre negocios para Páginas y cuentas de Instagram (dos negocios, mismo `page_id`).
- Una conexión con dos Páginas no cruza respuestas.
- Un envío fuera de ventana falla en validación local y no llama a Meta.
- `auditor-multitenant` antes de cerrar cada entrega; `ruff` y `pytest` en verde.
- Alta de punta a punta en test con una Página y una cuenta de Instagram de prueba.

## Impacto en el código (propuesto)

- `core/gateway/channels/registry.py`, `channels/messenger/`, `channels/instagram/` (adaptadores de salida).
- `core/gateway/meta_messenger.py`, `meta_instagram.py` (dominio) y `meta_token_store.py` (kinds nuevos).
- `core/gateway/webhook_route_tenant.py` (propiedad única) y `connection_types.py` (`available`).
- `core/api/` (alta de Messenger e Instagram) con `docs/openapi.json` regenerado.
- `core/tasks/` (refresco del token de Instagram) y migraciones nuevas.
