# Changelog

## v0.4.0 — SolversHub productivo (2026-11-19)

### Operador

- Producción pasa a una base nueva y vacía, `agent_core_prod`, en la misma instancia. La base vieja (`agent_core`) queda intacta como vuelta atrás.
- Antes del despliegue, `DATABASE_URL` y `MIGRATIONS_DATABASE_URL` de API, worker y beat deben apuntar a `agent_core_prod`; el dueño es `agent_core_migrations` y las consultas corren como `app_runtime`.
- Desplegar primero la API: su entrypoint aplica migraciones y `sync-packs`. Cuando esté `healthy`, desplegar worker y beat con el mismo commit.
- Crear el primer master con `agent-core admin create --email <correo> --role master` y el negocio desde el panel.
- Para el chat web: activarlo en Conexiones → Chat web con `https://www.solvershub.site` como origen permitido y publicar `NEXT_PUBLIC_WEBCHAT_KEY` en la landing.

### Correos con diseño HTML de marca (2026-11-19)

- Los correos salen ahora como `multipart/alternative`: el texto plano de siempre queda como
  respaldo y se agrega un HTML con la identidad de SolversHub (tokens de `solvershub-panel/brand`:
  navy, azul de acción, Montserrat/Inter). Sin cambios de esquema ni de contratos.
- Plantillas nuevas en `core/gateway/email_templates/`: MJML en `src/` y HTML compilado en
  `html/` (se compila con `scripts/build_email_templates.sh`; el runtime no necesita Node).
- Diseño propio por correo: código de recuperación (`password_reset`), acceso/invitación del
  dueño (`access_invite`), aviso de renovación (`renewal`), informe de pruebas (`qa_report`) y
  el correo libre del asistente (`generic`), que conserva el mismo armazón.
- `send_system_email` y el adaptador SMTP aceptan `html` opcional; si la plantilla fallara, el
  correo sale en texto plano (el diseño nunca tumba un envío).
- El isotipo de SolversHub encabeza los cinco correos (PNG público del sitio de marca, porque
  Outlook/Gmail no renderizan SVG); el wordmark de texto se mantiene al lado.
- El correo de confirmación de cita sigue enviando el enlace de Google Calendar (`{link}`,
  HU-CAL-05), pero el render lo muestra como botón "Agregar a mi calendario" en vez de una URL
  larga en el texto.
- Todo lo que viene de fuera (nombre del negocio, asunto y cuerpo del asistente) se escapa con
  autoescape de Jinja2; los tests cubren el escape y la ausencia de marcas técnicas.

### Mínimo de Wompi del cobro de plataforma según el ambiente (2026-11-18)

- `core/billing/providers/wompi.py` ya no fija 150.000 COP: usa 150.000 en sandbox y 1.500 en producción, igual que el pago en línea. Un plan de producción bajo 150.000 ya no se rechaza antes de llegar a Wompi.
- El mínimo por ambiente pasa a `core/payments/wompi_minimo.py`; `pack_payments` lo importa de ahí (mismos nombres) en vez de tener su copia.

### Segundo comprobante: la acción está en el paso «resultado» (2026-11-18, HU-COM-24)

- `comprobar_transferencia` 1.2.0: el paso «resultado» permite `comprobar_comprobante`. Con 1.1.0 las instrucciones pedían revisar la foto nueva pero el paso no tenía la acción, así que el agente no podía (visto en test). `pack_commerce` 4.2.0; gate regrabado.

### Runbook de base nueva: esperado con la 0148 (2026-11-18)

- `docs/runbooks/arranque-base-nueva.md`: en 7.2 el esperado pasa a `0` y en 7.3 a todo `[OK]` con código 0, ahora que la `0148` (agent-core#845) fuerza RLS en `meta_private.meta_tokens`. El registro del ensayo conserva lo que salió antes de esa migración.

### Runbook: arrancar producción sobre una base nueva y vacía (2026-11-18)

- `docs/runbooks/arranque-base-nueva.md`: base nueva al lado de la vieja en la misma instancia, roles idempotentes (ya existen en el clúster), `GRANT SET ON PARAMETER` de la `0072`, extensiones por base, marca propia (no la de `vaciar_base_nueva.sql`), migración y `sync-packs` desde el arranque de la API, master único, purga de `tenant-1` de la `0003` y verificaciones. Ensayado en Postgres efímero con develop `e085a923`.

### RLS forzada en `meta_private.meta_tokens` (2026-11-18)

- Migración `0148`: `FORCE ROW LEVEL SECURITY` en `meta_private.meta_tokens`. La `0018` solo la habilitó y la `0029` forzó únicamente `public`, así que el rol dueño leía los tokens sin la policy (invariante 1). Con esto `scripts/verificar_aislamiento.py` pasa `rls_otros_esquemas` y se cierra el pendiente de la fase 0 de `docs/runbooks/produccion-vps.md`. Nada cambia para `app_runtime`.
- Test de integración (`-m aislamiento`): ninguna tabla fuera de `public` queda con RLS sin FORCE.

### Suscripciones con LLM real: cambio de plan, pagar el período y journeys al día (2026-11-18, HU-ARCH-28)

La primera noche de evals con LLM real (run 44357972704) dejó los journeys de suscripciones en rojo (pass^4 58 %).

- `pack_subscriptions` 0.8.0:
  - Resumen propio de `sub_aplicar_cambio_plan` con la cotización vigente: «Cambio de plan: del Plan Basic al Plan
    Premium», lo que paga hoy, el precio nuevo y cuándo aplica. Antes salían los argumentos crudos («Plan:
    PLAN-PREMIUM») y el monto con el rótulo de una inscripción («Total del primer cargo»).
  - Playbook `suscripciones`: seguía mandando a `sub_cambiar_plan`, que no se ofrece desde HU-AOS-69 (motor siempre
    encendido); el asistente decía que no podía cambiar el plan y escalaba. Ahora describe el procedimiento «Cambiar
    de plan» y que pagar el período de una suscripción es `sub_link_pago`, no «Contratar un plan».
  - `contratar_plan` 2.2.0: el disparador aclara que no es para pagar el período ni para cambiar de plan, y la etapa
    `elegir_plan` permite `sub_link_pago`: el enrutador léxico manda a este recorrido «pasame el link» de un
    suscriptor (el golden de ruteo lo fija así), y la acción moría con «no corresponde a la etapa actual». El
    asistente manda el link y cierra el recorrido con `cancelar_tarea` (mismo patrón que HU-COM-24).
  - `sub_consultar` trae los cargos recientes de la suscripción (`list_for_customer` devolvía siempre `cargos: []`).
- Journeys: 08 y 09 esperan `sub_aplicar_cambio_plan` (no `sub_cambiar_plan`) e insisten con «Sí, confirmo el Plan
  …» («cambio» es negación en el léxico de confirmación); en 12 el paso de los datos ya no exige
  `sub_confirmar_inscripcion`, que solo corre después del «Sí, confirmo» del paso siguiente.
- `pack_commerce` 4.3.0, `pago_en_linea` 1.1.0: el disparador dice que no es para la mensualidad de una
  suscripción y la etapa `elegir_pedido` permite `sub_link_pago` y cierra el recorrido (`cancelar_tarea`) si el
  cliente habla del cobro de su suscripción. En la segunda noche, «pasame el link» abría este recorrido y le pedía un número
  de pedido a un suscriptor.

### Bienvenida de 5 pasos en el servidor (2026-11-18, HU-AOS-84)

- El recorrido de primer uso pasa a 5 pasos (`TOTAL_PASOS = 5`, SPEC-49): Tu negocio y tipo de agente · Habilidades y formas de pago · Datos del cliente · Datos para probar · Revisar, probar y publicar.
- Lo guardado con 4 pasos se lee con `PASO_DE_4_A_5` (1→1, 2→2, 3→4, 4→5); lo de 9 y lo de 6 (la APK, sin `pasos`) pasan por las tablas de siempre y luego por esa. El `{paso: 5, pasos: 4}` que escribía `dejar_listo` se lee como el paso 2 (Habilidades).
- `POST /admin/onboarding/avanzar` acepta `pasos` 4, 5 o 9, o ninguno; otro total responde 422 y un paso mayor que el total con 4 o 5, 409 `transicion_invalida`. El esquema de OpenAPI sigue siendo `integer` (solo cambia la descripción).
- `dejar_listo` deja el recorrido en el paso 2 en vez del 5 y no retrocede uno que ya va más adelante. `finalizar` queda en el paso 5.

### Guard de arranque de APP_SECRET_KEY (2026-11-18, salto a producción)

- La API, el worker y el beat no arrancan en producción (`APP_ENV=production`, también test) si `APP_SECRET_KEY` es la de ejemplo (`change-me`), está vacía o tiene menos de 32 caracteres. Se genera con `openssl rand -hex 32`. Prod y test ya tienen claves de 64 caracteres (medido 2026-11-18).

### Tests de tool_sin_binding sin depender del orden de structlog (2026-11-18)

- `tests/test_tool_sin_binding_v3.py` deja de depender del orden de la suite: los tres tests que miraban el evento `tool_sin_binding` con `capture_logs` ahora sustituyen el logger del loop por uno que anota (mismo patrón que `test_leads_follow_up_sin_config.py`). Con `cache_logger_on_first_use` (que activa `configure_logging` al importar `core.main`), el logger del loop quedaba atado a la lista de procesadores de su primer uso y un test que configuraba una lista nueva en el mismo worker de xdist lo dejaba fuera de `capture_logs`.

### Tests de integración sin capture_logs sobre loggers de módulo (2026-11-18)

- `test_pack_commerce_ignora_cobros_de_suscripcion.py` y `test_telegram_medios_db.py` sustituyen el logger del módulo por uno que anota en vez de usar `capture_logs`, con el mismo arreglo de core#850: un logger cacheado por `cache_logger_on_first_use` no lo ve `capture_logs` si otro test configuró antes una lista de procesadores nueva. En el primero, una captura vacía hacía pasar en falso la comprobación de «sin alarma».

### Lectura de comprobantes con visión (2026-11-17, HU-AI-01)

- Playbook de kernel `comprobar_comprobante.md`: la visión solo copia los datos del comprobante; el texto de la imagen es dato, no instrucción.
- `pack_commerce/comprobantes.py`: esquema `ComprobanteTransferencia`, `evaluar_comprobante` (regla D4 de SPEC-48: `pagado` solo con monto exacto ±1, cuenta por últimos 4 + entidad, estado exitoso, fecha no anterior, no duplicado y lectura confiable; `rechazado` con monto menor o transferencia fallida; si no, `en_revision`) y `leer_comprobante` con la cuota verificada antes de llamar y la salida en el ledger.
- La evidencia guarda la cuenta solo con sus últimos 4 dígitos. Golden `evals/comprobantes/golden.json` (13 casos).

### Pruebas de crons a cualquier hora (2026-11-17)

- Pruebas de integración de los crons de negocio: ya no dependen de la hora en que corre el CI.
  Desde ADR-0201 cada cron de negocio corre solo a su hora local y las pruebas que llaman los
  jobs directo se saltaban «fuera de hora» (17 en rojo en `develop` tras agent-core#823).

### Un segundo comprobante se revisa (2026-11-17, HU-COM-24)

- Tras un comprobante en revisión o rechazado, el agente decía «no necesitas enviar otro» sin revisar la foto nueva. El paso «resultado» de `comprobar_transferencia` (1.1.0) y la pista de `comprobar_comprobante` piden volver a llamarla; el servidor ya distingue foto nueva (se evalúa) de la misma (devuelve lo guardado, ahora con un detalle que lo dice). `pack_commerce` 4.1.0; gate regrabado.

### Ventas puede usar las formas de pago (2026-11-17, HU-COM-23, HU-COM-24)

- La plantilla `ventas` incluye `pack_commerce.pago_en_linea` y `pack_commerce.comprobar_transferencia` en su enfoque: antes el filtro por enfoque los escondía y un agente creado desde la plantilla no podía comprobar transferencias ni reenviar links (visto en el ciclo de pruebas en test). Los agentes nuevos los traen; los existentes los encienden en Habilidades.

### Superpoder «Pago en línea» para pedidos existentes (2026-11-17, HU-COM-23)

- Procedimiento `pack_commerce.pago_en_linea` (dominio «Formas de pago»): `elegir_pedido` → `enviar_link` → `esperar_pago` → `cerrar`. Disparadores de pedidos que ya existen («ya pagué», «no me llegó el link», «el link venció», «estado de mi pago»); nunca «quiero pagar». Reusa `consultar_estado_pedido`, `confirmar_pago_pedido` y `verificar_pago_pedido`; afirmaciones de link y de pago confirmado con dato del servidor.
- `consultar_estado_pedido` devuelve `order_id` (el mismo de Referencias vigentes) y el estado de pago en palabras (`en revisión`, `pago rechazado`).
- No declara `requires: pack_payments` (no trae acciones; el linter lo rechaza): sin proveedor la forma queda «Falta configurar» (HU-COM-21) y `confirmar_pago_pedido` responde que no está disponible.
- Golden de ruteo: catálogo `tienda_pagos` (12 casos) sin solapamiento con `tomar_pedido`; con `tomar_pedido` en foco, «ya pagué» no abre otra tarea.

### Leads en el core, parte 2: el core es el dueño del lead (ADR-0193, HU-AOS-70) (2026-11-17)

- `crear_lead`, `calificar_lead` y `actualizar_lead` son tools del core y llegan solo a los agentes con Captar leads (binding). Ventas, Agenda y Suscripciones nacen con Captar leads; Cobranza no. Un negocio sin comercio ya puede captar leads.
- El lead del cliente se asegura en cada conversación con un agente que tiene Captar leads (reemplaza `auto_create_lead`), con su origen según el canal.
- Clientes: las columnas, filtros, edición y la historia del lead salen del provider del core «Lead»; `PATCH /admin/leads/{id}/pack/pack_commerce` sigue funcionando como alias. Nuevo `POST /api/v1/admin/leads/{id}/seguimiento/hecho` (la ruta de commerce queda como alias).
- Los crons de reenganche y recordatorios de seguimiento pasan al core (`core.leads_follow_up`, `core.leads_recordar_seguimientos`) y solo corren en negocios con la config de Captar leads; la migración 0147 conserva la política guardada por negocio.
- Commerce 4.0.0: sin `LeadSkill`, sin crons de leads y sin modelos de leads (sus tablas quedan, sin escrituras).

### Horario por agente (2026-11-17, HU-AOS-82)

- Migración `0145`: `agent_drafts.schedule`, `agent_versions.schedule` y `schedule_hash` (nullable: sin backfill, todos heredan el del negocio). La unicidad de versiones pasa a un índice único `(tenant_id, agent_id, content_hash, flow_hash, schedule_hash)`: publicar un cambio solo de horario o solo de flujo ya no choca. La reversa no bloquea: pierde el horario propio (todos heredan el del negocio) y restaura la unicidad vieja solo si no hay versiones repetidas por contenido.
- `core/agents/horario_agente.py`: validación (zona, días, HH:MM), huella y fallback al horario del negocio (uno guardado inválido nunca falla el turno).
- `GET/PUT /api/v1/admin/agents/{agent_id}/schedule` (con `lock_version`; `null` vuelve a heredar). Se congela al publicar y viaja en la release (`components[].schedule`).
- Ingreso (fuera de horario) y bloque del prompt (horario informativo) usan el horario del agente que atiende (dueño vigente o el de la conexión); el modo y el aviso siguen siendo del negocio. Sin horario propio el prompt no cambia. Los crons no lo miran.

### Formas de pago por agente (2026-11-17, HU-COM-21)

- `CRMConfig.payment_forms` (contra entrega, transferencia, pago en línea, con interruptor e instrucciones), `transfer_proof_required` y `transfer_ocr_enabled`; los cuatro y `transfer_accounts` son de cada agente (`config_agente`). Sin configurar se deduce como antes.
- Nuevo `pack_commerce/formas_de_pago.py`: estado de cada forma («Activa», «Falta configurar», «Apagada») y métodos que el agente puede ofrecer. Transferencia sin cuenta o pago en línea sin proveedor no se ofrecen.
- `GET/PUT /api/v1/pack/commerce/agents/{agent_id}/payment-forms`. El resolver `metodo_de_pago` y `crear_pedido` solo aceptan formas activas; el bloque de cuentas usa las del agente.
- Referencia del turno `formas_de_pago` (solo ids y si/no, nunca cuentas) solo cuando el agente guardó sus formas: sin configurar, el prompt queda como antes y las grabaciones de los journeys siguen valiendo. `playbooks/ventas.md` no cambia.
- Las cuentas guardadas se sanean al cargar sin tumbar la config; la validación estricta es la del PUT.

### Escenarios de pago en línea y transferencia (2026-11-17, HU-COM-23, HU-COM-24)

- Escenarios 06–09 del set `procedimientos` (grabados con LLM real) en el gate en replay del CI: reenviar el link, estado del pago en línea, «ya transferí» y «¿les llegó mi transferencia?».
- Verificador: «link de pago» solo cuenta como afirmación si anuncia o presenta un link; nombrar la forma de pago ya no escala la venta.
- `verificar_pago_pedido`: la pista exige `confirmar_pago_pedido` para reenviar el link (el modelo decía «te lo reenvío» sin enviarlo).
- Salidas estables para el replay: `existing_data` de `crear_pedido` y `updated` de `update_customer_profile` van ordenados.

### Superpoderes agrupados por dominio (2026-11-17, HU-AOS-83)

- `BaseModule.grupos_de_procedimientos` y `etiquetas_de_grupos_de_procedimientos`; el editor expone `Superpower.group` y `group_label` (un procedimiento sin grupo cae en `otros`).
- Commerce: `tomar_pedido` en «Ventas»; `pago_en_linea` y `comprobar_transferencia` en «Formas de pago». Agenda, Cobranza y Suscripciones con su dominio.
- ADR-0203. El contrato `procedure/1` no cambia.

### El cron y su política miran el mismo reloj (2026-11-17)

- Los jobs de suscripciones (`expirar_inscripciones`, `generar_cargos`, `marcar_vencidas`,
  `avisos_de_cobro`) y el de recordatorios de citas le pasan su `now` a `debe_correr(..., ahora=now)`:
  antes la política de ADR-0201 leía el reloj real y el job el suyo. En producción no cambia nada
  (los dos son «ahora»). La fixture de integración ignora «fuera de hora» siempre (apagado y ventana
  siguen contando) y la regla de la hora queda probada en `tests/test_crons_negocio.py`.

### Superpoder «Comprobar transferencia» (2026-11-17, HU-COM-24)

- Procedimiento `pack_commerce.comprobar_transferencia` (dominio «Formas de pago»): `elegir_pedido` → `pedir_comprobante` → `analizar_comprobante` → `resultado`; afirmaciones de monto, pago confirmado y «en revisión» con dato del servidor.
- Acción `comprobar_comprobante(order_id, nota)`: pedido del cliente y por transferencia, última imagen entrante de la conversación posterior al pedido, idempotente por `image_sha256 + order_id` (un reintento no vuelve a llamar a la visión), mismo archivo en otro pedido = duplicado (revisión), visión con cuota previa y regla D4, evidencia en `pack_commerce_payment_proofs` y `payment_status`; `pagado` encola `order.paid`. Devuelve `pagado`, `en_revision`, `rechazado` o un error de negocio (`sin_comprobante`, `sin_cuota`… con `escalar`). Al modelo no le llega lo que leyó la visión.
- Simulador del playground: queda en revisión sin leer imagen ni cobrar.
- La confirmación manual del pago (HU-COM-22) también encola `order.paid`.

### Comprobantes de pago y revisión manual (2026-11-17, HU-COM-22)

- Migración `crm_0020`: `pack_commerce_payment_proofs` (RLS forzada, índices `(tenant_id, order_id, created_at)` y `(tenant_id, image_sha256)`, único parcial `(tenant_id, order_id, image_sha256)` para la idempotencia) y tres columnas nullable en `pack_commerce_orders` (`payment_status_changed_by/_at/_reason`). Reversa solo sin comprobantes.
- `payment_status` acepta `en_revision` (filtro `pago=en_revision`).
- `GET /api/v1/pack/commerce/orders/{id}/payment-proofs` y `POST /api/v1/pack/commerce/orders/{id}/payment-status` (`confirmar`, `rechazar` con motivo, `marcar_pagado`); queda quién y cuándo. Un pedido pagado no se rechaza desde aquí.

### Comprobantes: la hora se compara en la zona del negocio (2026-11-17, HU-AI-01, HU-COM-24)

- `comprobar_comprobante` pasa la hora del pedido a la zona del negocio antes de compararla con la del comprobante, que el banco imprime en hora local y sin zona. Con UTC, un pago hecho minutos después del pedido parecía cinco horas anterior y quedaba siempre «en revisión» (visto en el ciclo de pruebas en test).

### Bienvenida de 4 pasos (2026-11-17, HU-AOS-81)

- El recorrido de primer uso pasa a 4 pasos (`TOTAL_PASOS = 4`): Tu negocio y tipo de agente · Habilidades y formas de pago · Datos para probar · Revisar, probar y publicar.
- Lo guardado con 9 pasos (SPEC-47) o con 6 (sin `pasos`) se traduce al leerlo; `POST /admin/onboarding/avanzar` acepta `pasos: 4`, `pasos: 9` (un panel en caché) o ninguno (la APK, numeración de 6).

### Memoria fail-closed en todo turno v3 sin agente en foco (2026-11-16)

- `write_namespaces_del_turno()` devuelve `frozenset()` (ninguna escritura) siempre que el turno `exige_concesion()` y no hay agente en foco del negocio actual: release fijada sin foco, negocio v3 sin release o ranura de otro negocio. Antes, los dos últimos casos daban `None` y las tools de memoria trataban el turno como legacy, sin chequear `writeNamespaces`.
- Legacy (ranura sin marcar, bandera apagada o conexión interna del Centro de pruebas) sigue en `None`. El Coordinador y el turno de solo lectura no cambian.
- Hallazgo del auditor multitenant sobre HU-ARCH-35. `update_customer_profile` queda negado de inmediato; `remember_fact` lo queda cuando su chequeo de HU-ARCH-35 (core#815) esté en develop.

### Ventas respeta los `writeNamespaces` del agente al escribir el perfil (ADR-0114 §3, ADR-0179 §10) (2026-11-16)

- `crear_lead`, `actualizar_lead` y `crear_pedido` (pack_commerce) escriben el perfil del cliente por `datos_cliente.guardar()` con `concedidos=context.write_namespaces`. Lo encontró el auditor multitenant sobre agent-core#816. Era dentro del mismo negocio, sin fuga entre negocios.
  - Antes, los dos de leads no pasaban los `writeNamespaces` y `crear_pedido` escribía directo, sin pasar por la concesión ni por el aviso de datos.
  - El lead o el pedido se guarda siempre. Si el agente no tiene `perfil` concedido o el dato no se admite, solo se omite la copia al perfil (logs `ventas_perfil_no_concedido` / `ventas_perfil_dato_no_admitido`) y la tool responde igual.
  - `crear_pedido` sigue guardando solo con `guardar_datos_cliente` (D17). Con el aviso de datos activo y sin aceptación, guarda solo los campos requeridos de la acción (`ventas.pedido_domicilio`/`ventas.pedido_retiro`) y el resto espera. Con el aviso apagado (lo predeterminado), el comportamiento no cambia. La copia al perfil ahora se hace después de crear el pedido: si el pedido falla, el perfil no cambia.
  - Los valores vacíos ya no pisan datos del perfil.
- `datos_cliente.guardar()` exige `concedidos` (keyword-only, sin valor por defecto), así que una tool nueva no puede olvidarlo.
- El test estático de ADR-0179 ya no exceptúa `pack_commerce/skills.py`: esa excepción explica por qué no detectó la escritura directa.
- Se corrigió el docstring de `core/memory/facts.py`: las tools de los packs ya no figuran como escritoras sin chequeo.
- No hay migración ni cambio de API.

### Canal Telegram: webhook de entrada (ADR-0199, PR core 3) (2026-11-16)

- `POST /webhooks/telegram/{public_id}` (`core/gateway/telegram/webhook.py`). El negocio sale solo de la ruta (`telegram_route_connection`) y se exige el secreto de la conexión en `X-Telegram-Bot-Api-Secret-Token`. Sin el encabezado, o con una forma inválida, responde 401 sin tocar la base; el cuerpo se lee después de verificar el secreto.
  - Respuestas: 401 con secreto malo, 404 con `public_id` desconocido, 413 con un cuerpo de más de 64 KiB y 503 con el kill-switch apagado o Redis caído.
  - Grupos, mensajes editados, stickers y JSON raro responden 200 sin procesar. Una conexión pausada o un negocio no operativo, también.
- Auditoría en la tabla de webhooks con `telegram:<bot_id>`, sin el texto. La idempotencia usa llaves globales que no dependen de `update_id` (Telegram lo elige al azar tras una semana sin updates): `tg:<bot_id>:in:<chat_id>:<message_id>` y `tg:<bot_id>:cb:<callback_query.id>`. Un reintento de Telegram no duplica.
- Sentry tapa también los locales con el texto y el nombre del cliente (`raw_body`, `update_`, `mensaje`, `caption`, `first_name`…).
- Un toque de botón se publica con su `interactive_id` y el título visible, y se contesta con `answerCallbackQuery` (2 s, best-effort).
- Métricas: `telegram_webhook_total{resultado}` y `telegram_webhook_duracion_seg`.
- `agentcore_channel_telegram.adapters.TelegramInbound`, expuesto por el entry point:
  - Solo procesa chats privados con una persona: texto (`/start` incluido), foto y nota de voz con su caption, y `callback_query`.
  - Los medios van como `file_id`; se descargan en el PR core 5.
- Consumer: una conversación de Telegram sin su conexión validada no se crea (falla cerrado, como Twilio).
- Avisos por evento: `telegram` es un tipo de texto sin ventana de 24 h. El negocio puede definirle un texto de respaldo y la cobranza también sale por ahí.

### Canal Telegram: salida y voz para cualquier conexión (ADR-0199, PR core 4) (2026-11-16)

- `TelegramOutbound`:
  - Texto, que sale en partes de 4096 caracteres si es más largo.
  - Foto y nota de voz (`sendVoice`), por URL.
  - Botones y listas como teclado inline, una fila por opción. La lista no se degrada en el adaptador.
  - Devuelve el id global `tg:<bot_id>:out:<chat_id>:<message_id>`.
- `resolve_credentials`: una conversación de Telegram sale solo por su bot. Si el bot está pausado, borrado o el kill-switch está apagado, no se envía nada. Un bot nunca es respaldo de otra conversación.
- Ids de botón de más de 64 bytes: viajan como `t:<token>` (Redis, 7 días) y el webhook los devuelve al original. Tras el toque se quita el teclado.
- Ritmo de salida:
  - 25 mensajes por segundo por bot y 1 por segundo por chat.
  - Tras un 429, el bot espera los `retry_after` que pide Telegram.
- Errores de envío:
  - Un 401 deja la conexión `revoked` y un 409, `error`.
  - Un 403 («bot bloqueado») falla ese envío sin reintentos y **sin dar de baja** al cliente.
- **La voz no está atada a una conexión** (enmienda ADR-0197):
  - `CapacidadesCanal` suma `voz` (hoy QR y Telegram) y la política de `pack-voice` decide por esa capacidad, no por el proveedor.
  - Si un canal no sabe mandar audio, o lo rechaza, sale el texto ya escrito.
- Telegram queda **disponible en el catálogo y en el selector** para todos los negocios, salvo con el kill-switch apagado. El catálogo de mensajes lo ofrece en las opciones con botones.

### Canal Telegram: el agente pide el teléfono con un botón (ADR-0199 §14, HU-CONN-26) (2026-11-16)

- Tool nueva del core, `pedir_telefono`: manda al cliente de Telegram el botón «Compartir mi teléfono» (`request_contact`). El texto y la etiqueta los pone el agente.
  - Se ofrece en los negocios con una conexión de Telegram activa y el kill-switch encendido.
  - No envía nada si el canal de la conversación no lo permite (`canal_sin_pedir_contacto`) o si el cliente ya tiene teléfono (`ya_tiene_telefono`). Una llamada repetida no lo duplica (`ya_enviado`).
  - En el playground se simula.
- Capacidad nueva `pedir_contacto` en `CapacidadesCanal`, solo en `telegram`, y tipo interactivo `contacto` (`sender.contacto_valido`).
- Migración 0139: el CHECK `ck_messages_interactive_objeto` acepta `contacto`.
- `TelegramOutbound` lo manda como teclado de respuesta de un solo uso. Lo que el cliente comparte lo guarda el consumer en `contact_phone` (PR core 5).
- Los agentes ya publicados la reciben al republicar (HU-ARCH-34).

### Canal Telegram: fotos, voz y teléfono compartido (ADR-0199, PR core 5) (2026-11-16)

- Las fotos y las notas de voz que llegan por Telegram se descargan (`core/media/telegram_media.py`), y se transcriben y entienden como en WhatsApp.
  - La descarga usa `getFile` y el token del bot de la conexión de la conversación, con un tope de 20 MB.
  - Los errores salen clasificados y sin el token.
- **Teléfono compartido**:
  - `InboundMessage.shared_phone` es un campo nuevo y aditivo.
  - El adaptador lo llena solo con el contacto del propio remitente, en E.164.
  - El consumer lo guarda en `contact_phone` sin pisar otro número ni fusionar clientes.
  - El número no va en el texto del turno, que lleva «[Compartió su teléfono]».
- Sentry tapa `phone_number`, `contact`, `shared_phone` y `telefono`.
- Pedir el teléfono (`request_contact`) queda para un PR aparte, como tool del agente.

### Preset `citas` para negocios que atienden con cita (2026-11-16)

- Nuevo preset `citas` («Citas y reservas»): `pack_calendar` + `pack_commerce` (leads), playbooks `citas` y `seguimiento`, agente `agenda`, plan mínimo `basic`. Sin fragmentos de pack: la agenda usa la fuente interna por defecto.
- `peluquería`, `barbería`, `salón de belleza`, `spa` y `consultorio` pasan de `suscripciones` a `citas` (más `estética`, `uñas`, `manicure`, `masajes`, `odontología`, `veterinaria`, `reservas`, `agenda`). En la prueba de contratación del Free en test (2026-11-15), «peluquería» quedaba como «Planes por suscripción» y el alta le instalaba Ventas y Suscripciones.
- Prueba nueva: ningún sinónimo puede estar en dos presets (volvería ambiguo `tipo_desde_texto`).

### Cuándo pide el teléfono el agente en cobranza, citas y suscripciones (HU-CONN-26) (2026-11-16)

- **Cobranza** (`pack_cobranza` 0.7.0, playbook `cobranza.md`):
  - Si `cob_consultar_saldo` o `cob_estado_cuenta` no encuentran al cliente, el agente llama `pedir_telefono` una vez y vuelve a consultar cuando el cliente comparte el número.
  - En un canal sin botón, o si el cliente no lo comparte, no le pide que escriba el número (un número escrito no encuentra su cuenta) y le ofrece un gestor.
- **Enlace de cobranza** (adenda de ADR-0179 §7):
  - El camino perezoso (`enlazar_customer`) acepta también el teléfono verificado de Telegram. El alta y el backfill siguen solo con WhatsApp.
  - Un cliente de Telegram sin teléfono ya no deja caché negativo (resultado `sin_telefono`): al compartirlo se enlaza en el turno siguiente.
  - `tests/test_contact_phone_escritores.py` fija quién escribe `Customer.contact_phone`.
- **Citas** (`pack_calendar` 2.5.0, procedimiento `agendar_cita` 1.1.0, etapa `datos`):
  - El teléfono se pide solo si la política del negocio lo pide (por defecto `no_pedir`), con `pedir_telefono` si la tool existe y, si no, por texto.
  - No reemplaza el correo.
- **Suscripciones** (`pack_subscriptions` 0.7.0, procedimiento `contratar_plan` 2.1.0, etapa `datos`):
  - Si el perfil ya tiene `contact_phone`, el agente lo usa para `telefono` sin pedirlo. **Cambio visible también en WhatsApp:** ahí el número siempre está, así que ya no se le pregunta al cliente.
  - Sin número, lo pide con `pedir_telefono` o, si el canal no tiene botón, dentro de la lista numerada.
- **Evals:**
  - El golden admite `spec` (otro spec de `evals/specs/`) y `connection_types` por escenario.
  - Escenarios nuevos: `39-cobranza-pide-telefono-telegram` (`must_call_tool: pedir_telefono`) y `38-cobranza-sin-boton-no-pide-numero`.
  - Grabados con el LLM real (OK del dueño) y sumados al gate en replay (`scripts/gate_evals.sh`). El gate con LLM real de la release no cambia.
- Las reglas de citas y suscripciones van en la etapa del procedimiento y no en `citas.md` ni en `planes.md`, para no cambiar el prompt de los journeys grabados.

### Leads en el core, parte 1: tablas y copia desde commerce (ADR-0193, HU-AOS-70) (2026-11-16)

- Migración 0146: tablas `leads`, `lead_stage_changes`, `lead_followups` y `lead_settings`, con RLS forzada, un lead vivo por cliente, origen con catálogo cerrado y borrado lógico.
- Comando `agent-core db copiar-leads [--dry-run] [--tenant] [--lote]`: copia los leads de commerce, su historia, sus seguimientos y la config de intake al core, sin tocar los originales. Es idempotente y avisa de los leads que quedarían ocultos.
- Todavía nadie escribe en las tablas nuevas: el cambio de dueño llega en la parte 2.

### Bienvenida de 9 pasos (2026-11-16)

- HU-UX-23 (SPEC-47, D-75). El recorrido del onboarding pasa de 6 a 9 pasos (`TOTAL_PASOS = 9`): Tu
  negocio, Identidad, Cómo habla, Horario y reglas, Habilidades, Flujo, Conocimiento y datos, Pruébalo y
  Publicar. Lo guardado lleva `pasos`; un recorrido guardado con 6 (sin esa clave) se lee en su
  equivalente de 9 (`PASO_DE_6_A_9`) y, al escribirse otra vez, queda guardado con los 9 sin perder el
  paso. `POST /admin/onboarding/avanzar` admite pasos del 1 al 9. La APK sigue con sus 6 pasos: recorta
  el paso al suyo.
- `AvanzarIn` suma `pasos` (opcional): los clientes de 9 pasos mandan `pasos: 9`; sin él, el paso viene
  del asistente de 6 (panel o APK anteriores) y se guarda traducido. Así desplegar el backend antes que
  el panel, o una APK sin OTA, no deja pasos corridos guardados en la base.

### Pausa por agente (HU-ARCH-38, segunda parte, 2026-11-16)

- Un especialista se pausa o reanuda con `POST /api/v1/admin/agents/{id}/pause` (`{reason}`) y `.../resume`, con el permiso de escritura de Configuración. Es estado vivo (`agents.paused_at`, `paused_by`, `paused_reason`, migración 0141): aplica en el siguiente turno, sin publicar una release (ADR-0200).
- Un especialista pausado no atiende: el turno pasa al agente por defecto sin cambiar el dueño de la conversación (al reanudar, vuelve a él). Si el de por defecto también está pausado, el turno se cierra en silencio con `kill_switch`.
- A2A niega delegar, pasar la conversación o consultar a un agente pausado (`agent_paused`, nueva razón en `a2a_events`), y el modelo ni lo ve entre los destinos.
- El Coordinador no se pausa (409 `agent_reserved`: para eso está la pausa del negocio) ni un agente archivado (409 `agent_archived`). Cada cambio queda en la auditoría del negocio y en `agentcore_kill_switch_changes_total{level="agent"}`.
- `AgentSummary` suma `paused`, `paused_at` y `paused_reason`. La pausa por agente no frena jobs: corren por negocio.

### Interruptor global de la plataforma (HU-ARCH-38, primera parte, 2026-11-16)

- Nuevo interruptor que detiene en ≤ 5 s todos los agentes y los envíos automáticos de todos los negocios, sin desplegar (ADR-0200, D-76). Vive en la tabla de sistema `platform_kill_switch` (migración 0140, sembrada apagada) con su historial `platform_kill_switch_events` (solo inserciones).
- Se opera con `PUT /api/v1/admin/platform/kill-switch` (solo master; un no master recibe 403 y queda registrado) o con `agent-core admin kill-switch --activar --motivo "…"` / `--desactivar`. Activarlo exige motivo; cada cambio avisa a Slack y suma `agentcore_kill_switch_changes_total`.
- Con el interruptor activo: lo que entra se guarda y el turno se cierra en silencio con `kill_switch`; el guardián de HU-ARCH-37 frena todo lo automático con ese motivo; los crons programados que escriben a clientes (`CronMeta.writes_to_customers`) no corren, y a mano corren pero sus envíos se frenan. Lo que escribe una persona sale.
- Falla cerrado: si no se puede leer el interruptor se trata como activo, con evento, métrica `agentcore_kill_switch_read_failures_total` y alerta.
- Al reactivar no se responde en lote: la recuperación de pendientes no reprograma lo que llegó antes de la última reactivación.
- La pausa por agente llega en la segunda parte de HU-ARCH-38.

### La pausa del negocio y del cliente frena lo automático (HU-ARCH-37, 2026-11-16)

- Con `app_settings.bot_paused` (o `customers.bot_paused`) no sale nada automático hacia el cliente (D-76): respuestas del agente, seguimientos, recordatorios de cita, avisos de cobro, opciones, correos y envíos a segmento. Lo que escribe una persona desde el panel o la app (`sender_type="human"`) sigue saliendo. Lo frenado queda `failed` con el motivo (`negocio_pausado` o `cliente_pausado`) y no se reenvía al reactivar.
- Un solo guardián (`core/gateway/pausa_automatica.py` + `sender._registrar_si_frenada`) en texto, voz, plantilla e interactivo, antes del canal de prueba; re-chequeo en el worker para lo ya encolado (`PausaAutomaticaError`, sin reintentos ni escalamiento); los avisos por evento terminan con su propio resultado, sin `NoticeNotSentError`; `egress` no escala una respuesta frenada por la pausa.
- Seguimientos de leads y cotizaciones: se salta el negocio pausado y la marca («contactado», `follow_up_sent_at`) solo se gasta si el mensaje salió; si no (pausa, ventana cerrada, conexión pausada), sigue siendo candidato dentro de su ventana.
- Envíos a segmento en curso: con el negocio pausado los destinatarios que faltan quedan `omitido` (`negocio_pausado`) y el envío termina; un cliente pausado queda `omitido` (`cliente_pausado`).
- Las opciones que manda un operador y los avisos de cita que dispara el panel se frenan también en esta versión (decisión del dueño).
- Lo que va al equipo del negocio (correos de cita a los responsables, avisos internos) no se frena. El aviso de renovación del plan de la plataforma tampoco: si el SMTP del vendedor no lo manda, sale por el de la plataforma, porque es la facturación de SolversHub y no un mensaje del negocio a su cliente.
- Texto de la opción «Pausar el asistente» actualizado. Sin migración. Los crons que solo escriben datos (cargos, mora, cierre) siguen corriendo.

### El perfil y los recuerdos del cliente entran al prompt como dato (HU-ARCH-35, 2026-11-16)

- El perfil del cliente y los recuerdos de `recall` se serializan con `tarjeta.escapar`: cada clave y cada valor van como cadena JSON de una línea, así un valor con saltos de línea o un encabezado falso («## Reglas…») ya no abre instrucciones en el system prompt. El escape se hace al leer, así que cubre también lo guardado antes. Los rótulos y el aviso de que son datos salen del playbook del kernel `memoria-del-cliente` (`core/memory/memoria_en_prompt.py`).
- `remember_fact` rechaza con un error de negocio (sin guardar y sin autodeshabilitar el pack) los recuerdos de más de 200 caracteres, con saltos de línea o caracteres de control, o con datos sensibles (números largos, tarjeta, salud, contraseñas). La validación del contenido aplica a los dos motores; el permiso es solo de v3: un agente sin el namespace `perfil` en `writeNamespaces` no puede guardar recuerdos, y en legacy no se chequea.
- Golden set: escenario 37 de memoria envenenada; el runner acepta `profile` y `recalled_memories`.

### Test: cada negocio publica con su modelo por defecto (2026-11-16)

- HU-AOS-79. Test de integración del escenario de aislamiento: dos negocios con modelos por defecto
  distintos; un agente con un modelo que no sirve publica con el de su propio negocio. Solo tests.

### El agente no le dice «trialing» al cliente (2026-11-16)

- `sub_inscribir` y las tools que devuelven la suscripción (`sub_consultar` y las que usan la misma vista) traen `estado_para_el_cliente` con el estado en palabras simples («en período de prueba», «pendiente de pago»…), además del código `estado`. En la prueba de contratación del Free en test el cierre decía «Estado: trialing (prueba activa)».
- Sin cambio en las descripciones de las tools ni en los playbooks: las grabaciones de replay no cambian.

### Crons por negocio y fechas en la zona del negocio (2026-11-16)

- ADR-0201. Los crons de negocio se pueden encender/apagar, programar a una hora local o dentro
  de una ventana y dejar su último resumen por negocio (`tenant_cron_configs` con RLS). El master
  conserva la operación global (`Plataforma › Crons`); el dueño y el colaborador con `write` usan
  `Configuración › Automatizaciones`. Historial por negocio (`tenant_cron_runs`, solo ejecuciones
  con efecto/fallo/manual, 90 días) y vista master «Por negocio». Los crons diarios pasan a cadencia horaria con
  `hora_local_por_defecto`, para que cada negocio elija su hora sin crear tareas de beat por tenant.
- ADR-0202. Las fechas de los registros se pintan en la zona de `app_settings.timezone`:
  `lib/fechas.ts` + `useFormato()` en el panel, `MeResponse.timezone/currency`, y `formatDateTime`
  deja de usar la zona del servidor (UTC). Los crons de cobranza calculan «hoy» con el reloj del
  negocio (`core/shared/tiempo_negocio.py`) y la API de cobranza devuelve instantes ISO 8601 en vez
  de texto preformateado en UTC.

### El chequeo de citas no confunde cancelaciones de otros módulos (2026-11-16)

- `calendar_completion_check` (pack-calendar) ya no exige `cancel_calendar_event` cuando la cancelación es de otro módulo («tu suscripción quedó cancelada», «el pedido fue cancelado»): la afirmación solo cuenta si en la oración no hay un sujeto de suscripciones, pedidos o cobros. Las frases sin sujeto («ya quedó cancelada») siguen exigiendo la tool (D-76).

### WhatsApp Web: la nota de voz del agente se reproduce (2026-11-15)

- ADR-0197: neonize 0.10 arma el `audioMessage` con el mimetype de libmagic (`audio/ogg`) y WhatsApp solo reproduce una nota de voz (PTT) con `audio/ogg; codecs=opus`: el cliente recibía una burbuja vacía. `NeonizeSocket.send_audio` construye el mensaje con `build_audio_message`, fija `MIMETYPE_NOTA_DE_VOZ` y lo envía con `send_message`.

### HU-CONN-20 · Sandbox de Twilio, parte 4: logs y contadores (2026-11-15)

- Logs: `twilio_sandbox_alta`, `twilio_sandbox_gate_apagado` (con `via`: alta, webhook, envío), `twilio_sandbox_webhook_confirmado`, `twilio_sandbox_no_unido` (63015 en la prueba, el envío o el callback de estado), `twilio_sandbox_firma_invalida`, `twilio_sandbox_reintento_ritmo`, `twilio_sandbox_desconectado`, y `modo` en `twilio_webhook_received`. La ronda de salud suma `sandbox_sin_confirmar` al log `twilio_salud_ronda`.
- Métricas (OpenTelemetry, etiquetas cerradas): `agentcore_twilio_sandbox_events_total{evento}` (alta creada/repetida/rechazada, gate apagado, webhook confirmado, firma inválida, no unido, reintento por ritmo, desconectado); las conexiones sin confirmar van en el log de la ronda (un gauge daría valores viejos con el prefork del worker). La latencia y los errores de `POST /sandbox` salen de `agentcore_http_server_duration_seconds`.

### HU-CONN-20 · Sandbox de Twilio, parte 3: webhook y operación por modo (2026-11-15)

- Webhook: el primer entrante con firma válida y `AccountSid` correcto (no un estado: ese llega por el `StatusCallback` de cada envío) marca `webhook_confirmado_at` (un solo `UPDATE … WHERE webhook_confirmado_at IS NULL`) y pasa el sandbox de `pending` a `connected`. La firma no cambia: sigue fail-closed. Con `TWILIO_SANDBOX_PERMITIDO` apagada, un sandbox responde 200 sin procesar y no envía (`credenciales_de_envio` devuelve vacío).
- Salud de un sandbox: solo la cuenta (`GET /Accounts`) y la confirmación; nunca la Senders API.
- `POST /{id}/webhooks` en sandbox: 409 `sandbox_webhook_manual` con las dos URL. Rotar credenciales revalida la cuenta y no deja cambiar de cuenta (422 `sandbox_otra_cuenta`). Desconectar no restaura nada y responde `requiere_limpiar_consola=true`.
- Mensaje de prueba: el 63015 responde 422 `participante_no_unido` con el `join <código>` y el número del sandbox.
- Plantillas: crear, enviar a aprobación y borrar responden 409 `no_aplica_sandbox`; sincronizar sigue permitido.

### HU-CONN-20 · Sandbox de Twilio, parte 2: gate, alta y rutas (2026-11-15)

- `core/gateway/twilio/sandbox.py`: el único módulo que lee `TWILIO_SANDBOX_PERMITIDO` (lo vigila una prueba de arquitectura). Apagada, las rutas del sandbox responden 404.
- `alta.conectar_sandbox`: valida solo la cuenta (`GET /Accounts/{sid}`, sin Senders API), guarda `sandbox:<AccountSid>` con el número compartido como `From` y deja la conexión `pending` con `requiere_accion_manual` hasta el primer mensaje firmado. Es idempotente por cuenta, con el mismo candado advisory que el alta de senders. La misma cuenta en otro negocio da 409 `cuenta_en_otro_negocio`.
- API `/admin/connections/twilio`: `GET /capabilities` (`sandbox_disponible`, `sandbox_e164`), `POST /sandbox` (201/200) y `PATCH /{id}/sandbox` (`join_code`). Propietario o master con la bandera `canales.whatsapp_twilio`. `ConexionTwilioOut` suma `modo`, `sandbox_join_code`, `webhook_confirmado_at` y `requiere_accion_manual` (aditivo).

### HU-CONN-20 · Sandbox de Twilio, parte 1: esquema y configuración (2026-11-15)

- Migración 0136 (aditiva): `twilio_connections` suma `modo` (`sender|sandbox`), `sandbox_join_code` y `webhook_confirmado_at`; `sender_sid` admite NULL solo en modo sandbox (CHECK). Índice único parcial de un sandbox vivo por cuenta en cada negocio. La bajada falla si existe alguna conexión sandbox, viva o borrada.
- `Settings.twilio_sandbox_permitido` (`TWILIO_SANDBOX_PERMITIDO`, apagada por defecto) y `twilio_sandbox_e164` (`+14155238886`). Todavía no se leen: las rutas y el gate llegan en la parte 2.
- `save_connection_in` acepta `external_id_override` (el sandbox se guarda como `sandbox:<AccountSid>`).
- channel-twilio: el error 63015 (participante no unido al sandbox) es `PERMANENT`: no se reintenta y no da de baja al cliente.

### WhatsApp vía Twilio · resultado real del mensaje de prueba (2026-11-15)

- Twilio acepta la prueba (201) y el rechazo llega después por el status callback: el 63015 del Sandbox (el número no mandó `join`) o el 63016 de un sender real (fuera de la ventana de 24 h). Antes el panel decía «Enviamos la prueba» y nunca veía el rechazo.
- `enviar_prueba` guarda el SID de la última prueba por conexión en Redis (`twilio_prueba_ultima:{tenant}:{conexión}`, TTL de un día, sin migración) y el webhook de estado le aplica cada callback de ese SID. El estado solo avanza (`queued < sent < delivered < read`; `failed` es final) y se escribe con un script Lua, así que dos callbacks simultáneos no se pisan.
- Nueva ruta `GET /api/v1/admin/connections/twilio/{connection_id}/test-message/{message_sid}` (Conexiones en lectura, bandera `whatsapp_twilio`): `estado`, `provider_status`, `error_code` y `motivo` en palabras (63015 con el `join <código>` y el número del sandbox; 63016; 63003/63024; los demás con el código de Twilio). 404 si ese SID no es la última prueba de la conexión o ya venció.
- `twilio_connections.last_error_code` no cambia: es la salud de la conexión, y que un destinatario no se haya unido no la pone en error. Log nuevo `twilio_prueba_estado_fallido` si Redis falla al aplicar el estado (el webhook igual responde 200).

### Canal Telegram: base (ADR-0199, PR core 1) (2026-11-15)

- Migración 0137:
  - Agrega la tabla `telegram_connections`, 1:1 con su conexión, con RLS forzada.
  - Agrega el índice único global `uq_channel_connections_telegram_bot_viva`, para que un bot vivo esté en un solo negocio.
  - Agrega la función `telegram_route_connection(public_id)` para el webhook.
- Nuevo paquete `packages/channel-telegram` (`agentcore_channel_telegram`):
  - `client.py`: cliente de la Bot API que no deja salir el token del bot.
  - `errors.py`: mapeo de errores. Un 403 de bot bloqueado es `RECIPIENT`, nunca una baja, y un 404 es `CONFIG`, no `AUTH`.
  - `limites.py`: límites de la API y secreto del webhook.
- Tipo de conexión `telegram`, con botones y lista inline. Queda oculto (`available=False`, fuera del selector) hasta el PR core 4. `canales_con` no cuenta los tipos ocultos para todos.
- Kill-switch `TELEGRAM_HABILITADO`, encendido por defecto. Solo lo lee `core/gateway/telegram/habilitado.py`, y `disponible_para` lo mira antes que `available`.
- Redacción del token del bot:
  - En las trazas de OTel se tapa `/bot<token>/` y `/file/bot<token>/`, solo en `api.telegram.org`.
  - En Sentry se tapa el patrón `<id>:<secreto>` y las claves `bot_token` y `webhook_secret`.
- Pruebas que usaban `"telegram"` como canal inválido: ahora usan `"canal_inexistente"`.

### Canal Telegram: alta, operación y salud (ADR-0199, PR core 2) (2026-11-15)

- `core/gateway/telegram/alta.py`: alta del bot del negocio con el token de @BotFather.
  - Valida el token con `getMe` (el `bot_id` sale de ahí).
  - Pide confirmación antes de pisar un webhook ajeno (409 `webhook_ajeno`).
  - **Reclama la conexión antes de `setWebhook`**: un bot vivo en otro negocio es 409 `bot_en_otro_negocio` sin pisar su webhook.
  - Registra el webhook con un secreto nuevo y lo confirma con `getWebhookInfo`. Si no confirma, responde 502 y deja la conexión en `error`; repetir el alta la recupera.
- `core/gateway/telegram/operacion.py`:
  - Salud: `degraded` si el webhook cambió, si hubo un error de entrega reciente o si hay más de 50 pendientes; `revoked` con un 401. Una caída de Telegram no cambia el estado.
  - Reparar el webhook.
  - Mensaje de prueba a un `chat_id`.
  - Cambio de token del mismo bot (otro bot es 409).
  - Desconectar con `deleteWebhook`.
- Rutas `/api/v1/admin/connections/telegram`: alta, lista, ficha, `refresh`, `webhooks`, `test-message`, `credentials` y `disconnect`. Con el kill-switch apagado responden 404, salvo desconectar. Las escrituras exigen ser propietario o master.
- Cron `core.telegram_health_check` cada 15 min, que no consulta a Telegram con el kill-switch apagado. Migración 0138 con `admin_telegram_connection_ids()`.
- Conexiones genéricas:
  - Borrar una conexión de Telegram borra también su fila.
  - Revelar credenciales, pausar o borrar un bot exige ser propietario o master.
  - El formulario manual no convierte un bot en otro tipo.

### Tarjeta de estado: guarda lo que el cliente ya dijo y no ofrece correos (2026-11-15)

- Hallazgo de la prueba del plan Free por chat web en test: el `goal` de las etapas de un procedimiento no llega al
  prompt (el kernel no lo lee), así que los cambios de `contratar_plan` 2.0.1 en `elegir_plan` y `pagar` no cambiaron
  la conducta. El arreglo va en la tarjeta (`core/playbooks/kernel/estado-conversacion.md`), que sí ve el modelo.
- `pide_ahora`: si el cliente ya dijo un dato, aunque sea con sus palabras, se guarda con `actualizar_tarea` en la
  misma vuelta y no se le pide; los datos se piden en palabras simples, nunca por su nombre interno ni un código.
  El agente respondía «1. Plan (el código del plan que quieres contratar)» a «quiero contratar el plan Free».
- `dato_a_pedir`: el rótulo humano va primero (`1. "Plan" (se guarda como plan_code)`).
- `acciones`: no ofrecer mandar por correo ni por otro medio una confirmación o un resumen del recorrido (el agente
  ofrecía «¿Quieres que te envíe la confirmación al correo…?» tras inscribir).
- Sin cambios de pack ni de procedimiento: no hay que reconstruir releases; entra con el despliegue. Ningún cassette
  de replay contiene esas líneas (solo salen con una tarea en foco).

### Prueba del panel: la cotización simulada ya no apaga Comercio (2026-11-15)

- HU-ARCH-14: `simular_generar_cotizacion` y `simular_agregar_item_pedido` leían `data["sin_inventario"]`, que `_get_variant_with_stock` nunca devuelve. Cada cotización en la prueba del panel lanzaba `KeyError` y ADR-0024 apagaba `pack_commerce` para el negocio («el agente no puede acceder al inventario»). El simulador valida el stock igual que el handler real. Test de regresión en `tests/test_sandbox_comercio_stock.py`.

### La prueba no apaga módulos ni cuenta en las estadísticas (2026-11-15)

- Un fallo de una tool durante una prueba (canal `test`: la prueba del panel y los evals) ya no apaga el pack del negocio (ADR-0024). El turno responde con el error y se registra `plugin_error_en_prueba`; las conversaciones reales siguen con el pack habilitado.
- «Clientes nuevos» del Resumen de Ventas ya no cuenta el cliente sintético de la prueba del panel (`pack_commerce/resumen.py`).
- El dashboard SSR `/admin` ya no cuenta los mensajes de la prueba en «mensajes 7 días», como `/api/v1` (`core/admin/views.py`).
- `/admin/connections/{id}/a2a/calls` ya no lista llamadas de la prueba del panel (`core/api/routers/a2a.py`).

### Las mismas plantillas en todas las conexiones de WhatsApp (HU-CONN-21, 2026-11-15)

- Nueva vista agregada `GET /api/v1/admin/whatsapp-templates`: cada plantilla del negocio una vez (nombre e idioma) con su estado en cada conexión de Meta y de Twilio (aprobada, pendiente, sin enviar, rechazada, pausada, falta o no replicable, y si el texto es distinto), y los avisos por evento cuya plantilla falta o no está aprobada en una conexión activa.
- `POST /api/v1/admin/whatsapp-templates/copy` copia una plantilla aprobada a otras conexiones (la crea y la envía a aprobación) con resultado por destino: creada, ya existía, distinta (no se pisa), no replicable o error. `POST …/copy-all` copia todas las que falten a una conexión, hasta 10 por pedido, y dice cuántas quedan.
- Hacia Twilio solo se copian plantillas de texto con variables numeradas (sin encabezado, pie ni botones); de Meta a Meta van los componentes completos salvo un encabezado con multimedia; de Twilio a Meta el texto pasa a un BODY con sus ejemplos. El Sandbox de Twilio y las conexiones pausadas no son destino. En Twilio el nombre no se repite entre idiomas: el mismo nombre en otro idioma queda «no replicable».
- Sin tabla ni migración nuevas. Log `plantilla_replicada` por destino y contador `agentcore_plantillas_replicadas_total` por resultado.

### El agente usa el modelo del negocio cuando el suyo no sirve (2026-11-15)

- HU-AOS-79 (SPEC-47, D-75). Publicar un agente con un modelo retirado o de otro proveedor ya no
  responde 422 `modelo_retirado` / `modelo_de_otro_proveedor`: la versión se congela con el
  modelo por defecto del negocio (`modelo_del_negocio`: el de «Cuenta y proveedor», si no el del
  despliegue cuando el proveedor lo sirve, si no el primero vigente de ese proveedor) y lo anota
  en sus notas y en `warnings`. El borrador no cambia; un modelo válido se respeta.
- `GET /api/v1/admin/agent-models` suma `business_model` y, con `?agent_id=`, `effective_model` y
  `effective_source` (`negocio` o `agente`); 404 si el agente es de otro negocio. Campos aditivos.
- `versiones.publicar(..., modelo=)` reemplaza `model.primary` solo en la versión.

### Test de `/admin/collaborators/existe` al día con `eliminada` (2026-11-15)

- ADR-0198 sumó `eliminada` a `ExisteResponse` y `test_existe_y_username_disponible` seguía esperando solo `existe`: dejaba rojo el CI de `develop` (el PR solo corre `-m aislamiento`). La aserción ahora incluye `eliminada: False`. Sin cambios de código de producción.

### Contratar un plan: hallazgos de la prueba del Free en test (2026-11-15)

- `pack_subscriptions` 0.6.1, `pack_subscriptions.contratar_plan` 2.0.1. Prueba por chat web en test (solvershub, plan Free).
- La etapa `elegir_plan` pide el plan por su nombre y fija `plan_code` con las palabras del cliente: el agente pedía «el código del plan».
- El resumen que el cliente confirma ya no repite el plan («Plan: Free mensual» y «Plan: PLAN-FREE»): la línea del pack se llama `plan_code`, como el dato de la tarea, y el kernel no la vuelve a sumar.
- `sub_listar_planes` con un `plan_code` que no existe (p. ej. «free» por «PLAN-FREE») devuelve el catálogo con `codigo_no_encontrado` en vez de un error: el modelo elige el código real sin otra vuelta.
- La etapa `pagar` dice el número y el estado de la suscripción y no ofrece mandar una confirmación por correo (el acceso lo manda el sistema).
- Tras desplegar, reconstruir las releases de los negocios que declaran `contratar_plan` para que tomen 2.0.1.

### Baja de cuentas de equipo (2026-11-15)

- **Equipo:** el menú de cada persona y su ficha ganan **Eliminar del equipo** (y
  **Reactivar cuenta** en las eliminadas). Solo se ofrece cuando la API lo permite
  (`can_delete` / `can_reactivate`), sin 403 por diseño.
- **Baja lógica (ADR-0198):** la cuenta deja de entrar y desaparece de Equipo; se revocan
  sesiones, vínculos, permisos e invitaciones. La fila `admins` conserva correo, usuario y
  nombre: el historial de cartera (`Pago.registrado_por`) y la auditoría siguen mostrando la
  identidad real.
- **Historial de Cobranza intacto:** el perfil `Gestor` se da de baja lógica (no se borra) y
  conserva sus carteras, pagos, visitas, promesas, gastos y notas. Al reactivar la cuenta se
  revive el MISMO perfil, con su historial.
- **Correo no reasignable:** un correo dado de baja no se reutiliza automáticamente. El alta
  avisa y pide reactivar la misma cuenta; reactivar es una acción explícita (master, o el
  dueño que la dio de baja), restaura los mismos negocios y roles (`deleted_links`) y vuelve
  a ocupar cupo del plan.
- **API:** `POST /admin/collaborators/{id}/delete`, `POST /admin/collaborators/{id}/reactivate`,
  `GET /admin/collaborators?eliminadas=1`; `CollaboratorItem` gana `deleted`, `can_delete` y
  `can_reactivate`, y `ExisteResponse` gana `eliminada`.
- **Migración 0135:** columnas de baja lógica en `admins` (tabla de sistema, sin RLS).

### Onboarding: el aviso solo insiste si no hay agente (2026-11-15)

- HU-SH-10. `recorrido.avisar` y `recorrido.abrir` se apagan en cuanto el negocio tiene un agente
  propio (`agents.kind <> 'core'`), aunque el recorrido esté pendiente, en curso u omitido. El
  Coordinador del alta no cuenta como agente creado. Los endpoints del recorrido siguen disponibles.

### ADR-0186: adenda del modo Sandbox solo en test (2026-11-15)

- HU-CONN-20: diseño aprobado para conectar el Sandbox de WhatsApp de Twilio sin sender propio, detrás de `TWILIO_SANDBOX_PERMITIDO` (apagada por defecto, solo en test). Solo documentación; el código va en PR aparte (migración 0136).

### ADR-0186: ajustes del modo Sandbox al implementar (2026-11-15)

- HU-CONN-20: la adenda pasa a decir que solo un mensaje entrante firmado confirma la conexión sandbox (un estado no prueba que las URL se pegaron en la consola) y que el conteo de conexiones sin confirmar va en el log `twilio_salud_ronda`, no en un gauge. Solo documentación: el código ya lo hace así (#788 a #791).

### WhatsApp Web (QR) ya entiende audios e imágenes (2026-11-14)

- ADR-0196. El canal `waweb` descarga notas de voz y fotos con Neonize, las transporta por Redis y el consumer las procesa con el mismo STT/visión de Meta y Twilio. La cuota de audio/OCR se aplica antes de encolar. Un fallo de descarga responde el texto fijo de media no recibida, nunca en silencio.
- La API de conversaciones expone `media_type`, `media_url` firmada y `media_transcription`; el panel muestra el reproductor de audio, la transcripción y la imagen. Las imágenes salientes por QR ahora salen nativas, no como una URL en el texto.

### Identidad de sesiones WhatsApp Web deslogueadas (2026-11-14)

- La detección de números duplicados ignora sesiones en `logged_out`: una fila
  sin credenciales ya no alerta «número en más de un negocio» en cada reinicio.
- Migración `0132`: limpia `jid`, `phone`, `push_name`, `business_name`,
  `platform` y `linked_at` de sesiones ya deslogueadas (backfill de `0130`).
  Conserva `last_connected_at` como historial.

### Identidad del vínculo WhatsApp Web (2026-11-14)

- El vínculo por QR deja de mostrar `numero: User`: el JID se extrae del proto de
  Neonize (no de `str(proto)`) y se guarda normalizado como `<numero>@s.whatsapp.net`.
- Se persiste la identidad visible del teléfono (`phone`, `push_name`, `business_name`,
  `platform`) y la fecha de primera vinculación (`linked_at`), que se conserva al
  reconectar y se limpia al re-vincular o cerrar sesión.
- La ficha de la conexión y la fila de Conexiones del panel muestran nombre, número,
  negocio, dispositivo y fechas. La APK muestra nombre y número en Configuración →
  Conexiones.
- Migración `0130`: agrega las columnas y normaliza los JID viejos (`User: "..."`);
  `linked_at` se rellena desde `last_connected_at` como mejor aproximación.
- `GET /admin/meta/waweb`, `GET /admin/connections` y `GET /admin/connections/console`
  exponen los nuevos campos.

### Voz y calidez con efecto real (2026-11-14)

- El playbook `tono.md` interpola `{tone}`, `{warmth}` y `{emojis}` y explica cada nivel: tono, calidez
  0–5 y emojis dejan de ser texto fijo y cambian el prompt de verdad.
- `sembrar_voz_del_negocio` publica una versión nueva del Coordinador cuando la persona del negocio
  cambió, antes de construir la release. El primer agente del onboarding hereda la voz elegida.
- La calidez se alinea al contrato 0–5 en panel y APK, con seis etiquetas (`Seco`…`Muy cálido`) y un
  preview que distingue Cálido (4) de Muy cálido (5).
- Tests nuevos de interpolación del prompt y de la siembra de voz; se regraban las grabaciones del gate
  de evals.

### Test del sandbox del playground estable (2026-11-14)

- `test_pedido_simulado_no_toca_order_ni_consecutivo` compara cuántos pedidos tenía el
  tenant antes y después del pedido simulado, en vez de exigir que no haya ninguno. El
  tenant de prueba es compartido con otras suites que crean pedidos y, con xdist, el
  total dependía del orden de los tests (falló en develop @ fd7c197).

### Helper de IA estructurada en el core y ADR-0194 (SPEC-45, D-71, 2026-11-14)

Primera entrega de SPEC-45 «Carga de datos al crear un agente».

- **`core/ia/estructurado.py` (nuevo):** un solo camino para pedirle a la IA un objeto con forma conocida
  fuera del turno. `generar_estructurado` (texto) y `reconocer_imagen` (imagen, sobre
  `core.media.vision.describir_imagen`) reciben un playbook del kernel, un esquema pydantic y la entrada
  como datos; resuelven proveedor y `key_mode` (llave propia del negocio o de plataforma), validan el
  primer objeto JSON y miden la llamada en `usage_ledger` con el `kind` y el `result` de quien llama
  (créditos por `creditos_por_evento`, concepto `llm_turn` o `vision`). Errores `ErrorProveedor` (sin
  fila) y `ErrorFormato` (con fila).
- **Datos de prueba (HU-QA-10):** `core/qa/datos_generados.py` usa el helper. Sin cambio de
  comportamiento: mismos respaldos a plantilla (motivo `proveedor` o `formato`), tiempo límite, tokens y
  fila `generacion` con créditos de plataforma.
- **ADR-0194 (propuesto):** importación con IA en producción y cobro de acciones de IA cotizadas.
  Enmienda ADR-0180 §8, ADR-0144 §11 y ADR-0171 §2; nombra el hook `describir_registros` y registra el
  retiro futuro de `POST /pack/commerce/products/import`.
- **Despliegue:** sin migración ni cambio de API.

### Importar el catálogo desde fotos: una foto = un producto, cotizado y cobrado (SPEC-45, ADR-0194 §2 y §3, 2026-11-14)

- **Rutas** (con la misma compuerta de producción que la importación: plantillas `en_produccion`):
  - `GET …/importar-datos/fotos/cotizacion?fotos=N`: créditos de N fotos, por foto, si el negocio usa
    llave propia, usado/límite/disponible del mes y si alcanza. No escribe.
  - `POST …/importar-datos/fotos` (una foto): la guarda con el perfil `producto` (nombre = sha256
    del contenido), cotiza y verifica el crédito (409 `plan_limit_creditos` sin llamar al proveedor),
    la reconoce con visión (playbook del kernel `reconocer_foto`, esquema de la plantilla) y
    devuelve la fila propuesta para la precarga con la foto. 422 `foto_no_reconocida` si no se ve un
    producto (se cobra, el proveedor ya cobró); 503 `vision_no_respondio` sin cobrar.
- **Idempotencia:** la misma foto otra vez se responde desde el ledger (`result.salida`) sin llamar ni
  cobrar (`repetida = true`).
- **Ledger:** `kind = 'vision'` con créditos reales y `result = {proposito: 'foto_catalogo', image_key,
  agent_id, plantilla, salida}`. `reconocer_imagen` gana `guardar_salida`.
- **Contrato de importación:** `PlantillaDeImportacion` gana `esquema_foto`, `desde_foto` y `columna_foto`
  (columna interna, fuera de la plantilla descargable); `PlantillaImportacionOut.importa_fotos`.
- **Commerce:** la plantilla `productos` importa fotos (`ProductoEnFoto`: nombre, descripción, categoría
  y precio solo si se ve). La fila propone el SKU `FOTO-<huella>` y la columna `imagen`; al importar se
  valida que la foto sea de este negocio y queda como `image_key` del producto (`seed_catalog`).
- **Despliegue:** sin migración propia; necesita la 0129 de HU-BILL-16 (#765) y el PR del CSV (#767).

### Importar el catálogo desde un CSV con mapeo de IA, también en producción (SPEC-45, ADR-0194 §2, 2026-11-14)

- **Mapeo de columnas** (`core/qa/mapeo_columnas.py`, playbook del kernel `mapear_columnas`): el dueño sube
  su hoja con sus cabeceras. Primero se reconocen por nombre y por los sinónimos que declara el pack
  (`PlantillaDeImportacion.sinonimos`); solo si quedan cabeceras sin reconocer y columnas libres, una
  llamada a la IA con las cabeceras y 5 filas de muestra. La IA solo puede asignar columnas libres de la
  plantilla, cada una una vez; si falla, esas cabeceras quedan sin asignar. Se mide en `usage_ledger`
  (`kind='generacion'`, `result.proposito='mapeo_columnas'`) con la llave del negocio si la tiene.
- **Precarga sin estado en el servidor:** `POST …/importar-datos/mapear` devuelve el mapeo, las filas
  normalizadas y su vista previa; el cliente las edita y las confirma en JSON con
  `POST …/importar-datos/filas/validar` y `POST …/importar-datos/filas`, que vuelven a validar contra el
  esquema del pack (mismos errores por fila, 422 `archivo_con_errores` salvo `solo_validas`,
  idempotente por clave). Auditoría con `fuente = 'precarga'`.
- **Producción:** las rutas de importación responden sin `QA_TESTERS_ENABLED` solo si la plantilla del
  agente se declara `en_produccion` (hoy, `productos` de commerce); las demás siguen en 404.
  `PlantillaImportacionOut.en_produccion` lo expone para que el cliente pinte lo que el servidor declara.
- **API (compatible):** 3 rutas nuevas y un campo nuevo en `PlantillaImportacionOut`.
- **Despliegue:** sin migración. Depende del helper de IA estructurada (PR #764).

### Pruebas del panel efímeras (2026-11-14)

- HU-ARCH-14. El playground no ejecuta tools con efectos en el canal `test`: comercio, agenda,
  cobranza, suscripciones, pagos, correo y plantillas usan simuladores y estado sandbox en Redis.
- Contadores, Dashboard, Costos, fiabilidad, cuotas y listas de packs excluyen las conversaciones
  de prueba; un pedido simulado no toca `crm_order_counters` ni descuenta stock.
- Nuevo `POST /admin/playground/close` y purga física acotada (`purge_test_conversation`): al
  cerrar o reiniciar la prueba no quedan conversación, cliente, mensajes, tareas, ledger,
  decisiones, auditoría ni memoria. Tarea horaria para pruebas huérfanas.
- Auditoría de CI: toda tool declara `solo_lectura` o `side_effects` + simulador.
- Panel y APK cierran la prueba al salir y muestran «Acción simulada». ADR-0195.

### Probar el borrador de un agente sin publicarlo (2026-11-14)

- HU-AOS-72: con `agent_id`, `GET /admin/playground`, `POST /admin/playground/message`, `/reset` y
  `/close` prueban el **borrador** de ese agente sin publicarlo: el turno corre con una release efímera
  armada en memoria (sin release, versión ni instancia), directo a ese agente (sin el Coordinador),
  siempre simulado (sandbox de HU-ARCH-14) y sin cupo de agentes.
- Exige escritura en `settings` (la sección del editor de agentes). Un agente que no es un especialista
  activo del negocio con borrador es 404 `agente_no_encontrado`, sin eco del id.
- Adenda HU-AOS-72 de ADR-0191 y nota en ADR-0117.

### Respuestas por voz con `pack-voice` (2026-11-14)

- ADR-0197. El agente puede responder con notas de voz en WhatsApp Web (QR) cuando el cliente manda un audio: modo mixto configurable (probabilidad, sin dos voces seguidas, voz + texto, tope de caracteres) con TTS de OpenAI en el worker y fallback a texto. Solo `waweb` en esta tanda.

### Lo cargado para probar el agente, en palabras (HU-QA-16, SPEC-45, 2026-11-14)

El asistente de crear agentes muestra la lista de lo que se generó o importó para tenerla en cuenta al
probar el agente. Hasta ahora la API solo devolvía conteos.

- **Hook nuevo y opcional `describir_registros(tenant_id, registros)`** en `BaseModule` (ADR-0194, en
  curso), con el tipo `RegistroDescrito` en `core/qa/contrato_datos.py`. Por lote, solo lectura, bajo
  `tenant_scope` y filtrando por `tenant_id`. Lo implementan:
  - `pack_commerce`: productos con precio (el menor de sus variantes activas, «desde» si varían) y categoría.
  - `pack_calendar`: servicios (tipos de cita) con su duración. Citas y bloqueos no salen como filas.
  - `pack_cobranza`: clientes con su cartera, valor y saldo de sus préstamos vivos.
  - `pack_subscriptions`: planes con precio, periodicidad y días de prueba; suscripciones con el nombre del
    cliente, su plan y su estado. Clientes y cargos no salen como filas.
- **API (aditiva):** `GET /api/v1/admin/agents/{agent_id}/datos-de-prueba/registros` →
  `RegistrosDePrueba` (`lote`, `registros[]` con `tipo`/`nombre`/`detalle`/`origen`, `total`,
  `sin_detalle`). Solo con `QA_TESTERS_ENABLED` (404 sin ella) y sección `agents`, como el `GET` del
  estado. Trae el último lote generado más lo importado vivo; lo de un módulo cuya sección el usuario
  no ve no sale. Hasta 200 filas. `LoteGenerado` no cambia.
- **Despliegue:** sin migración.

### Cotizar y verificar créditos antes de una acción de IA (HU-BILL-16, 2026-11-14)

Primera pieza del cobro de las fotos de catálogo (SPEC-45, ADR-0194, D-71): el ciclo
cotizar → confirmar → verificar saldo → registrar. Aquí van las piezas de `core/billing`; el endpoint
de fotos (HU-COM-yy) las usa después.

- **`cotizar(concepto, unidades, key_mode)`** (`core/billing/creditos.py`): créditos enteros, hacia
  arriba. Con llave propia solo el fee (1,0 por foto); con la de la plataforma, el costo de referencia
  `BILLING_VISION_COSTO_REFERENCIA_USD` (0,003 USD por defecto) más margen y fee.
- **`alcanza(session, tenant_id, créditos)`**: toma `pg_advisory_xact_lock` por negocio, mide el crédito
  del mes (misma ventana y límite que `/admin/plan`) y, si no alcanza, levanta
  `QuotaExceeded("creditos")`, que `plan_limit_error` convierte en 409 `plan_limit_creditos` con
  `{metric, used, limit}`. Sin suscripción o sin crédito en el plan no limita. Interruptor
  `BILLING_BLOQUEAR_POR_CREDITOS`, **encendido** por defecto (apagado solo mide). Sin reserva (el
  ledger es append-only): dos peticiones concurrentes pueden pasarse en una foto, aceptado.
- **`imagen_ya_cobrada(session, tenant_id, image_key)`**: una foto ya registrada (`kind = 'vision'`,
  `result->>'image_key'`) no se cobra dos veces.
- **Desglose por `kind`:** `medir_creditos` suma las filas `vision` del ledger a «Imágenes» en vez de
  «IA». `by_concept` de `GET /plan` mantiene sus cuatro claves.
- **Migración 0129:** el CHECK `ck_usage_kind` admite `'vision'` y `'stt'`. Antes no las admitía y
  las filas en sombra de imágenes y notas de voz de la ingesta de WhatsApp (`credits = 0`) fallaban
  en silencio; desde aquí se registran. Ambas cuentan como gasto pero no como turno (`NO_SON_TURNOS`).
- **Despliegue:** migración 0129 (CHECK `NOT VALID` + `VALIDATE`, sin bloquear escrituras). Ninguna
  ruta usa todavía `alcanza`, así que el bloqueo no cambia nada hasta el endpoint de fotos.

### Flujo de atención por agente (2026-11-14)

- `agent_drafts.flow` y `agent_versions.flow` (migración `0133`) con `flow_hash`: el flujo
  deja de ser solo del negocio y se versiona con el agente (ADR-0195, SPEC-46, HU-AOS-74).
- `GET/PUT /admin/agents/{id}/flow` y `GET .../flow/options`: validan secciones y acciones
  del agente; la semilla sale de los `flow.json` de sus packs.
- Publicar congela el flujo y su hash con la versión; las versiones anteriores quedan con
  `flow` NULL y siguen leyendo `flow_tenant`.
- Pendiente en HU-AOS-75: la release y el runtime todavía compilan `flow_tenant`.

### Flujo de atención por agente en la release (2026-11-14)

- El manifiesto congela el flujo de cada agente (`flow` + `flow_hash`) y la release lo compila
  por agente; `flow_tenant` queda para las releases anteriores (ADR-0195, HU-AOS-75).
- `resolve_flow` acepta `flow_agente`: compone core + las secciones del agente, sin volver a
  mezclar los defaults de sus packs.
- Los procedimientos abiertos de un agente se derivan de su flujo; un agente sin flujo propio
  sigue con los del negocio.
- Nota: los gates y veredictos de release siguen leyendo el `flow_tenant` global; se revisará
  cuando termine el retiro del General/legacy.
- La semilla del flujo no se crea sola al publicar: mientras el agente no tenga flujo propio se
  sigue leyendo `flow_tenant` (conducta anterior y evals). El panel la guarda cuando el dueño
  confirma el paso «Flujo de atención».

### Datos del cliente por agente (2026-11-14)

- Propone el ADR-0198 y la HU-CRM-28 (SPEC-33): la política de datos que pide el cliente deja de ser del negocio y pasa a `agents.data_policy` de cada especialista. `AccionDatos.tools` relaciona cada operación con las acciones del agente; el editor del agente gana el paso «Datos del cliente» con la matriz editable y los requisitos de los superpoderes en solo lectura. El runtime resuelve por `ToolContext.agent_id` o el especialista en foco. Los métodos de entrega (`domicilio`/`recoger_en_tienda`) se habilitan por agente; `crear_pedido` y el resolver del procedimiento rechazan los no habilitados y ofrecen los disponibles (pack_commerce 3.3.1, procedimiento `tomar_pedido` 1.2.0). `scripts/migrar_datos_cliente_agentes.py` copia la política global a cada agente y la elimina del `behavior_config`; el aviso de tratamiento de datos sigue siendo del negocio. Enmienda ADR-0179.

### Remitentes del sistema en inglés (2026-11-14)

- D-73 de solvershub-docs: `docs/despliegue-coolify.md` usa `notifications@solvershub.site` (producción) y `notifications@test.solvershub.site` (test) como `SYSTEM_SMTP_FROM`.

### Check `base-segura` en vez de exigir la rama al día (2026-11-14)

- Nuevo workflow `.github/workflows/base-segura.yml`: publica el estado `base-segura` en
  cada PR. Si el PR va atrasado respecto a su base, solo pide actualizarlo cuando la base
  cambió algo que se cruza con él (mismos archivos, migraciones de Alembic en la misma
  cadena, workflows, `uv.lock` o `pyproject.toml`). Si no, deja mergear sin re-correr el CI.
- Corre en el runner `livianos` (aparte del CI pesado) y se re-evalúa en cada push a
  `develop` y `main`.

### El motor de procedimientos y el ruteo ya no son banderas por negocio (HU-AOS-69, 2026-11-13)

Decisión del dueño (2026-11-13): el motor de procedimientos queda siempre encendido y el ruteo entre
especialistas siempre en `active`. Primera fase del retiro de `tenants.aos_procedures` y
`tenants.aos_routing`: el código deja de leerlas y escribirlas; las columnas, su CHECK, la función
`aos_fijar_banderas` y el guardia `tenants_aos_banderas_guard` salen en la migración siguiente.

- **Turno:** el motor corre en todo turno con negocio (sale su modo sombra, `procedures/sombra.py`) y
  el router enruta siempre; solo baja a sombra si la release lo pide (`routing.shadowMode` del
  KernelSpec, que se queda). `oferta.motor_activo()` ya no lee la base: las tools `sin_motor_activo`
  (`sub_cambiar_plan`, `cob_registrar_promesa_pago`) dejan de ofrecerse y las `solo_con_motor_activo`
  se ofrecen en todo negocio, como ya pasaba con la bandera encendida.
- **Cron de esperas vencidas:** recorre los negocios no borrados (`banderas.negocios_vivos`), también
  los suspendidos.
- **API (incompatible):** salen `GET`/`PUT /admin/platform/tenants/{id}/agent-os` y
  `POST .../agent-os/simular` con sus schemas `AgentOS*`, y `ConnectionsConsole.routing`. El catálogo
  de configuración pierde las opciones `aos_procedures` y `aos_routing`. `RoutePreview.mode` vale
  `active`, o `shadow` si la release pide sombra.
- **CLI:** sale `agent-core db ruteo`. `db reporte-ruteo` sigue con sus dos fuentes (golden contra la
  release activa y tráfico real) y ya no imprime banderas.
- **Centro de pruebas:** «Preparar mis datos» ya no enciende banderas (no hay nada que encender).
- **Evals:** sale `--runtime sin-motor|v3` de `evals/journeys.py` (ya no hay línea base sin el motor),
  `motor_de_procedimientos` y `requiere_motor` de los sets, y el input `runtime` de
  `evals-procedimientos.yml`. Los sets que corrían sin el motor ahora lo traen: sus casetes de replay
  se regraban.
- **Despliegue:** sin migración. Los negocios que tenían alguna bandera apagada pasan a la conducta
  con todo encendido al desplegar.

### El runtime deja de ser bandera: nadie lee `tenants.aos_runtime` (HU-AOS-69, 2026-11-13)

Todo negocio corre en el Agent OS desde ADR-0191. Este cambio quita las lecturas de la columna; la
columna se borra en el PR siguiente (migración 0127), cuando este código ya esté desplegado.

- **API (quita campos de respuesta):** sale `runtime` de `GET /admin/dashboard/agents` (`ResumenPorAgente`) y de la consola de
  conexiones (`ConnectionsConsole`); sale `aos_runtime` del catálogo de configuración (`Catalogo`).
  En el panel master, `AgentOSBanderas`/`AgentOSEstado`/`AgentOSResultado` pierden `runtime` y
  `aos_connection_routing`, y `AgentOSObjetivo` pierde `runtime` y `construir` (si un panel viejo los
  manda, se ignoran). El panel y la APK dejan de leerlos en sus propios PRs, que van primero.
- **APK instalada:** `AgentsOverview.runtime` se queda, deprecado y siempre `"v3"`: lo lee el JS
  de la APK anterior a apk#250 y los binarios que ya no reciben OTA dependen de él.
- **Configuración:** sale la opción del master «Motor de conversación» (`aos_runtime`) y la
  dependencia de las demás banderas del Agent OS sobre ella. El asistente siempre omite las preguntas
  que solo cambian la voz (la voz sale de la versión publicada del agente).
- **Kernel:** el turno, el motor de procedimientos, el router, la consola, el canary (`requiere_v3`
  ya no existe), el contract (`negocio en legacy`), los presets v2 (`banderas_apagadas` ya no
  existe), «Preparar mis datos», los escenarios de QA, los journeys y el gate dejan de mirar el
  runtime. `negocios_v3` pasa a `negocios_activos`.
- Sin migración en este PR: `aos_fijar_banderas` sigue recibiendo `'v3'` fijo hasta 0127.

### Salen las columnas `tenants.aos_procedures` y `tenants.aos_routing` (HU-AOS-69, migración 0128, 2026-11-13)

- **Migración 0128:** borra el trigger `tenants_aos_banderas_guard` y su función, la función
  `aos_fijar_banderas` (firma de 0127), el CHECK `ck_tenants_aos_routing` y las dos columnas. Si queda
  algún negocio vivo sin todo encendido, avisa con un WARNING (no aborta). Las filas `agent_os.banderas`
  de `audit_log` se quedan como historia.
- **Downgrade:** repone las columnas con `true`/`'active'` para los negocios existentes (la conducta de
  hoy) y `false`/`'off'` por defecto para los nuevos, su CHECK y el texto de 0127 de la función y del
  guardia.
- **Despliegue:** después del PR que dejó de leer las columnas (ya desplegado), las tres apps (API,
  worker y beat) con la misma imagen.

### Sale la columna `tenants.aos_runtime` (HU-AOS-69, migración 0127, 2026-11-13)

- **Migración 0127:** quita los CHECK `ck_tenants_aos_runtime`, `ck_tenants_aos_order` y
  `ck_tenants_aos_routing_order`; `aos_fijar_banderas` pasa a 5 argumentos (sin `p_runtime`) y su
  auditoría deja de llevar `runtime`; el guardia `tenants_aos_banderas_guard` mira solo
  `aos_procedures` y `aos_routing`; y borra la columna. Si queda algún negocio fuera de `'v3'`, avisa con un WARNING (no aborta: una base nueva trae `tenant-1` en `'legacy'`).
  El downgrade la repone en `'v3'` con sus CHECK y el texto de 0126.
- **Código:** `fijar_banderas` llama a la firma nueva; sale el código `orden` de `BanderasRechazadas`.
- **Despliegue:** migración y código juntos, las tres apps (API, worker y beat) con la misma imagen y
  después del PR que dejó de leer la columna.

### ADR-0193: los leads viven en el core (2026-11-13)

- ADR-0193 (propuesto): los leads, sus tools de Captar leads, los crons de seguimiento y el intake público pasan al core, para que un negocio capte leads sin comercio (HU-AOS-70, D-70). Enmienda ADR-0190, ADR-0022 y ADR-0057.

### ADR-0193 aceptado (2026-11-13)

- ADR-0193 (los leads viven en el core) pasa a aceptado por decisión del dueño; HU-AOS-70 puede empezar.

### El tope de agentes también bloquea la creación del borrador (2026-11-12)

- **Planes:** al llegar al tope de agentes publicados del plan, `POST /admin/agent-templates/{key}/agents`
  y `POST /admin/agents/from-script` responden `409 plan_limit_agentes` sin crear el agente ni habilitar
  habilidades. Antes solo se bloqueaba al publicar.
- **API:** `GET /admin/agents/overview` trae `cuota_agentes` (`used`, `limit`, `allowed`) para que el panel
  y la APK avisen antes de abrir el asistente, sin exigir el permiso de Plan.
- Sin migración: el tope sigue contando agentes publicados con instancia; el General y el Coordinador no cuentan.

### Retiro del camino legacy, primera tanda (HU-AOS-69, ADR-0191, 2026-11-12)

- `AOS_CONNECTION_ROUTING` deja de existir: el ruteo por (conexión, cliente) de ADR-0115 es siempre. El consumer siempre busca la conversación por conexión y asegura la conexión del número; el alta, los presets v2, los datos de QA, el contract y `migrar-a-v3` ya no la exigen. Si la variable quedó cargada en Coolify no hace nada.
- Las claves reservadas de los agentes pasan a `core/agents/claves.py` y los procedimientos publicados del negocio a `core/agents/procedimientos_de_negocio.py`, fuera del lector tolerante, que se retira en la siguiente tanda.
- La API de plataforma sigue respondiendo `aos_connection_routing: true` hasta que se retire el campo junto con `runtime`.

### Segunda tanda
- Los negocios nacen en `aos_runtime = 'v3'` (migración 0126: valor por defecto, guardia de 0097 y `aos_fijar_banderas`, que ya no acepta `legacy`). El alta deja de hacer el paso «nacer en legacy y migrar».
- Sin vuelta a legacy: se retiran `agent-core db migrar-a-v3 --volver` y `--apagar-procedimientos`; el rollback es la release anterior de v3 o el respaldo.
- Sin el 403 `aos_runtime_legacy` en agentes, instancias por conexión, releases, habilidades por conexión y A2A.
- Antes de desplegar: confirmar que ningún negocio vivo sigue en `legacy` (`select slug from tenants where aos_runtime = 'legacy' and deleted_at is null`).

### Tercera tanda
- Release 1 al alta de la conexión (ADR-0191 §2): cuando una conexión conversacional nueva o revivida recibe sus instancias base, la misma transacción deja `connection.release_1` en el outbox y el handler del core construye y promueve su release 1 después del commit (idempotente; un error reintenta con el backoff del outbox).
- Sin release no hay turno (ADR-0191 §1): un negocio v3 sin release activa ya no responde con el AgentSpec v2, tampoco en la conexión interna del Centro de pruebas. El playground, sin conexión elegida, prueba un negocio v3 por su conexión conversacional con release activa (la principal primero).
- Configuración deja de editar agentes (decisión del dueño del 2026-11-13, ADR-0191 §4): en un negocio v3 el build ya no refresca el General desde el AgentSpec v2. Identidad, Voz y Cerebro quedan como datos del negocio para el contenido inicial de un agente nuevo; cada agente se edita en su ficha.
- Hacia el retiro del General (ADR-0192): el build falla `sin_especialista` si la conexión no tiene un especialista habilitado por defecto; el primer especialista habilitado que se agrega a una conexión sin agente por defecto queda por defecto; la release 1 se construye para toda conexión sin release activa (también la que solo tiene candidatas).

### Retiro del General, primera parte (HU-AOS-68, ADR-0192)
- El alta crea solo el Coordinador (`core/agents/base.py`: `KernelSpec` por defecto con la voz y el presupuesto del negocio, sin lector tolerante), antes de aplicar el preset. Sin General ni bandera de recorte.
- El primer especialista nace con la primera conexión conversacional (D-35): el handler de `connection.release_1` aplica los agentes del preset del negocio y construye la release 1; sin especialista posible no reintenta.
- Los especialistas del preset y del editor parten de la configuración del negocio (`contenido_base`) y nacen con los bindings de todas sus tools propias (su pack y las esenciales del core).
- La release 1 de un negocio v3 congela las versiones nativas de sus agentes, sin refrescar desde el AgentSpec v2.
- El especialista por defecto cuenta en el cupo `agentes` del plan (decisión del dueño, ADR-0192 §7): sale la excepción del General en la métrica y en `agregar_instancia`. Se retiran el recorte del General (`core/agents/coordinacion.py`, `agent-core db recorte-general`) y el acople de especialistas al General en el editor; `general_updated` de la publicación queda siempre en `null` hasta retirar el campo.

### Cuarta tanda: fuera el lector tolerante
- Se borran `core/agents/tolerante.py`, `core/agents/backfill_agentes.py` y `core/releases/migracion_v3.py`, con los comandos `agent-core db backfill-agentes` y `agent-core db migrar-a-v3`. El build y la release 1 ya no refrescan nada desde el AgentSpec v2 (`refrescar_desde_v2` y el parámetro `refrescar` salen).
- Una conexión conversacional nueva de un negocio que ya tiene agentes nace con el Coordinador y el especialista por defecto del negocio (el que es el por defecto en sus otras conexiones), en la misma transacción del alta; sin ninguno, queda pendiente y el preset se lo da por `connection.release_1`.
- El panel del master ya no corre `migrar-a-v3`: el runtime es siempre v3, `construir` no tiene efecto y pedir `legacy` es 422. «Preparar mis datos» asegura el Coordinador y sus instancias en lugar de los backfills.
- La exención de gate `migrar_a_v3` sale (no queda quien la use); `migrar_a_v3` sigue como motivo en el historial de eventos.

### Quinta tanda: la API sin el General (HU-AOS-68, ADR-0192 §5–6)
- «Mi agente y su data» y el Resumen por agente ya no tienen un General virtual: el `ref` es siempre el id del agente (el literal `general` es 404) y lo no atribuido va al especialista por defecto del negocio (`incluye_sin_atribuir` lo marca). Los módulos que ningún agente cubre salen en el campo nuevo `general.modulos_sin_agente` del Resumen (aditivo). Un negocio sin especialistas solo tiene el bloque del negocio.
- `general` deja de ser clave reservada: solo `coordinador` lo es. Sale `general_updated` de la respuesta de publicar del editor.

### Habilidades por agente y cuentas Wompi (2026-11-12)

- Habilidades instaladas por agente (ADR-0190, HU-AOS-61): `pack_installations` acepta `scope='agent'` con `agent_id` (migración 0125), `core/agents/instalaciones_agente.py` instala, quita y configura (cifrado con AAD por instalación), y `plugin_manager.load_config(pack, agent_id=…)` combina la config del negocio con la del agente.
- `BaseModule.tipo` (`especialidad` | `transversal`) y `BaseModule.config_agente`; `config_de_agente()` arma el esquema del agente desde el del pack. Ventas, Agenda y Suscripciones declaran los campos que son de cada agente.
- La release congela en cada componente las habilidades activas del agente (`skills`, solo ids) y el turno v3 ofrece solo las acciones de esas habilidades (HU-AOS-63). Un agente sin instalaciones propias hereda las de la release hasta la resiembra (HU-AOS-65).
- Crear un agente desde una plantilla instala sus habilidades en ese agente (ADR-0190 reemplaza ADR-0173).
- Cuentas Wompi del negocio (`pack_payments_accounts`, migración `pack_payments_0004`, HU-AOS-62): cada agente con Pagos en línea elige una en su instalación (`cuenta_id`); `create_link(..., agent_id=…)` cobra con esa cuenta y la transacción guarda `cuenta_id` y `agent_id`. Webhook nuevo por cuenta `POST /webhooks/payments/wompi/{slug}/{cuenta_id}`: valida con el secreto de esa cuenta y no confirma links de otra. Rotar llaves no pide publicar.
- `pack_subscriptions_0014`: la suscripción guarda el agente que la vendió; cargos, recordatorios y reenvíos cobran con su cuenta. Comercio pasa el agente del turno al pedir el link.
- API del agente (HU-AOS-64): `GET /api/v1/admin/agents/{id}/actions` (acciones de sus habilidades agrupadas por lo que hacen, con su binding del borrador), `GET /api/v1/admin/agents/{id}/skills` y `PUT /api/v1/admin/agents/{id}/skills/{pack_id}` (instalar, quitar y configurar Pagos en línea con `cuenta_id` y Conocimiento; sin secretos en la respuesta). `BaseModule.grupos_de_acciones` en Comercio, Agenda y Cobranza.
- API de cuentas Wompi para el panel: `GET/POST /api/v1/pack/payments/accounts`, `PATCH/DELETE /api/v1/pack/payments/accounts/{id}` (sección `plugins`; sin secretos en la respuesta; `events_url` por cuenta; 409 `account_in_use`). Pagos en línea se activa sin config del negocio (`requires_config = False`): las llaves van en las cuentas.
- Los presets (v2) instalan en cada agente que crean las habilidades de su plantilla y las opcionales que el negocio ya tiene (HU-AOS-65, parte 1).
- `GET /api/v1/plantillas-opciones/catalogo-acciones?agent_id=…` (HU-AOS-67): el catálogo y el lint de las opciones salen de las habilidades y los bindings del agente que atiende la conexión. Activar un pack sin config obligatoria del negocio (`requires_config = False`) ya no pide la fila de config.

### Backfill de habilidades por agente (2026-11-12)

- `agent-core db habilidades-por-agente --todos|--tenant <slug> [--aplicar]` (HU-AOS-65, ADR-0190): instala en cada agente especialista existente las habilidades de su plantilla, sin borrar ningún negocio. Idempotente; dry-run por defecto.

### Backfill de habilidades: Conocimiento (2026-11-12)

- `agent-core db habilidades-por-agente` instala también Conocimiento en los agentes que ya usan `buscar_conocimiento` (borrador o alguna versión) si el negocio lo tiene: no lo pierden en su próxima release.

### Aviso permanente de plan Free (2026-11-12)

- HU-BILL-15. `GET /api/v1/admin/me` incluye `plan` con `plan_id`, `plan_name`, `status`, `days_left`, `trial_ends_at` y `has_plan`. Sale de la suscripción activa del negocio, sin consultar consumo ni estado operativo, para que el panel y la app muestren el recordatorio de upgrade sin otra petición.
- Nuevo `core.billing.enforcement.plan_summary()`: lectura barata y sin efectos secundarios de `tenant_subscriptions` + catálogo de planes.
- `docs/openapi.json` regenerado con `MePlanSummary`.

### ADR-0191 y ADR-0192: un solo motor y sin General (2026-11-12)

- ADR-0191 (propuesto): un solo motor. Todo turno corre sobre la release v3 de su conexión y sin release falla cerrado; release 1 al alta de la conexión; el Centro de pruebas prueba por una conexión real; se retiran el runtime v2, el lector tolerante, `refrescar_desde_v2`, el 403 `aos_runtime_legacy`, `migrar-a-v3 --volver` y `AOS_CONNECTION_ROUTING` (HU-AOS-69, D-69).
- ADR-0192 (propuesto): sin General. El Coordinador enruta a un especialista por defecto de cada conexión (HU-AOS-68, D-69).

### Acciones concedidas por un superpoder (2026-11-12)

- `GET /api/v1/admin/agents/{id}/actions` suma `by_superpower`: la acción la concede un superpoder que el agente declara (D-67), así que corre aunque no tenga binding. Campo nuevo, compatible hacia atrás. Sin esto el panel y la APK mostraban «Apagada» acciones que el agente sí usa (p. ej. `crear_pedido` con «tomar pedido»).

### En v3 solo corre lo que concede la versión del agente (2026-11-10)

- HU-ARCH-34, parte 2. En un negocio v3, una acción sin binding, sin declarativa y sin procedimiento declarado que la incluya se niega y deja el evento `tool_sin_binding`. Tampoco se ofrece.
- Sin agente en foco se niega todo: un negocio v3 sin release, sin conexión o con release sin foco.
- Legacy, la bandera `AOS_CONNECTION_ROUTING` apagada y el Centro de pruebas siguen abiertos, con el evento `tool_sin_binding_legacy`.
- Antes de desplegar: `agent-core db reporte-bindings` en 0 y pasado `episode.idleMinutes`.

### Número visible del WhatsApp oficial en la consola de conexiones (2026-11-10)

- `GET /api/v1/admin/connections/console` suma `display_phone_number` por conexión: el número de WhatsApp como lo sincronizó Meta (`phone_numbers.display_phone_number`), unido por `phone_number_id = external_id`. Sin llamadas a Meta; null en los demás canales o si el número aún no se sincronizó. Campo nuevo y opcional: no rompe a los clientes.
- `external_id` documenta que en WhatsApp oficial es el phone_number_id (clave de ruteo), no el número.

### Modelo C4 canónico en Structurizr (2026-11-09)

- ADR-0189. `docs/architecture/workspace.dsl` es la fuente única de los diagramas C4: contexto, contenedores, componentes de `core-api`, `celery-worker` y packs, y despliegue por entorno.
- `docs/architecture/c4.md` pasa a ser generado con `scripts/export_c4.py`.

### Confirmación de acciones con efecto (2026-11-09)

- HU-ARCH-36. `confirmar_cotizacion`, `cob_registrar_abono`, `sub_cancelar`, `sub_cambiar_plan` y `cancel_calendar_event` piden el sí del cliente en legacy, en v3 con el motor en sombra y en v3 sin gobierno de etapa, aunque el binding no lo pida.
- Una confirmación solo vale con un mensaje del cliente posterior a la pregunta del turno anterior: el modelo ya no se confirma solo en el mismo turno.
- `confirmation_required` deja de contar como evidencia para el verificador y los guardrails, también en el camino del motor.
- Playbooks de ventas, citas y suscripciones ajustados para una sola pregunta. Suben pack_commerce 3.2.0, pack_calendar 2.4.0 y pack_subscriptions 0.6.0.

### Reporte de acciones sin conceder en v3 (2026-11-09)

- HU-ARCH-34, parte 1, sin cambio de conducta. `agent-core db reporte-bindings` lista por negocio y agente lo que una release activa o canary debe conceder y no concede.
- Los agentes nuevos desde un preset llevan binding de las acciones esenciales del core.
- Enmiendas de ADR-0019 y ADR-0118: en v3 una acción sin concesión no corre, y declarar un procedimiento concede sus acciones.

### Los PR a `develop` no pueden romper el contrato de la API sin avisar (2026-11-09)

- **CI compara `docs/openapi.json` del PR contra `develop`** con `oasdiff` y falla si hay un cambio incompatible en `/api/v1`. Antes solo se comprobaba hacia `main`, y la APK instalada no se actualiza con el backend.
- **Un valor nuevo en un enum de respuesta solo avisa:** es la forma habitual de ampliar esos campos y no bloquea el PR.
- **Cambio incompatible intencional:** etiquetar el PR con `api-incompatible` y relanzar el job.

### CI: modelo C4 sin drift (2026-11-09)

- El job `rapido` levanta `structurizr/mcp` en versión fija, valida `docs/architecture/workspace.dsl` y falla si `docs/architecture/c4.md` no coincide con lo que produce `scripts/export_c4.py`. Las PRs que no tocan el modelo se saltan el paso.
- `scripts/export_c4.py` ahora sí falla cuando el DSL es inválido: la herramienta `validate` devuelve el error como texto y el script lo ignoraba.

### ADR-0189 aceptado (2026-11-09)

- El modelo C4 canónico en `docs/architecture/workspace.dsl` pasa de propuesto a aceptado, también en el índice de ADR.

### La skill `abrir-pr` queda versionada y su verificador es obligatorio (2026-11-09)

- **`abrir-pr` y la plantilla de PR entran al repo**: antes solo existían en un checkout, así que los worktrees no las tenían. Claude ya puede usar la skill cuando se le pide abrir un PR; el OK antes de publicar se mantiene.
- **`gh pr create` hacia `develop` corre `verificar_pr.py`** desde el hook `guards.py` y se bloquea si hay errores: falta el fragmento de changelog, chocan números de migración o ADR, hay más de una cabeza de Alembic o el árbol no está limpio. Los PR a `main`, los apilados y los de otros repos no se revisan.

### Envío a segmentos por WhatsApp vía Twilio (PR-7b, ADR-0186) (2026-11-08)

- **Envío de una plantilla a un segmento** (HU-CRM-06, ADR-0150): ahora también sale por una conexión de Twilio.
  - `GET /admin/leads/acciones/plantilla/conexiones` lista las conexiones de Twilio con sus plantillas `MARKETING` aprobadas y de texto.
  - La vista previa, la confirmación y los lotes son los mismos que en Meta: mismo tope, consentimiento, 7 días entre promociones e idempotencia por destinatario.
  - Cada mensaje lleva el `ContentSid` y las variables; al enviar se revalida en la conexión actual, como en el chat.
  - Sin migración: `segment_sends.template_id` apunta a `message_templates` o a `twilio_content_templates` según el proveedor de la conexión del envío.
- **Cliente que cambia de número** (también en Meta): si la conversación pasó a otra conexión después de la vista previa, ese destinatario se omite con el motivo `otra_conexion`. La promoción no sale por un número que el dueño no eligió.
- **Pausa**: una conexión de Twilio pausada no arma el envío (409 `connection_paused`), y su motivo aparece en la lista de conexiones.
- **Baja por el proveedor**: un error `OPT_OUT` de Twilio (21610) en un mensaje de segmento da de baja al cliente una sola vez, con origen `twilio_21610`.
  - No escala la conversación, igual que el 131050 de Meta.
  - Esa baja no se deshace desde la ficha (409 `baja_del_cliente`).
  - El 63049 de Twilio no da de baja: lo decide Meta por envío, no el cliente.
- Fuera de eso, el camino de Meta no cambia: `_baja_por_meta` solo se generaliza a `_baja_por_proveedor`.
- **Límite**: la baja se registra cuando el 21610 llega como error al crear el mensaje. Si llegara por el status callback no se registra, igual que el 131050 de Meta por webhook.
- **Panel**: `consentimiento.tsx` todavía no tiene etiqueta para `twilio_21610`; va con el panel P-3.
- **Sin límite diario en la vista previa para Twilio**: `limite_diario` sale de `phone_numbers`, que no tiene el número. Solo es informativo.

### Plantillas de Twilio en el chat y en los avisos por evento (PR-7a, ADR-0186) (2026-11-08)

- **Selector del chat** (`GET /admin/conversations/{id}/templates` y `POST .../template-messages`): en una conversación de Twilio lista las plantillas aprobadas de su conexión y encola la elegida. Las variables son `{{1}}`, `{{2}}`… y se exigen todas.
- **Avisos por evento** (ADR-0108): con la ventana cerrada, una conversación de Twilio usa la plantilla aprobada de su conexión con los valores del servidor (`resolve_values`). Los resultados son los mismos que en Meta: `plantilla`, `sin_plantilla` o `duplicado`.
- **Mapa de avisos** (`/admin/settings/message-templates`): lista y acepta plantillas de Twilio y valida sus variables.
- **Mensaje encolado**:
  - `template_name` lleva el nombre legible, que usan el panel, la línea de tiempo y el crédito.
  - `template_components` lleva un único componente `twilio_content` con el `ContentSid` y las variables.
  - `_send_one` lo envía por `send_content_template`.
- **Errores de envío de plantillas**: una plantilla que el canal no puede enviar (`ChannelError`) ahora es **permanente**, también en Meta. Antes caía en el `except` genérico y se reintentaba como si fuera un error de red.
  - **Cambio en Meta**: una plantilla en un canal sin plantillas, o en una conexión de Meta sin `phone_number_id` o `access_token`, antes era TRANSIENT, tenía 3 reintentos y terminaba en la DLQ. Ahora falla al primer intento, sin DLQ, y escala igual. Lo fija `test_canal_sin_plantillas_es_permanente_tambien_en_meta`.
- **Al enviar se revalida la plantilla** (`plantillas_en_uso.enviable`): el `ContentSid` tiene que seguir vivo, aprobado y de texto en la conexión **actual** de la conversación. Si no, el error es permanente. Así se cubren dos casos:
  - El cliente escribió por otro número de Twilio del negocio después de encolar; sin la comprobación, saldría el HX de una conexión por el número de otra.
  - La plantilla se pausó o se borró mientras el mensaje esperaba en la cola.
- **Mapa de avisos**: si el mismo nombre existe en varias conexiones de Twilio, todas tienen que pedir las mismas variables; si no, responde 400. Las listas se ordenan también por conexión, para que el resultado sea siempre el mismo.
- `{{n}}` acepta como máximo 3 dígitos. Un número desmedido en una plantilla sincronizada ya no rompe la lista con un 500.
- **Capacidades**: `whatsapp_twilio` pasa a tener plantilla aprobada y ventana de 24 h. Sigue fuera del puente de avisos con opciones hasta que lleguen botones y listas (PR-7i).
- El camino de Meta (`approved_template_for`, `build_components`) no cambia.
- **Auditoría multitenant** (sin hallazgos altos):
  - Las variables de las plantillas de Twilio se limpian con `clean_param`, como en Meta, y la vista previa usa los mismos valores.
  - Solo se usan y se importan plantillas **de texto**. Una con botones, tarjeta o media enviaría algo distinto de lo que se muestra; quedan para el PR-7i.
  - Una plantilla de Twilio que no se puede enviar se registra como error de Twilio y no de Meta.
  - Nuevo test con dos conexiones de Twilio en el mismo negocio.

### Observabilidad B6: Sentry en el conector de WhatsApp Web (2026-11-08)

- `core/waweb/connector.py:main()` llama a `configure_sentry()` (no-op sin `SENTRY_ENABLED`/`SENTRY_DSN`), con el mismo scrubber que la API y el worker, y marca los eventos con `role=connector`. Antes, los errores del conector solo quedaban en el log del contenedor.
- El scrubber de Sentry también tapa las variables locales del conector que llevan datos del cliente o el código de vinculación: `event`, `qr`, `job`, `text`, `sender`, `jid`, `to`, `raw`.

### Observabilidad B5: `X-Request-ID` y `trace_id` en los logs fuera del turno (ADR-0188) (2026-11-08)

- Middleware ASGI `RequestIdMiddleware`: cada petición lleva un id (el del cliente si es seguro, `[A-Za-z0-9._-]{1,64}`, o uno nuevo), que vuelve en la cabecera `X-Request-ID` y va como `request_id` en todos los logs de la petición.
- Procesador `con_trace_id` en structlog: agrega el `trace_id` del span vigente cuando el evento no trae uno (dentro del turno manda el del Coordinador). Sirve para saltar de una línea de Loki/Kibana a la traza en Tempo desde peticiones HTTP y tareas de Celery.
- Los registros de `logging` (httpx, SQLAlchemy, Celery) también llevan `request_id` y `trace_id` (`foreign_pre_chain`).

### Observabilidad B4: `GET /healthz/deep` (ADR-0188) (2026-11-08)

- Ruta nueva y pública (prefijo `/healthz`): `SELECT 1` y `PING` a Redis con tope de 1 s, y la edad del latido `worker:heartbeat` (fallo si pasa de 180 s o no existe). Responde 200 o 503 con `{status, componentes: {db, redis, worker}}` y solo `ok`/`fallo`, sin DSN ni mensajes. El resultado se guarda 5 s (también el 503) y las peticiones simultáneas comparten una medición.
- `/healthz` no cambia: lo siguen usando los health checks de Coolify y de compose.

### Observabilidad B3: métricas propias y sondas de la plataforma (ADR-0188) (2026-11-08)

- `core/observability/metricas_negocio.py`: `agentcore_llm_tokens_total{model,kind}`, `agentcore_llm_cost_usd_total{model}`, `agentcore_llm_calls_total{model,outcome}` (respuesta y fallo del LLM en el loop), `agentcore_turn_duration_seconds{channel,outcome}` (cierre del turno del Coordinador), `agentcore_celery_tasks_total{task,status}` y `agentcore_celery_task_duration_seconds` (sin `task`), `agentcore_graph_api_errors_total{category}` (`graph_fetch`) y `agentcore_http_server_duration_seconds{route_group,status_class}` (middleware de la API, sin `/healthz*`).
- `core/observability/sondas.py`, solo con `AGENT_CORE_ROLE=api` y `OTEL_METRICS_ENABLED=true`: una tarea cada 30 s llena los valores que leen los gauges `agentcore_dlq_length`, `agentcore_inbound_pending`, `agentcore_worker_heartbeat_age_seconds` (ausente = 9999), `agentcore_dependency_up{component}` y `agentcore_cron_failures_24h{job}` (`crons_service.fallos_24h`).
- Etiquetas de conjuntos cerrados (un valor raro pasa a `otro`); nunca `tenant_id`, `agent_id`, `conversation_id`, teléfonos ni rutas sin agrupar. Histogramas con buckets explícitos y cortos. Registrar una métrica nunca rompe el camino que la registra.
- `Settings.agent_core_role` lee `AGENT_CORE_ROLE` (la misma variable de los entrypoints; por defecto `api`).

### Observabilidad B2: autoinstrumentación de FastAPI, SQLAlchemy y httpx (ADR-0188) (2026-11-08)

- Con `OTEL_ENABLED=true`, spans por petición HTTP (sin `/healthz*` ni los spans internos de send/receive), por sentencia SQL del engine del core y por llamada httpx de cualquier cliente del proceso.
- Redacción: la query sale de `http.url`/`url.full` y `url.query` se tapa, tanto en el servidor (el GET del webhook trae `hub.verify_token`) como en httpx (tokens de Meta, `client_secret`). `db.statement` es el SQL del cursor con marcadores (`$1`), sin valores; sin sqlcommenter.
- Las tres instrumentaciones usan un `MeterProvider` no-op: sus histogramas llevan ruta, host y código, y no caben en el presupuesto de series.
- No se instrumentan Celery (ya lo propaga `celery_trazas.py`) ni Redis.
- Dependencias nuevas: `opentelemetry-instrumentation-fastapi`, `-sqlalchemy` y `-httpx` (`>=0.66b1`, la serie del SDK). SQLAlchemy se instrumenta con `skip_dep_check`: la 0.66 declara `sqlalchemy < 2.1` y la imagen instala 2.1.
- Errores sin datos en los spans: `ExportadorRedactado` deja solo el código del status y el tipo de la excepción (el instrumentador de SQLAlchemy ponía `str(exc)`, con el `DETAIL` de asyncpg; FastAPI registraba el mensaje). El token de `GET /api/v1/files/{token}` se tapa en `http.target`/`url.path`/`http.url`. El engine del core usa `hide_parameters=True`: los errores de SQL ya no llevan `[parameters: …]` en logs, spans ni Sentry.

### Observabilidad B1: proveedor de métricas OTLP (ADR-0188) (2026-11-08)

- `core/observability/metricas.py`: `configure_metrics()` fija un `MeterProvider` con exportador OTLP/HTTP cada 60 s, temporalidad acumulada y recurso `service.name` / `service.instance.id = <host>-<pid>` / `OTEL_RESOURCE_ATTRIBUTES`. Se llama en el lifespan de la API, en cada hijo del prefork del worker (`worker_process_init`, no en el padre), en `beat_init` y en `main()` del conector; `shutdown_metrics()` exporta lo pendiente al salir.
- Con eso salen los contadores que ya existían (`option_*`, `event_notice_omitted_total`…).
- Variables nuevas: `OTEL_METRICS_ENABLED` (por defecto `false` = no-op, ni se importa el SDK) y `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` (URL completa, `http://alloy:4318/v1/metrics`).
- Si el SDK de métricas no carga (un Python sin `_ctypes`, como el del runner de CI), el proceso arranca igual con un warning. Las pruebas del SDK real se saltan en ese Python.

### Aislamiento entre gestores, sesión y limpieza de la API de campo (2026-11-08)

- agent-core#705, puntos 2 a 4. La cola offline deduplica por cuenta: un gestor que repite el id de evento de otro ya no recibe «duplicado» (migración `pack_cobranza_0019`; los eventos anteriores siguen contando como aplicados para no cobrar dos veces).
- Un préstamo borrado, liquidado o cancelado no admite visitas, y lo borrado (cuota, pago, abono, cliente) da 404 en vez de volver a operarse. Las cuotas de suscripción (sin préstamo) se editan, eliminan y cobran desde su cartera; el link lleva el cliente para que el pago se pueda registrar.
- Un gestor desactivado o borrado pierde la sesión abierta en el siguiente pedido (401), sin esperar a que venza el token.
- La API ya no devuelve la ruta del archivo en el servidor (`carteraArchivo`), `ultimaVisita` del detalle de la cuota ahora se llena, y el permiso se comprueba antes que la cartera en todos los eventos de la cola.

### Botones y listas por WhatsApp vía Twilio (PR-7i, ADR-0186 §9) (2026-11-08)

- **Capacidades**: `whatsapp_twilio` admite botones y lista dentro de la ventana de 24 h, igual que Meta. Las opciones directas (SPEC-40) y los botones del agente (HU-AOS-09) salen como interactivo en una conversación de Twilio con la ventana abierta.
- **Envío** (`core/gateway/twilio/interactivos.py`): el interactivo del kernel se convierte en un Content de Twilio de su **forma**.
  - Las formas son `quick_reply:N` (`twilio/quick-reply`) y `list:N` (`twilio/list-picker`).
  - El cuerpo, los títulos y los ids van como `ContentVariables`. Así un solo Content por forma sirve para todos los mensajes de esa forma y no hace falta aprobación dentro de la ventana.
  - El pie se agrega al cuerpo. Una fila sin descripción lleva un guion, porque Twilio la exige.
  - Los títulos, ids y descripciones van sin saltos ni caracteres de control.
  - Un título vacío, un id de más de 200 caracteres o más de 3 botones son un `ChannelError`. El id no se trunca, porque tiene que volver igual en `ButtonPayload` o `ListId`.
  - Los ejemplos de variables del Content son neutros: el recurso queda en la cuenta del negocio y no guarda datos del primer cliente.
- **Caché** (migración **0124**): tabla `twilio_interactive_contents`.
  - Tiene `tenant_id`, RLS forzada, FK compuesta a la conexión y único por (negocio, conexión, forma) entre las filas vivas.
  - Guarda la cuenta (`account_sid`) que creó el Content. Si la conexión cambia de cuenta, la fila vieja se reemplaza. Pasa al rotar credenciales o al reconectar el número con otra cuenta, porque las dos cosas conservan la conexión. Desconectar da de baja la caché, como ya pasaba con las plantillas.
  - Si dos envíos crean a la vez el Content de la misma forma, gana el primero que se guarda y los dos usan ese. La baja tras un 404 apunta solo a ese Content y no toca uno que otro envío acaba de recrear.
  - La primera vez de una forma se crea el Content; después se reutiliza.
  - Si Twilio responde 404 al enviar, porque alguien borró el Content en la consola, la fila se marca borrada y el Content se recrea una sola vez.
  - Es reversible: el downgrade elimina la tabla, los Content quedan en Twilio y se recrean al volver.
- **Errores**:
  - **Cambio respecto de lo aprobado** en ADR-0186, decisión 5: no hay texto de respaldo en el envío, igual que en Meta. Queda para la adenda del ADR. Quien acepta degradar a texto ya lo decidió al encolar (`send_interactive_to_conversation`); las opciones directas y las confirmaciones no degradan.
  - Un interactivo que no sirve es un error **permanente** de Twilio. Los errores de Twilio siguen su categoría: un 63016 fuera de la ventana no se reintenta.
- **Fuera de la ventana**: las opciones directas no buscan la plantilla de contraparte de Meta para una conversación de Twilio, aunque conserve un `meta_connection_id` de cuando era de Meta. Por eso queda el mensaje `failed` con `sin_plantilla`. Antes salía un texto numerado, que fuera de la ventana tampoco llega.
- Los **avisos por evento con opciones** siguen sin puente para Twilio y usan su plantilla de Content (PR-7a).
- **Pendiente de verificar en test con el número real**:
  - Que Twilio acepte variables en `actions[].id` e `items[].id`.
  - Qué código devuelve al enviar con un ContentSid borrado; hoy solo un 404 o un 20404 provocan la recreación.

### La API v1 de campo aplica la matriz de permisos y las carteras del gestor (2026-11-08)

- agent-core#690. `POST /sync/eventos`, `GET /carteras/{id}/clientes-dia` y `POST /clientes/{id}/evidencia` aceptan el mismo token de cuenta que la API v2 y no aplicaban ni la matriz ni las carteras: un gestor sin `registrar_pago` registraba el pago por aquí, en cualquier cartera del negocio. Ahora aplican lo mismo que la v2, y `/sync/eventos` devuelve también `rechazados` (los eventos sin permiso no se guardan y se pueden reintentar). El propietario, el master y los dispositivos sin gestor no cambian.

### ADR-0188 y runbook de observabilidad (2026-11-08)

- `docs/adr/0188-observabilidad-metricas-trazas-y-logs.md` (propuesto): modelo de métricas (eventos en el código y sondas solo en la API), etiquetas prohibidas y presupuesto de series, trazas con redacción en origen, correlación de logs y redacción en Alloy, `/healthz/deep`, variables de entorno.
- `docs/runbooks/observabilidad.md`: encendido y verificación en test, apagado y qué hacer ante cada alerta.

### ADR-0188 aceptado y runbook de observabilidad corregido (2026-11-08)

- `docs/adr/0188-observabilidad-metricas-trazas-y-logs.md` pasa a aceptado con la verificación en test del backend `be29a373`; queda escrito lo que no se probó.
- `docs/runbooks/observabilidad.md`: la comprobación de `/healthz/deep` usa `http://127.0.0.1:3013`, porque el túnel de test no enruta esa ruta y el dominio devuelve el 404 del panel.

### Adenda del ADR-0186: lo que cambió al implementar WhatsApp vía Twilio (2026-11-08)

- Registra los cambios de decisión de la fase 1: sin texto de respaldo en los interactivos, un Content por forma en vez de por hash, auditoría en la tabla de webhooks existente y sin resolvedor común de plantillas.
- Agrega las precisiones del alta, el webhook, las plantillas y los segmentos, y lo que falta verificar en test con el número real.

### Webhook de WhatsApp vía Twilio (PR-4, ADR-0186) (2026-11-07)

- **Rutas públicas** `POST /webhooks/twilio/{public_id}/inbound` y `/status` (`core/gateway/twilio/webhook.py`):
  - El negocio sale solo de la ruta, con `twilio_route_connection`. Si no existe responde 404.
  - La firma `X-Twilio-Signature` se verifica con el Auth Token de la conexión contra la URL **registrada**. Además, el `AccountSid` debe ser el de la conexión. Si falla, 403 y alerta por firmas repetidas.
  - Una conexión pausada o un negocio no operativo reciben 200 y no se procesa nada.
  - Responde TwiML vacío (`text/xml`).
  - El cuerpo tiene un tope de 64 KiB.
- **Idempotencia** en las mismas tres capas que Meta:
  - La auditoría de webhooks guarda `meta_app_id = "twilio:<AccountSid>"` y `MessageSid` (o `MessageSid:estado`). Solo guarda campos de diagnóstico, nunca el texto del mensaje.
  - `claim_message_once`.
  - El stream.
- `publish_inbound_message`: el `xadd` al stream del core, extraído del webhook de Meta, que ahora usan los dos webhooks.
- **Estados**: `parse_status` → `apply_status_update`, la misma máquina que Meta. Un estado nunca retrocede y `undelivered` queda como `failed` con su código.
- **Ventana de 24 h**: cada entrante de Twilio la refresca, igual que en Meta.
- **Media**: `core/media/twilio_media.py` descarga `MediaUrlN` con auth básica, solo contra `api.twilio.com`. Sigue un único redirect, sin credenciales, hacia el CDN de Twilio o S3. Usa el mismo tope y la misma clasificación de errores que Meta.
- `resolve_credentials`: el respaldo a la conexión primaria ya no puede caer en una conexión de Twilio.
- **Ajuste a ADR-0186 §6**: se reutiliza la auditoría de webhooks existente en vez de crear una tabla `twilio_webhook_events`. Los SID de Twilio son únicos en todo Twilio, la FK a la conexión de Meta queda NULL y así se aprovecha la alerta de eventos atascados.
- **Pendiente**: registrar la baja por 21610 (STOP total, distinto del 131050 de Meta) llega con los envíos a segmentos (PR-7b). Los costos llegan en el PR-5.
- **Auditoría multitenant** (sin hallazgos altos):
  - El `To` firmado tiene que ser el número de la conexión. Si no, no se publica: la conversación nacería sin conexión y su respuesta podría salir por la primaria o por el número de la plataforma.
  - El cuerpo se lee por partes con tope, sin confiar en `Content-Length`.
  - La descarga de media acepta solo `MediaUrl` de un mensaje de la cuenta de la conexión (ruta anclada al `AccountSid`).
  - S3 queda acotado a `s3[.-]…amazonaws.com`.
  - Las dos respuestas se leen en streaming con tope.
- **Revisión cruzada del #717**:
  - El stream lleva el `connection_id` que el webhook ya validó y el consumer lo usa primero, filtrando por negocio y proveedor. Si se borra o pausa la conexión en medio, la conversación igual queda con su conexión y el envío falla cerrado. Un entrante de Twilio sin conexión se descarta: ya no se crea una conversación sin ruta.
  - `publish_inbound_message` libera el claim si falla el encolado, para que el reintento del proveedor no llegue como duplicado. Aplica también a Meta.
  - Con la conexión pausada, los estados de entrega de mensajes ya enviados se siguen aplicando.
  - Se aclara que la auditoría no deduplica.
  - Nuevos tests de aislamiento entre negocios:
    - una firma de A contra el `public_id` de B;
    - un estado firmado por A con un SID que solo existe en B;
    - un número que pasa de un negocio a otro.

### Test de escenarios QA: activa `pack_subscriptions` en la base recién migrada (2026-11-07)

- `test_qa_escenarios.py::test_escalacion_en_curso` fallaba en una base recién migrada: `pack_subscriptions` pide configuración y queda `detected` hasta que otro test la guarda, así que sus procedimientos (`contratar_plan`) no se registraban y la tarjeta «En curso» devolvía `missing: []` con `missing_reason: definicion_no_disponible`. Pasaba o fallaba según el orden de la suite (y el reparto entre workers de xdist). El fixture del archivo ahora guarda la configuración de la habilidad antes de activar los packs. Solo tests, sin cambios de código de producción.

### Salud, operación y costos de WhatsApp vía Twilio (PR-5, ADR-0186) (2026-11-07)

- **Salud de la conexión**:
  - Revisa la cuenta (activa), el sender (`ONLINE`) y que sus webhooks apunten a solvershub.
  - Estados posibles: `connected`, `degraded`, `revoked` o `error`. Una caída transitoria de Twilio no cambia el estado.
  - Alerta una vez al día por conexión cuando pasa a `degraded` o `revoked`.
  - Cron `core.twilio_health_check` cada 15 min, declarado en el catálogo de crons (ADR-0183).
  - Las conexiones se listan con `admin_twilio_connection_ids()` (SECURITY DEFINER, migración 0122) y cada una se lee dentro del scope de su negocio.
- **Acciones**, todas en `/api/v1/admin/connections/twilio/{id}/…` y para propietario o master:
  - `refresh`: revisa la salud ahora.
  - `webhooks`: vuelve a registrar los webhooks.
  - `test-message`: manda un texto fijo a un número. Solo funciona dentro de la ventana de 24 h; fuera de ella responde 409 `fuera_de_ventana`. Tope de 1 cada 30 s y 10 por día.
  - `PUT credentials`: rota el Auth Token o la cuenta. Se validan contra Twilio antes de guardarlos.
  - `disconnect`: pide la contraseña, borra lógicamente la conexión y su fila de Twilio (libera el número) y puede restaurar los webhooks que el sender tenía antes.
  - `GET costs`: devuelve lo que Twilio cobró por moneda.
- El borrado genérico de una conexión también borra lógicamente su fila de `twilio_connections`.
- **Costos por mensaje**:
  - Cuando llega un estado final, el webhook encola `core.twilio.costo_mensaje`. La tarea lee el `price` del recurso Message y guarda un costo por mensaje en `message_costs`, con `provider='twilio'`, el monto en positivo y la moneda.
  - Si Twilio todavía no tiene el precio, reintenta a los 5, 10 y 15 min.
  - La migración 0122 agrega `provider`, `price` y `price_unit`, más un índice único parcial para Twilio.
  - La lectura de costos de Meta ahora filtra `provider='meta'`.
  - Twilio no documenta si su precio incluye el cargo de Meta; la respuesta lo aclara.
- **Reintentos de Twilio** (*connection overrides*): las URL de webhook se registran, y el `StatusCallback` de cada mensaje se envía, con `#rc=3&rp=5xx,ct`, es decir hasta 3 reintentos ante un 5xx o un fallo de conexión. El fragmento no entra en la firma, así que la URL guardada sigue sin él.
- **Auditoría multitenant** (sin hallazgos altos):
  - `/costs` exige además la sección Costos, como el resto del dinero en el panel.
  - Rotar las credenciales de una conexión pausada responde 409 `conexion_pausada`, para no reactivarla.
  - `PUT /credentials` pide la contraseña.
  - Repetir un alta o rotar credenciales ya no pisa el `previous_webhook` original.
  - El contador diario del mensaje de prueba fija su TTL al crearse (SET NX EX).
- **Revisión cruzada del #718**:
  - Re-registrar webhooks, rotar credenciales y desconectar quedan auditados como `twilio.*`.
  - Desconectar restaura los webhooks **antes** de borrar. Si no puede restaurarlos, no borra nada: responde 502 `webhooks_no_restaurados`, salvo que se pida `continuar_si_falla`. Solo restaura si el sender sigue apuntando a solvershub.
  - Desconectar ya no exige la bandera.
  - La firma se acepta también sobre la URL con el fragmento de los overrides.
  - Rotar con webhooks ajenos pide `confirm_overwrite_webhook`.
  - El costo solo se guarda para mensajes del negocio (como Meta, ADR-0100).
  - Una revisión de salud que se cruza con una rotación se descarta: así no salta una alerta falsa de `revoked`.
  - Las URL del webhook solo las ve el propietario o el master.
  - Nuevos tests entre negocios.

### Plantillas de WhatsApp vía Twilio (PR-6, ADR-0186) (2026-11-07)

- **Tabla `twilio_content_templates`** (migración 0123):
  - Guarda las plantillas de la Content API (`HX…`) de cada conexión de Twilio, con el estado de su aprobación de WhatsApp.
  - RLS forzada y FK compuesta a la conexión.
  - Nombre único entre las vivas de la conexión.
  - Borrado lógico.
- **Crear**, en dos pasos para que un corte a mitad no duplique nada:
  - Primero la fila local en `creating` y después el Content en Twilio.
  - Si el segundo paso falla, repetir el pedido reconcilia por nombre con lo que Twilio ya tenga.
  - Antes de llamar a Twilio valida: nombre, idioma, categoría, texto de hasta 1.024 caracteres y variables `{{1}}…` contiguas, cada una con su ejemplo.
- **Aprobación**:
  - Enviar a aprobación es idempotente.
  - Twilio no avisa por webhook, así que el estado se consulta por polling con backoff: cada 5 min la primera hora, cada 30 min hasta 48 h y cada hora hasta 14 días.
  - Cron `core.twilio_template_poll` (cada 5 min), sobre `admin_twilio_plantillas_por_revisar` (SECURITY DEFINER).
- **Sincronización**:
  - Cron `core.twilio_template_sync` (cada hora): trae todas las plantillas de cada cuenta, incluidas las creadas en la consola de Twilio, y adopta las que quedaron en `creating`.
  - El sync manual tiene un tope de uno por minuto por conexión.
- **Rutas** en `/api/v1/admin/connections/twilio/{id}/templates`:
  - `GET`: listar (con `solo_aprobadas`).
  - `POST`: crear.
  - `POST sync`.
  - `POST {tid}/submit` y `POST {tid}/refresh`.
  - `DELETE {tid}`: borrado lógico aquí; en Twilio también, si se puede.
- Los dos crons están declarados en el catálogo de ADR-0183.
- **Auditoría multitenant** (sin hallazgos altos):
  - **Polling**: un error que no es pasajero (token revocado, por ejemplo) aplaza la consulta una hora, para que una fila trabada no acapare el cron. Si el Content ya no existe (404), la plantilla se da de baja. El enumerador omite las conexiones pausadas.
  - **Sync**: al adoptar una plantilla en `creating` toma el contenido de Twilio, así lo que se muestra es lo que el `HX` envía. Da de baja las borradas en la consola. Los datos importados se acotan: solo el texto, hasta 20 variables y un idioma válido. Ante un conflicto de integridad responde 409, sin el SQL en el log.
  - **Borrar en Twilio**: solo se borra lo que creó solvershub (`origen`) y que ninguna otra fila usa. Las importadas de la consola se quedan en la cuenta.
  - **Permisos**: sincronizar y borrar piden propietario o master. Crear, enviar a aprobación y borrar quedan auditados.
  - **Tope del sync manual**: se toma después de validar la conexión.
- **Revisión cruzada del #719**:
  - El sync da de baja lo que falta solo si la lista de Twilio llegó **completa**. La paginación ahora avisa cuando la corta el tope, y las páginas son de 500.
  - Una plantilla adoptada por nombre queda como `origen='twilio'`, así que no se borra en Twilio.
  - Desconectar da de baja también las plantillas de la conexión.
  - Las regex usan `fullmatch` (antes `$` aceptaba un salto de línea final).
  - Ejemplos de variables numéricos y de hasta 256 caracteres. Tope de 1.000 plantillas por conexión.
  - Una plantilla borrada aquí que sigue en Twilio no se reimporta.
  - La auditoría guarda cuál plantilla fue y también registra el sync manual.

### Paquete `channel-twilio` (PR-2 de WhatsApp vía Twilio, ADR-0186) (2026-11-07)

- Nuevo `packages/channel-twilio`, registrado como el proveedor `twilio` en `agent_core.channels`. Trae:
  - la firma `X-Twilio-Signature`, igual que twilio-python y probada con sus vectores, que acepta la URL con puerto y sin él;
  - un cliente HTTP con auth básica, limitado a los hosts de Twilio y con los SID validados antes de armar la URL;
  - la tabla de códigos de Twilio a categoría de `ProviderError`;
  - Senders API v2 (listar, consultar y registrar webhooks) y Content API (crear, enviar a aprobación, consultar y listar plantillas, y validar sus variables);
  - el normalizador del status callback, con el mismo dict que lee `apply_status_update`;
  - los adaptadores de entrada (formulario → `InboundMessage`, con media, botón y lista) y de salida (texto, imagen y plantilla por `ContentSid`).
- Todavía no se usa: el tipo `whatsapp_twilio` sigue «Próximamente» hasta el PR-3 (alta y bandera por negocio). Sin migraciones ni rutas.

### Un gestor solo actúa sobre sus carteras (2026-11-07)

- agent-core#701 (seguridad, entre gestores del mismo negocio). La API de campo acota cada acción a las carteras que el token puede ver:
  - `/sync-offline/eventos`: el `carteraId` del evento debe ser una cartera visible (visita, pago, abono, gasto, trayectoria); el préstamo y el cliente deben ser de esa cartera; `cambio_cliente` exige un cliente de una cartera visible. Antes un gestor podía mover el saldo y la caja de la cartera de otro.
  - `DELETE …/pagos/{id}` y `…/abonos/{id}`: el movimiento debe ser de la cartera de la ruta (404 si no).
  - `POST …/visitas/no-pago`: valida `prestamoId` y `clienteId` contra la cartera (404) y un `clienteId` ausente ya no da 500 sino 400.
  - `GET /clientes/{id}/evidencia/{ev}`: solo si el cliente es de una cartera visible.
  - Un `cuotaId` que no es del préstamo ya no se enlaza al pago ni se marca pagada.
  - `GET …/clientes/{id}/prestamos` y `…/cuotas/{id}/detalle`: el cliente / el préstamo deben ser de la cartera de la ruta (404 si no).
  - Una fecha prometida mal formada da 400 sin dejar la visita a medias.
  - El HTML de la liquidación escapa el nombre de la cartera y el periodo.
- Propietario y master siguen viendo todas las carteras. Sin migración ni cambios en `docs/openapi.json`.

### Aceptar promociones exige evidencia con cualquier forma de obtenerlo (2026-11-07)

- Decisión del dueño (2026-11-07), ADR-0150 §6. `POST /admin/leads/{id}/consentimiento` con `estado: "otorgado"` ahora responde 400 `evidencia_requerida` («Cuenta cómo lo obtuviste.») si la evidencia viene vacía o solo con espacios, con «En el local», «Por teléfono», «Por escrito» y «Otro» (antes solo «Otro» la exigía). `revocado` no la exige.
- Orden de las comprobaciones: 403 por rol (solo propietario o master), luego la evidencia, luego el 409 `baja_del_cliente`. Sin migración; el contrato (schemas) no cambia, solo la descripción de la ruta en `docs/openapi.json`.
- Pendiente fuera de este PR: comprobar que el panel y la APK pidan siempre la evidencia al marcar «Aceptó recibir promociones»; si no, el equipo verá el 400 con el mensaje de arriba.

### Errores de proveedor por categoría (2026-11-07)

- PR-1 de WhatsApp vía Twilio (ADR-0186, en redacción). Base sin cambio de comportamiento para Meta:
  - `ProviderError(ChannelError)` en `core/gateway/channels/base.py`, con `provider`, `code`, `http_status`, `category` y `retry_after` (segundos). Categorías: TRANSIENT, RATE_LIMIT, QUOTA, AMBIGUOUS, AUTH, CONFIG, WINDOW, OPT_OUT, RECIPIENT, CONTENT, PERMANENT y la interna POLICY (waweb). Sin categoría, sale del estado HTTP: 429 es rate limit, otro 4xx es permanente y un 5xx o la falta de respuesta es transitorio.
  - `GraphError` pasa a ser `ProviderError` (`provider="meta"`) con las mismas categorías de antes; `retry_after_ms` se conserva.
  - `decide_retry` decide por categoría: CONFIG, WINDOW, OPT_OUT, RECIPIENT y CONTENT fallan al primer intento como PERMANENT y AUTH; QUOTA reintenta a la hora; AMBIGUOUS reintenta una sola vez y no va a la DLQ.
  - `InboundAdapter.verify_request` acepta `url` (los adaptadores actuales la ignoran) y `OutboundAdapter.send_content_template` lanza `ChannelError` por defecto.
  - Un `GraphError` de un envío interactivo ya no cae en el `except ChannelError` que lo volvía permanente.

### Aceptar promociones lo registra solo el propietario, y una baja del cliente no se deshace (2026-11-07)

- D-SPEC-33-9, opción C (decisión del dueño del 2026-11-07). `POST /admin/leads/{id}/consentimiento` con `estado: "otorgado"` ahora responde 403 a un colaborador con Clientes en escritura, antes de leer nada; propietario y admin de plataforma siguen igual. `revocado` no cambia (sigue abierto a quien tiene Clientes en escritura).
- Si la última fila del cliente es `palabra_de_baja` (el cliente se dio de baja) o `meta_131050` (WhatsApp ya no entrega marketing), `otorgado` responde 409 `baja_del_cliente` para todos, incluidos propietario y master; la baja hecha por el equipo (`ficha`) se sigue pudiendo deshacer. «Ya no quiere promociones» sobre una de esas bajas no agrega fila (si no, taparía la baja y la reactivación pasaría en dos pasos). La lectura de la última fila va con el cliente bloqueado para no perder una baja simultánea.
- ADR-0150 §6 enmendado. Sin migración. `docs/openapi.json`: solo cambia la descripción de la ruta, no los schemas.
- Pendientes fuera de este PR: panel, APK, ayuda y casos nuevos del catálogo de pruebas.

### El General y el Coordinador no se archivan (2026-11-07)

- `PATCH /api/v1/admin/agents/{id}` con `status: "archived"` responde 409 `agent_reserved` para el General y el Coordinador, aunque el General ya no esté en ninguna conexión. Antes se podía archivar el General tras quitarlo de todas, y entonces las conexiones nuevas quedaban sin agentes (`core_missing`) y fallaban el backfill y los presets.
- Archivar un agente que sigue en una conexión responde 409 `agent_in_use` (antes `conflict`), para que el panel diga qué hacer. Reactivar (`status: "active"`) no cambia.
- Sin migración ni cambios en `docs/openapi.json`.

### Alta de WhatsApp vía Twilio (PR-3, ADR-0186) (2026-11-07)

- **Bandera por negocio** `tenants.settings.canales.whatsapp_twilio`, apagada por defecto. Solo la cambia el master con `GET`/`PUT /api/v1/admin/platform/tenants/{id}/canales/{canal}`, y queda auditada como `canal_bandera`. Con la bandera encendida, el catálogo del negocio muestra el tipo `whatsapp_twilio` como disponible (`setup_kind: wizard`). Para el resto sigue «Próximamente», también en el panel clásico.
- **Alta guiada** en `/api/v1/admin/connections/twilio`. Piden Conexiones en escritura y ser propietario o master:
  - `POST /discover` valida el Account SID y el Auth Token y lista los números de WhatsApp de la cuenta.
  - `POST` guarda la conexión con las credenciales cifradas y su fila `twilio_connections` en una sola transacción, registra los webhooks del sender y los relee. Responde 201 al crear y 200 si la conexión ya existía.
  - `GET /{id}` devuelve el resumen.
  - Si el sender ya tenía webhooks de otra integración, el alta pide `confirm_overwrite_webhook` y guarda los anteriores.
  - Las credenciales inválidas responden 422, no 401, porque el panel trata un 401 como sesión expirada.
- **Migración 0121**:
  - tabla `twilio_connections`, con RLS forzada y sin DELETE para `app_runtime`;
  - índice único global `uq_channel_connections_twilio_sender_viva`: un número, un negocio, con el mismo patrón que `phone_numbers` y waweb;
  - función `twilio_route_connection(public_id)` (SECURITY DEFINER), para el webhook del PR-4.
  - Ajuste a ADR-0186 §4: la unicidad del número va sobre `channel_connections`, no sobre `twilio_connections`. Así la cubre la misma transacción de `save_connection_in` y la tabla conserva su RLS.
- `save_connection_in(session, …)`: `save_connection` dentro de la sesión de quien llama, con las mismas reglas.
- **Envío**: `resolve_credentials` arma las credenciales que espera el adaptador de Twilio (`from` y `status_callback_url`) y no cae a la primaria. `_send_one` falla con CONFIG si la conexión de Twilio no tiene credenciales completas.
- **Auditoría multitenant** (antes de abrir el PR):
  - FK compuesta `(tenant_id, channel_connection_id)`.
  - **Una cuenta de Twilio, un negocio**: lo controla `twilio_account_en_otro_negocio` (SECURITY DEFINER) con un candado por cuenta, y responde 409 `cuenta_en_otro_negocio`. El Auth Token de la cuenta firma los webhooks de todos sus números, así que dos negocios con la misma cuenta podrían suplantarse.
  - El alta exige que el `tenant_id` sea el del `tenant_scope`.
  - Revelar el Auth Token pide ser propietario o master.
  - Una conexión de Twilio no se puede convertir en otro tipo desde el formulario manual.
- **Pendiente para el PR-5**: el borrado lógico de la conexión todavía no marca `twilio_connections` ni restaura los webhooks previos. El ruteo ya lo descarta, porque mira `channel_connections.deleted_at`.
- **Revisión cruzada del #716**:
  - 0121 exige un rol con BYPASSRLS, como 0063 y 0073. Sin él, las funciones SECURITY DEFINER quedarían sujetas a la RLS y «una cuenta, un negocio» fallaría abierta.
  - `twilio_account_en_otro_negocio(account_sid)` toma el negocio de `app.tenant_id` y, si no hay negocio, falla cerrada.
  - Una conexión de Twilio borrada, pausada o sin fila viva no envía, aunque tenga credenciales guardadas.
  - Pausar, borrar o marcar como principal una conexión de Twilio exige ser propietario o master.
  - Los demás choques de integridad responden 409 `conflicto`, sin el texto de la base.
  - Los SID se limpian de espacios antes de validarlos.

### El agente no dice que no hay promociones (2026-11-07)

- `aplicar_promocion` ya no devuelve las frases «No hay promociones vigentes aplicables» ni «No aplicó ninguna promoción», que el modelo repetía al cliente. Devuelve datos (`applied: false`, `motivo` = `sin_vigentes` o `no_aplica`, sin la palabra «promociones» para no invitar al modelo a decirla, y el `subtotal`/`total` de la cotización, que la primera rama no traía).
- El playbook `ventas` (pack_commerce 3.1.0) dice cuándo hablar de promociones: solo si `aplicar_promocion` devolvió `applied: true`. Sin una vigente, el agente no saca el tema ni dice que no existen; si el cliente pregunta, responde con naturalidad y lo orienta a lo que sí ofrece el negocio.
- El playbook cambia el prompt de los negocios con `pack_commerce` y sin override de `ventas`: las grabaciones de evals (`evals/cassettes/`) hay que regrabarlas con OK del dueño. Los negocios con `ventas` propio (p. ej. `solvershub-comercial`) no reciben la regla.

### ADR-0186 y ADR-0187: WhatsApp vía Twilio (2026-11-07)

- Se proponen dos ADR, sin cambios de código.
- **ADR-0186:** conexión WhatsApp vía Twilio con la cuenta del negocio (BYOK). Cubre:
  - el alta guiada, con descubrimiento del número y registro automático de webhooks;
  - el webhook por conexión, firmado contra la URL registrada;
  - las plantillas de la Content API, con su polling de aprobación;
  - botones y listas;
  - los errores por categoría y la salud.
- **ADR-0187:** fase ISV con una subcuenta por WABA. Está bloqueada hasta que Meta verifique a solvershub.
- Producto: D-63, HU-CONN-11 a HU-CONN-18 (docs).

### Retirar los datos de un cliente es solo del propietario o el master (2026-11-06)

- agent-core#692. `POST /admin/leads/{id}/consentimiento-datos/revocar` aceptaba a un colaborador con Clientes en escritura aunque la API dice «Solo dueño o master»: ahora responde 403 «Solo el propietario del negocio puede retirar los datos de un cliente.» antes de leer o cambiar nada. Propietario y admin de plataforma siguen igual; ver el estado (`GET …/consentimiento-datos`) sigue siendo de lectura. Sin cambios en el contrato (`docs/openapi.json`) ni migración.

### «Conversaciones recientes» del Resumen trae el canal y cuándo empezó (2026-11-06)

- `GET /admin/dashboard`: cada conversación de `recent` suma `channel` y `started_at` (opcionales, sin migración). El panel los usa para el avatar y el «hace X» de «Conversaciones recientes».

### La API de campo aplica la matriz de permisos del gestor (2026-11-06)

- agent-core#690 (seguridad). Antes la matriz de 14 permisos del gestor solo se le devolvía a la app y el servidor no la validaba: quitar `registrar_pago` no impedía registrar el pago por la API. Ahora las rutas de escritura de `/api/pack/pack_cobranza/gestor/…` y la cola offline (`/sync-offline/eventos`) responden `403 {"error": "sin permiso", "permiso": "<nombre>"}` si el gestor no tiene el permiso, sin escribir nada: `registrar_pago` / `registrar_abono` / `registrar_no_pago` (visitas, incluida la promesa; el permiso sale de la acción efectiva, no de la URL), `registrar_gasto`, `registrar_prestamo`, `anotar_notas_cartera`, `actualizar_cliente` (editar cliente y agregar evidencias), `eliminar_pago` / `eliminar_abono` y `generar_reporte` (generar liquidación). En la cola offline el evento sin permiso vuelve con `estado: error` y `error: "sin permiso: <nombre>"`.
- Denegado por defecto: un gestor sin filas en su matriz no puede escribir (la app ya lo veía todo en falso). Propietario y master no cambian. Sin migración ni cambios en `docs/openapi.json` (las rutas de campo no están en el OpenAPI).

### El asistente dice su nombre cuando se lo preguntan (2026-11-06)

- El playbook `identidad` ahora afirma «Tu nombre es {assistant_name}» y le pide responder con ese nombre si el cliente pregunta cómo se llama o quién es. Antes solo decía «Eres {assistant_name}, el asistente virtual de: …» y el modelo a veces respondía «el asistente virtual de la tienda» sin el nombre (agent-core#674).
- Cambia el prompt de todos los negocios sin override de `identidad`: las grabaciones de evals (`evals/cassettes/`) hay que regrabarlas con OK del dueño.

### Una inscripción creada desde el panel ya se puede aprobar en Alta automática (2026-11-06)

- Corrige agent-core#672: la espera de aprobación de una inscripción con `origen = panel` se lanzaba como revisión «por un dato» y `POST …/altas/{referencia}/aprobar` respondía 409 `alta_no_revisable`; solo se podía descartar, así que ningún alta desde el panel (Free o de pago) llegaba a crear el negocio.
- Ahora es un motivo más de la revisión **por riesgo** (ADR-0125 § 6), junto a los demás controles (interruptor, aceptación, Free repetido, recompra, señales): quien aprueba ve todos los motivos y la aprobación no salta controles que nunca se evaluaron. Un dato inválido (correo, plan) sigue siendo revisión por dato: se corrige y se reintenta.
- Sin cambios de API ni de esquema. Pruebas en `tests/integration/test_antiabuso_alta.py`.

### Los runners de evals registran las tools del core antes de activar los packs (2026-11-04)

- `evals/journeys.py` y `evals/runner.py` activaban los packs antes de registrar las tools del core, así que el resumen de confirmación de `pack_subscriptions` sobre `sub_confirmar_inscripcion` no se registraba y los journeys confirmaban con el texto de respaldo y el `monto_a_pagar` que firmó el modelo (tras cambiar de plan salía el anual con el total del mensual; corrida #17 de pass^4, `cambia-de-plan-antes-de-confirmar` 3/4). Ahora el orden es el de `core/main.py`. Prueba de guardia en `tests/test_resumen_del_pack_orden_runners.py`. La app real no estaba afectada.

### El error de monto al confirmar una inscripción dice cómo corregirlo (2026-11-04)

- `sub_confirmar_inscripcion`: cuando el `monto_a_pagar` no coincide con el total vigente del plan (p. ej. tras cambiar de plan el modelo repetía el de la cotización anterior), el error ahora le indica cotizar de nuevo ese plan con `sub_cotizar_inscripcion`. Antes decía solo «no coincide» y el modelo repetía el mismo monto turno tras turno hasta el tope (corrida de pass^4 del 2026-11-04, `cambia-de-plan-antes-de-confirmar` 2/4).
- El resumen de confirmación ya no repite la periodicidad que el nombre del plan dice («Premium anual», no «Premium anual anual»).

### El verificador detecta montos con COP al final (2026-11-04)

- El verificador de afirmaciones ahora detecta montos escritos con la moneda al final («200.000 COP»), no solo `$ 200.000` o `COP 200.000`: una cifra sin respaldo en las tools se bloquea igual (antes pasaba con o sin evidencia).

### Journey de informar planes sin comprar (2026-11-04)

- Journey `procedimientos/05-informar-planes-sin-comprar` (ADR-0185 / HU-SUB-37): el cliente solo pregunta por planes y el asistente usa `sub_listar_planes`, no inicia el procedimiento de compra y no deja tarea abierta. El runner de journeys expone `{conversacion.id}` para los pasos `verifica`. Sin grabación: el set `procedimientos` corre solo con LLM real a demanda.

### Guardar un borrador con algo retirado avisa en palabras simples (2026-11-04)

- `PUT /api/v1/agents/{id}/draft`: si el borrador nombra una acción o un procedimiento retirado, el 422 trae el mismo aviso en palabras simples que publicar (qué ya no existe y qué hacer) en lugar del mensaje técnico del linter (HU-AOS-59).

### Corregido

- Declara `packaging` como dependencia directa. kombu 5.7.0 lo importa en el transporte de redis sin declararlo; la imagen instala con `pip` sin lock y, al reconstruirse, dejaba al worker y al beat en bucle de reinicios (`ModuleNotFoundError: No module named 'packaging'`).

### El verificador reconoce `saldo`, `saldo_total` y `total_vigente` como montos (2026-11-03)

- HU-ARCH-29, ADR-0119. Sigue a «`precio` es clave de dinero»: se revisaron las demás tools de los packs y `MONEY_KEYS` suma
  las que son claramente dinero y hoy no respaldaban lo que el asistente dice: `saldo` y `saldo_total` (consultar saldo y
  opciones de promesa de pack-cobranza) y `total_vigente` (cotización en curso de pack-commerce). `valor` y `valorPeriodo`
  quedan fuera a propósito (genéricos). Solo se amplía la evidencia: un monto que ninguna tool devolvió sigue bloqueado.
- Limitación conocida, sin cambio: el detector solo reconoce `$` o `COP` ANTES de la cifra, así que «200.000 COP» no se
  verifica (pasa con o sin evidencia). Ampliarlo es un cambio de comportamiento aparte; un test lo documenta.

### `GET /admin/dashboard/agents` trae la serie diaria del negocio entero (2026-11-03)

- ADR-0171, rediseño del Resumen (SPEC-29). `general` suma el campo `serie` (turnos, conversaciones y costo por día, cero-relleno,
  `dias` puntos): la suma de todos los agentes, el Coordinador y lo no atribuido, así que coincide con el total del negocio. La
  serie del agente «General» sigue siendo solo su actividad más lo no atribuido.
- Campo opcional con valor por defecto `[]`; vacío sin la sección `agents` (igual que la serie por agente) y con `costo` nulo
  sin la sección `costs`. Sin cambios de esquema de base de datos.

### `reporte-ruteo` mide sin depender de que el agente inicie el procedimiento (2026-11-03)

- HU-AOS-17, ADR-0138 (adenda). El reporte comparaba la predicción del router con la primera tarea iniciada en la
  conversación. En `shadow` la inicia el agente por defecto; si no lo hace, «Reales» salía 0 y la precisión 0,00
  aunque la predicción fuera correcta (visto en `solvershub`: 7 aciertos, 0 falsas). Con `aos_routing = active` no se
  escribe `shadow_route` y el reporte quedaba vacío.
- `agent-core db reporte-ruteo --tenant X [--fuente golden|trafico|ambas] [--golden archivo]` ahora trae dos fuentes,
  cada una rotulada con lo que mide:
  1. **Golden de ruteo** (verdad por escenario) contra la release activa de cada conexión: no depende del tráfico ni
     del agente, y sirve en `off`, `shadow` y `active`. Lista los casos que fallan y los no aplicables. Un caso «ninguno» donde el router elige un procedimiento ajeno al catálogo del golden queda «sin verdad», no como falso positivo.
  2. **Tráfico real** con su origen (`sombra` o `activo`): en activo mide los disparadores (primer candidato) frente a
     lo que inicia el especialista; las reglas no se miden en activo.
- Una predicción en un turno donde no se inició nada es ahora «sin verdad», no un fallo; la precisión es «no medible»
  si no hay ninguna con verdad. Una leyenda explica por qué una fuente sale vacía (ruteo apagado, `active`,
  `aos_procedures` apagada, nadie inició nada) y se imprimen las banderas del negocio.
- El golden de ruteo pasa de `tests/fixtures/ruteo/golden.jsonl` a `evals/ruteo/golden.jsonl` (viaja en la imagen).
  Sin cambios de esquema ni de lo que se escribe en `core_decisions`.
- Runbook de migración § 3.4 con cómo leer cada fuente.

### Referencias retiradas en agentes y releases: detectar, migrar y no repetir (2026-11-03)

- HU-AOS-59 (SPEC-26), entrega backend. Quitar una acción o un procedimiento del binario exige una entrada en el nuevo registro de retiros (`core/agents/retiros.py`: reemplazo con versión o «quitar»), con las dos de SPEC-43 PR-4 (`informar_plan` → quitar; `pack_commerce.contratar_plan` → `pack_subscriptions.contratar_plan ^2.0`). El CI lo hace cumplir contra `packages/catalogo-nombres.lock.json` (`python scripts/catalogo_nombres_lock.py`).
- `agent-core db auditar-referencias [--tenant <slug>|--todos]`: solo lectura; compara la última versión publicada de cada agente y el manifiesto de la release activa de cada conexión viva con el catálogo del binario. Borradores y conexiones borradas, de pruebas o de integración aparte. Exit code distinto de cero con afectados.
- `agent-core db migrar-referencias --tenant <slug> [--aplicar]`: dry-run por defecto; publica una versión nueva por agente partiendo de la última publicada (el borrador no se toca), agrega el reemplazo solo si el negocio tiene la habilidad, y reconstruye y promueve la release por la ruta de `migrar-a-v3 --construir` (con canary en curso construye y no promueve). Idempotente y con auditoría. Nueva exención `migrar_referencias`.
- Decisión del dueño: solo avisar, sin corrección automática. Runbook `docs/runbooks/referencias-retiradas.md` (tras migrar la base: `auditar-referencias --todos`), ADR-0184. Falta (entregas posteriores): endpoint de master, aviso al dueño en el editor y en la publicación, y botón «Corregir» del panel.

### El precio del listado de planes respalda lo que el asistente dice (2026-11-03)

- ADR-0185 (seguimiento), HU-SUB-37. `precio` pasa a ser clave de dinero del verificador (`MONEY_KEYS`): `sub_listar_planes` devuelve el precio del plan bajo esa clave y, sin ella, un «$200.000» tomado del listado se bloqueaba con `monto_sin_evidencia` y el cliente recibía un escalamiento en vez del precio (solo pasaba «200.000 COP»). Un monto que la tool no devolvió se sigue bloqueando. Hoy solo esa tool devuelve `precio`; no cambia ningún prompt ni cassette.

### pass^k de procedimientos: sin línea base imposible (2026-11-03)

- Corrida #14 de `evals-procedimientos` (develop `ac66a190`, k=4): con motor 94 % / pass^4 75 %, sin motor 44 % / 25 %. Causa raíz, sin LLM: desde SPEC-43 `contratar_plan` vive en `pack_subscriptions` y sus acciones (`sub_listar_planes`, `sub_cotizar_inscripcion`, `sub_confirmar_inscripcion`, `sub_inscribir`) las ofrece y ejecuta solo el motor (`solo_con_motor_activo`). Sin motor un cliente nuevo no tiene cómo ver ni cotizar un plan (`sub_consultar` es de la suscripción propia): el asistente dice «no pude cargar el detalle» y escala, 0/4 por diseño. En la #13 la línea base pasaba porque los planes eran productos de commerce (`consultar_inventario`, siempre disponible).
- Los journeys pueden declarar `"requiere_motor": true`; `evals/journeys.py --runtime legacy` los omite sobre un set con motor (lo dice el log y el reporte, campo `omitidos`). Marcados: `01-contratar-premium-mensual` y `02-cambia-de-plan-antes-de-confirmar`. Con `--runtime v3` o sin `--runtime` corren todos.
- `04-agendar-una-demo`: el cliente simulado ya no pregunta por planes ni precios (el journey mide `agendar_cita`). El único intento fallido del motor (3/4) abrió preguntando por el Plan Premium: sin tarea en foco, `sub_listar_planes` no se ofrece y el asistente escaló antes de pedir la demo.
- Runbook de migración § 3.5: la comparación con y sin motor es sobre los journeys que ambos pueden cumplir.

### Sumas por etapa para el tablero de embudo (HU-CRM-24, 2026-11-03)

- **`GET /api/v1/admin/leads` acepta `resumen=valor`** junto a `facetas`: cada faceta devuelve además `sumas`, el valor
  estimado que suman los leads de cada opción (`sin_valor` incluido, `0` si no hay). Las sumas respetan los mismos filtros,
  agente y búsqueda que los contadores, y un lead sin valor estimado aporta `0`. Sin `resumen` la respuesta no cambia
  (`sumas` queda en `null`). Un valor de `resumen` desconocido se rechaza con 422.
- **Registro de leads.** Nuevo gancho opcional `medida_query(medida)` en `LeadProvider`: un `SELECT customer_id, monto` por
  cliente. El core lo une a `facet_query` sin conocer las tablas del pack; `pack_commerce` lo implementa para `valor`.
- Sin migración ni cambios en el esquema de tools: las grabaciones del LLM siguen vigentes.

### Agent OS como único motor desde el panel (2026-11-03)

- El panel del master ya no puede devolver un negocio a `legacy`: `PUT` y `POST …/agent-os/simular`
  responden 422 `agent_os_orden` si el cambio incluye el paso `runtime_legacy`. Un negocio que ya está
  en `legacy` sigue pudiendo pasar a v3, y repetir el mismo estado sigue siendo «sin cambios».
- El rollback de emergencia queda solo por consola: `agent-core db migrar-a-v3 --volver --tenant <slug>`.

### Aviso de referencias retiradas: API de solo lectura (2026-11-03)

- HU-AOS-59 (SPEC-26), entrega API (ADR-0184, adenda). Solo avisa; no escribe ni corrige.
- Master: `GET /api/v1/admin/platform/tenants/{tenant_id}/retired-references` devuelve lo mismo que `auditar-referencias --tenant` (agentes, versiones, rutas, códigos, releases activas afectadas, borradores y conexiones aparte). Solo `master`.
- Dueño: `GET /api/v1/admin/agents/retired-references` y `GET /api/v1/admin/agents/{id}/retired-references` devuelven, por agente, el aviso en palabras simples (qué ya no existe y qué hacer, sin códigos como `accion_inexistente`), aislado por negocio. Texto en `core/agents/aviso_retiros.py`.
- Publicar un agente cuyo borrador nombra algo retirado ahora da 422 `invalid_spec` con ese mismo texto por campo y `details.retired_references`, antes de publicar. Para un procedimiento retirado cambia el comportamiento: antes la versión se publicaba (200) y el build de la release fallaba después; ahora es 422 antes de publicar. La fixture de ventas de `test_agentes_versionados` ya no nombra procedimientos retirados.
- `docs/openapi.json` regenerado.

### Informar planes sin iniciar la contratación (2026-11-03)

- ADR-0185 (aceptado), SPEC-43 · HU-SUB-37. Con el motor de procedimientos activo, `sub_listar_planes` deja de ser una acción solo de procedimiento: es una consulta de solo lectura que se ofrece también sin tarea en foco, así que un cliente que solo pregunta por planes recibe la respuesta del catálogo en una vuelta, sin abrir `contratar_plan` ni escalar. Cotizar, confirmar e inscribir siguen detrás del procedimiento. Sin el motor nada cambia (la tool no se ofrece, y no se tocan las cassettes legacy).
- El playbook `planes` dice que se informa con `sub_listar_planes` directo y que `contratar_plan` se inicia cuando el cliente quiere el plan.
- Se regraba la cassette `suscripciones/01-informar-planes` del gate (cambió el conjunto de tools del turno).
- Pendiente aparte: `precio` como clave de dinero del verificador (hoy un «$200.000» tomado del listado se bloquea por `monto_sin_evidencia`).

### El reenganche de leads vuelve a enviar con la configuración por defecto (2026-11-02)

- `pack_commerce.follow_up_leads` pedía leads con actividad más vieja que `follow_up_hours` **y** dentro de las
  últimas 24 h (ventana de WhatsApp). Con `follow_up_hours = 24` (el default) las dos cotas coincidían: el cron
  corría cada día a las 9:00 y no enviaba nada. Los tests usaban `follow_up_hours = 1` y no lo veían.
- `follow_up_hours` pasa a 4 por defecto. Una espera que no cabe en la ventana (0, o 24 o más: una config
  guardada con el valor viejo) se lee como el valor por defecto en vez de dejar el seguimiento muerto. El rango
  del esquema (`le=8760`) no cambia, para que las configuraciones ya guardadas con 24 sigan cargando.
- Un lead con actividad hace más de 24 h no se reengancha (no se puede mandar texto libre).
- Test de integración `test_pack_commerce_reenganche_leads_ventana.py`.

### Gestión de crons de la plataforma (ADR-0183, 2026-11-02)

- **API master.** `/api/v1/admin/platform/crons`: catálogo de los 34 crons del beat clasificados por módulo (estado, horario
  vigente y por defecto, próxima ejecución, última ejecución y fallos de 24 h), resumen por módulo, detalle, historial
  paginado, **ejecutar ahora**, **pausar/reanudar** y **reprogramar** (`PUT`) / restaurar (`DELETE`) el horario. Solo el
  master, también en test: el tester no opera crons.
- **Historial.** Cada cron (core y packs) pasa por `run_cron`: registra en `cron_runs` quién lo disparó, cuánto tardó y cómo
  terminó (de un fallo, solo la clase de la excepción), con un lock en Redis que impide que dos ejecuciones del mismo cron se
  solapen. Sigue sin relanzar la excepción. El latido del worker y el outbox no guardan sus éxitos. Retención de 30 días
  (`core.cron_runs_retention`, nuevo cron diario).
- **Horario reprogramable.** Nuevo planificador `DbOverrideScheduler` (`-S core.tasks.cron_scheduler:DbOverrideScheduler`
  en `entrypoint-beat.sh` y `docker-compose.yml`): el beat relee `cron_job_configs` cada 30 s, sin reiniciar. **Requiere
  reiniciar el beat al desplegar.**
- **Migración 0120.** Tablas de sistema `cron_job_configs` y `cron_runs` (sin RLS, sin datos de negocio).
- **Catálogo.** `core/tasks/cron_catalog.py`; un cron nuevo debe agregarse ahí (un test lo exige).
- **Compatibilidad.** Aditivo: los crons siguen con los mismos nombres y horarios (un test lo comprueba); la tarea de cada
  cron acepta `trigger`/`triggered_by` opcionales. `docs/openapi.json` actualizado.

### Motivo de pérdida y valor estimado del lead (HU-CRM-23, 2026-11-02)

- **Perder un lead pide motivo.** Pasar un lead a «perdido» (desde la ficha del panel o la app, o con la tool `actualizar_lead`
  del agente) exige un motivo del catálogo cerrado (`precio`, `sin_respuesta`, `compro_en_otro_lado`, `no_era_para_el`,
  `otro`) y admite una nota de hasta 300 caracteres. Sin motivo válido no cambia nada y el mensaje lista los permitidos.
  La tool ofrece `loss_reason`/`loss_note` (opcionales en el esquema, obligatorios al perder).
- **Reabrir.** Reabrir un perdido limpia el motivo vigente; el motivo de la pérdida queda en la historia de etapas y en la línea
  de tiempo («Motivo: Precio — nota»). Un perdido anterior se muestra como «Sin motivo registrado» y se puede anotar después.
- **Valor estimado.** Campo opcional del lead (`numeric(12,2)`, nunca negativo, en la moneda del negocio) editable desde la
  ficha; se escribe como número simple, sin separador de miles. Nuevo `tipo="numero"` en los campos editables del lead.
- **Lista.** Filtros `motivo_perdida` y `valor` (tramos fijos, con contadores) y órdenes `orden=valor` / `orden=valor_desc`
  con los leads sin valor siempre al final; columnas `motivo_perdida` y `valor_estimado`.
- **Migración `crm_0019`** (aditiva; CHECK creados `NOT VALID` y validados; `lock_timeout` 5 s). Reversa solo sin datos.
- **Grabaciones del LLM.** El esquema de `actualizar_lead` cambió: las 9 grabaciones que incluyen la tool quedan desactualizadas
  hasta regrabarlas con `scripts/gate_evals.sh record` (~0,01 USD, con OK del dueño).

### Recordatorios de seguimiento y resumen diario de ventas (HU-CRM-22, 2026-11-02)

- **Aviso al vencer.** Nuevo trabajo `pack_commerce.recordar_seguimientos` (cada 5 minutos, sin LLM y sin mensajes al
  cliente): cuando llega la hora de un seguimiento que una persona programó (HU-CRM-21) crea el aviso
  `commerce.seguimiento_vencido` («Seguimiento: <cliente>», con la nota y la hora) que abre la ficha del cliente.
- **Una sola vez.** Sin tabla ni migración: la clave de deduplicación del aviso es `seguimiento:<lead>:<instante>`, así que
  correr el trabajo de más o reiniciarlo no repite nada y, si la fecha cambia, vuelve a avisar por la fecha nueva.
- **Reglas.** Espera al horario de atención del negocio (`escalation_config.business_hours`; sin horario, siempre abierto);
  no avisa de un lead `convertido` o `perdido`; un seguimiento vencido hace más de 4 días ya no genera aviso (sigue en
  «Vencidos»); un lead sin fecha, como los que crea el agente, no genera nada.
- **Resumen diario.** `commerce.resumen_ventas` («Tu día de ventas»): una vez al día, desde la hora configurada en la zona
  del negocio y hasta 4 horas después, con los seguimientos de hoy, los vencidos y los leads nuevos de las últimas 24 h
  (mismas reglas que los filtros de la lista). Sin nada que decir, no sale. Abre la lista filtrada por «Vencidos» o «Hoy».
- **Configuración.** Tres campos nuevos en la configuración del pack, encendidos por defecto: `seguimiento_recordatorios`,
  `seguimiento_resumen_diario` y `seguimiento_resumen_hora` (0–19, por defecto 8).
- **Contrato.** El destino de un aviso (`NotificationTarget`) gana el campo opcional `filtro` (`clave:valor`, p. ej.
  `seguimiento:vencidos`); aditivo: un cliente que no lo conozca abre la lista sin filtrar. `docs/openapi.json` actualizado.

### Próxima acción y fecha de seguimiento del lead (HU-CRM-21, 2026-11-02)

- **Seguimiento del lead.** Una persona deja anotado qué va a hacer con un lead y cuándo (`next_action_note`,
  `next_action_at`, migración `crm_0018`). La fecha se guarda en UTC; lo que se escribe sin zona es hora del
  negocio y «hoy» se cuenta con la zona del negocio, no con la del servidor.
- **Contrato.** Nuevo tipo `fecha` en los campos editables de la ficha (ADR-0149, enmienda ADR-0181); el campo
  `seguimiento_en` se devuelve en hora del negocio para el selector. La lista trae `seguimiento_en`,
  `seguimiento_nota` y `seguimiento_estado`; filtro `seguimiento` (`vencidos`, `hoy`, `proximos_7_dias`,
  `mas_adelante`, `sin_seguimiento`, con contadores por faceta) y `orden=seguimiento` (el más atrasado primero).
- **Historia.** Tabla nueva `pack_commerce_lead_followups`: al reprogramar, quitar o completar un seguimiento queda
  su nota y la fecha que tenía en la línea de tiempo del cliente. Ruta nueva
  `POST /api/v1/pack/commerce/leads/{cliente}/seguimiento/hecho` para completarlo y programar el siguiente.
- Programar no mueve `last_activity_at` y el agente no tiene cómo cambiarlo (sin tools nuevas ni cambios de prompts).

### Configuración: cierre de la ola 1 (2026-11-02)

- Registro de opciones: cuatro grupos nuevos con su ayuda, etiqueta y rango — `cerebro` (modelo principal, de razonamiento y de respaldo, temperatura, herramientas por turno, presupuesto), `memoria` (mensajes recientes, fragmentos a buscar, parecido mínimo), `chat_web` (activo y dominios permitidos) y `multimedia` (notas de voz, imágenes y modo de imagen). Solo describen: cada una se sigue guardando con su `PUT` (`/brain`, `/memory`, `/web-chat`, `/media`). El proveedor, el modelo y la llave de audio y visión no se describen.
- Los rangos enteros de herramientas por turno, mensajes recientes y fragmentos salen de `core/config_limites.py`, y los decimales de las constantes del registro; `PUT /brain` y `PUT /memory` leen de ahí. `PUT /brain` ahora rechaza un presupuesto por conversación mayor de 1000 USD (antes sin tope).
- `PUT /admin/settings/identity`: si el horario de atención usaba la zona del negocio, al cambiar `timezone` el horario la sigue; uno con zona propia no se toca.
- Configuración de habilidades: un campo marcado `oculto` en el esquema (`json_schema_extra={"oculto": True}`) no sale en el formulario y conserva lo guardado. `asistente_activo` y `escalar_siempre_a_humano` de Cobranza quedan ocultos: el runtime no los lee.
- `GET /admin/settings/catalog` trae `aos_runtime` (`legacy` o `v3`) para que el panel avise, junto al nombre, la descripción y la voz de marca, que con `v3` esos cambios se aplican al publicar una versión del agente.

### Asistente de configuración por preguntas (2026-11-02)

- Configuración: `core/config_registro/asistente.py` traduce las respuestas del dueño a opciones del registro (SPEC-37). Dos secciones en la ola 1: «Cómo habla tu asistente» (tono, emojis, largo, frases prohibidas) y «Cuándo y cómo atiende» (horario, fuera de horario, velocidad, cuándo pasa a una persona).
- API: `POST /admin/settings/asistente/{seccion}/vista-previa` (valor actual → propuesto, sin escribir) y `POST .../aplicar` (exige la huella de la vista previa; 409 si cambió). Aplica todo o nada, en una sola transacción: configuración del negocio y persona del agente.
- `GET /admin/settings/catalog` trae el guion en `asistente`. Con `aos_runtime = v3` las preguntas que solo cambian la voz no se ofrecen, porque la voz sale de la release publicada.
- `save_spec_version` acepta una `session` opcional para guardar el AgentSpec dentro de la transacción de quien llama.

### El borrador automático de lead avisa «Lead nuevo» (2026-11-02)

- **Fix de HU-CRM-20.** Con `auto_create_lead` (activo por defecto) el lead nace como borrador al abrirse la
  conversación y `crear_lead` lo encuentra y responde `ya_existe`, así que el aviso `commerce.lead_nuevo` no salía
  nunca para los leads del chat web ni de WhatsApp: solo salía si el pack tenía el borrador apagado. Ahora
  `_on_conversation_started` lo emite al crear el borrador, con destino al cliente (la ficha se abre por id de
  cliente) y una sola vez por lead (`dedupe_key = lead_nuevo:<lead_id>`).
- Con `auto_create_lead` apagado, o si el cliente ya tiene lead, no se crea ni se avisa nada. Efecto a tener en
  cuenta: el borrador nace al abrirse cada conversación nueva de un cliente sin lead, así que habrá un aviso por
  cada cliente nuevo; quien no lo quiera lo apaga por categoría en las preferencias del centro de notificaciones.

### Test de horario de ingress con reloj fijo (2026-10-31)

- `tests/test_kernel_ingress_orden.py::test_dentro_de_horario_no_avisa` usaba la hora real con la franja `00:00`–`23:59` UTC. El fin de la franja es exclusivo, así que entre las 23:59 y las 00:00 UTC el negocio contaba como cerrado y el test fallaba (visto en agent-core#645, 23:59:23 UTC). Ahora evalúa el horario con `now` fijo a mediodía. Solo cambia el test; no hay cambio de comportamiento.

### Etapa del lead confiable y con historia (2026-10-30)

- **HU-CRM-18 / ADR-0181.** La etapa del lead de venta pasa por una sola función
  (`pack_commerce.etapas.cambiar_etapa`) que valida contra las cinco etapas, aplica las reglas por actor y
  registra cada cambio (anterior, nueva, agente|persona|sistema, cuándo) en la tabla nueva
  `pack_commerce_lead_stage_changes` (migración `crm_0015`, con RLS e idempotencia).
- `actualizar_lead` rechaza una etapa inválida con la lista de las válidas y no guarda nada del llamado;
  `calificar_lead` actualiza el score pero ya no devuelve a «calificado» a un lead contactado, convertido o
  perdido; solo una persona (panel o APK) reabre un lead «perdido»; el seguimiento automático deja a «sistema»
  como actor y no pisa un cambio hecho a mano mientras salía el mensaje.
- La línea de tiempo de la ficha muestra «Etapa: A → B» con quién lo hizo, y la ficha trae `etapa_desde`
  y `etapa_desde_aproximada` en `extras` (los leads anteriores usan su última actividad, marcada como
  aproximada). Sin cambio en `docs/openapi.json`.

### Descuento de N períodos en suscripciones (2026-10-30)

- SPEC-43 · HU-SUB-35 (PR-2b-2a). Migración `pack_subscriptions_0012`: `promocion_id`, `descuento` y `descuento_periodos_restantes` en `subscriptions` (las tres NULL o las tres con valor; `restantes` de 1 a `periodos_total`), el único parcial `uq_pack_subscriptions_charges_periodo` (un cargo de período no anulado por suscripción y período; la migración se detiene si ya hay duplicados) y el paso de los canjes de un período `confirmado` a `agotado`.
- La API de promociones acepta de 1 a 12 períodos (el monto fijo, solo 1). El pago de la inscripción deja el descuento en la suscripción con los períodos que faltan; cada cargo de período lo consume una vez, en la misma transacción que crea el cargo. Al agotarse, el canje queda `agotado` y la suscripción sin descuento.
- `crear_cargo_de_periodo` es el único creador de cargos de período (cron, link de pago y pago externo). El cron toma las suscripciones con `SKIP LOCKED` y un negocio que falla ya no frena a los demás. Un pago externo se crea a precio de lista y no consume. El link de pago ya no crea un cargo de período encima del período que pagó la inscripción.
- Cancelar o cambiar de plan (inmediato, al pagar un upgrade o al aplicarse un downgrade programado) termina el descuento: el canje queda `terminado`, sigue contando para el límite por cliente.
- Un upgrade acepta `codigo_promocion`: el descuento recae sobre la diferencia prorrateada, esa diferencia no consume período y los N empiezan en el siguiente cargo completo; un código nuevo reemplaza el descuento vigente solo al pagar. Un downgrade con código responde `promo_no_aplica_a_downgrade`. `cotizar_cambio` acepta `codigo_promocion` y devuelve `monto_bruto`, `descuento` y `reemplaza_descuento`. La diferencia de un upgrade usa el factor `monto / monto_bruto` del período en curso si se pagó con descuento.
- Cron diario `verificar_descuentos`: loguea `subs_descuento_inconsistente` si `restantes` no cuadra con los cargos que consumieron período, y los gauges de descuentos vigentes. Logs `subs_descuento_otorgado|consumido|agotado|terminado` y contador `descuento_periodos_consumidos_total`.
- Corrige un error del PR-2: `change_plan` entre planes del catálogo no actualizaba `plan_id` de la suscripción (solo `variante_id`).

### Contratar un plan desde suscripciones (2026-10-30)

- SPEC-43 · HU-SUB-29 y lado del agente de HU-SUB-34 (PR-3). Nuevo procedimiento `pack_subscriptions.contratar_plan` (v2.0.0, pack `pack_subscriptions` 0.4.0) con las tools del core `sub_listar_planes`, `sub_cotizar_inscripcion`, `sub_confirmar_inscripcion` y `sub_inscribir`, que despachan a la capability de inscripción. El modelo solo manda el código del plan, el código de promoción, los datos y el monto que el cliente confirmó; el cliente sale del contexto y el precio, la periodicidad y el descuento los calcula el servidor (el monto se compara, nunca se usa). Solo las ofrece el motor de procedimientos.
- El botón de confirmación muestra el resumen del servidor (plan, descuento, total del primer cargo, datos y términos). `register_resumen` acepta tools del core, no solo las del propio pack.
- `sub_cotizar_cambio_plan` y `sub_aplicar_cambio_plan` aceptan `codigo_promocion`; el código viaja al proveedor solo si el cliente lo trajo. `cotizar_inscripcion` recibe el `customer_id` para el límite por cliente.
- El verificador de montos conoce `monto_a_pagar` y `precio_nuevo` como dinero.
- La plantilla «Suscripciones» incluye el procedimiento; los playbooks `planes` y `suscripciones` dejan de decir que el plan es un producto de commerce. Los escenarios de QA apuntan al id nuevo.
- Queda igual hasta el PR-4: el procedimiento `pack_commerce.contratar_plan`, la plantilla «Ventas» y su pregunta `que_vendes` (panel y APK la envían; se quitan juntos), y los datos de prueba de planes en commerce.
- El motor gobierna las acciones del core de una etapa `risk: high` (`sub_inscribir`): sin tarea en foco se niegan, solo corren en su etapa y sus argumentos (plan, código, monto, datos del cliente) se atan a los datos de la tarea, sin pedir un segundo botón. `register_resumen` de una tool del core solo lo acepta el pack dueño de su prefijo (`TOOLS_DEL_CORE_POR_PACK`).

### Commerce sin planes (2026-10-30)

- SPEC-43 · HU-COM-17 (PR-4). El plan deja de ser un producto de `pack_commerce`: el catálogo de planes, la inscripción, el cobro y la activación viven en `pack_subscriptions`. `pack_commerce` sube a 3.0.0.
- Migración `crm_0017` (commerce, destructiva, con bajada que recrea las columnas vacías): da de baja (lógica) los productos y variantes que eran planes y desactiva las promociones que apuntaban a ellos; elimina `billing_period`, `trial_days` y `entitlements` de las variantes y `plan_activation_status`, `plan_activation_error` y `datos_alta` de los pedidos.
- Se retiran de commerce: el procedimiento `contratar_plan`, las tools `informar_plan` y `cotizar_plan`, la rama de plan de `crear_pedido`, `agregar_item_pedido`, `consultar_estado` y `verificar_pago`, los módulos `planes`, `altas` y `precios_de_plan`, los jobs `reintentar_activaciones` y `sincronizar_precios_de_plan`, el umbral `monto_alto_planes_threshold`, los datos de prueba y escenarios de planes y la clave `plan` de los campos de checkout. `payment.confirmed` con referencia `sub-<cargo>` se ignora sin registrar `payment_event_no_matching_order` (el cobro de una suscripción lo concilia `pack_subscriptions`).
- Contrato v1 de suscripciones contraído (migración `pack_subscriptions_0013`): `variante_id` pasa a `plan_id` en suscripciones, `CrearSuscripcionBody` exige `plan_codigo` y pierde `variant_id`, `nombre`, `precio`, `moneda`, `periodicidad` y `trial_dias`. `SubscriptionCapability` ya no tiene `activate` ni `PlanCatalog`.
- La plantilla «Ventas» ya no trae `contratar_plan`; `PlantillaAgente.preguntas_retiradas` lista ids de preguntas que el guion ignora en vez de rechazar con 422, así que el panel y la APK que aún envían `que_vendes` siguen funcionando hasta que lo quiten.
- No se re-grabaron cassettes ni journeys de `evals/`: cambian la descripción de `crear_pedido`/`consultar_inventario` y el playbook de ventas. Los journeys `procedimientos/01` y `02` apuntan a `pack_subscriptions.contratar_plan` pero sus pasos de verificación siguen mirando pedidos de commerce.
- `docs/openapi.json` se editó a mano (solo `VariantOut`, `ProductSave`, `OrderOut`, `CrearSuscripcionBody`, `SuscripcionItem`, `SuscripcionDetalle` y el filtro de `/suscripciones`); los tipos del panel y la APK no se regeneraron.
- Corrección en `core/billing/conciliacion.py`: la conciliación llamaba `plan_del_cargo(vendedor, plan_id)` con tres argumentos y fallaba en silencio; ahora usa los dos de la firma (`plan_id` de la vista del cargo).

### Cobro de plataforma y MRR con descuentos (2026-10-30)

- SPEC-43 · HU-SUB-36 (PR-2b-2b). `subscription.charge_paid` lleva `monto` (el neto cobrado), `monto_bruto` (precio de lista) y `descuento`. El cobro de plataforma (`core/billing/canales.py`) compara el precio de lista con el plan: un período pagado con descuento ya no dispara `monto_distinto`, se aplica al negocio y `SubscriptionPayment.amount_cop` queda con el neto; el audit de `cobro_plataforma_aplicado` suma `monto_bruto`, `descuento` y `monto_cobrado`. Una suscripción creada a mano con un precio menor y sin descuento sigue alertando.
- El alta y el cambio desde Free registran lo cobrado: `subscription.activated` ya traía `monto_cobrado` y `registrar_periodo_pagado` recibe ahora `monto`.
- La conciliación diaria compara contra el precio de lista (`monto_bruto`) y el `SubscriptionView` expone `monto_bruto` por cargo: un cargo pendiente con descuento ya no se marca `monto_distinto`.
- Métricas de suscripciones: `mrr_descuento` y `suscripciones_con_descuento` (activas de pago con descuento y períodos por consumir; porcentaje o monto fijo normalizado a mes). El MRR sigue nominal. El Resumen muestra «Descuento vigente» solo si hay alguno.

### Perfil del gestor en Equipo (2026-10-30)

- ADR-0176, HU-ACC-07. Hook opcional `BaseModule.collaborator_profiles(tenant_id, admin_ids)` (por lote, solo lectura) y modelo `PackProfile`: el core pide a los packs habilitados los perfiles de los colaboradores de la página y los agrega al listado, sin leer tablas de ningún pack. Una llamada por pack con todos los ids, en paralelo y con tope de 1 s; si un pack falla o se pasa, el listado sale sin sus perfiles (`log.warning`).
- `GET /api/v1/admin/collaborators` suma `pack_profiles` a cada cuenta (aditivo; vacío si no tiene perfil). El core descarta `resumen` y `edit` para quien no es dueño ni master ni tiene acceso a una sección del pack.
- Cobranza implementa el hook con tres consultas por lote: el perfil de gestor trae estatus, carteras `{id, nombre}` y permisos de campo activos (de 14), con `edit.kind = cobranza_gestor_permisos`. Sin migración.

### Eventos de acceso ampliados y límites de equipos (2026-10-30)

- ADR-0174, HU-ACC-06. `login_fallido` y `login_bloqueado` quedan en cada negocio de la cuenta (tope 10) desde `POST /api/v1/auth/login`, el login unificado y el login por negocio de la app de campo, y el login v1 (código + API key, canal `campo_v1`). Un identificador que no es cuenta no escribe filas; el detalle no lleva el identificador. Dedupe de 5 min por negocio, cuenta, equipo, tipo y canal (las repeticiones se anotan en el siguiente evento).
- El login unificado de campo (`POST /api/pack/pack_cobranza/auth/login`) ahora tiene bloqueo progresivo (5 fallos en 10 min → 15 min, 429).
- Límites de `device-session`: tope de 10 equipos vigentes por cuenta (429 `demasiados_equipos`) y 30 consultas por minuto por cuenta (429 `demasiadas_solicitudes`).
- Asignar el rol `gestor` a un colaborador baja a `pendiente` sus equipos que entraron por política abierta (los aprobados por una persona se respetan); cambiar la política (`PUT /access/policy`) invalida la caché de vigencia de todo el negocio; el tester habilitado se trata como master en `device-session`.
- Tarea diaria `core.access_events_retention`: baja lógica de los eventos de más de 180 días. Sin migración.

### WhatsApp Web (QR): el conector se reinicia solo cuando se agota su pool de hilos (2026-10-29)

- **Síntoma:** el agente "respondía" en el panel pero al cliente no le llegaba nada: los mensajes quedaban en
  `queued` y el worker fallaba cada envío con «WhatsApp (waweb) no está conectado para este negocio».
  Los mensajes entrantes seguían llegando. Se recuperaba solo reiniciando el conector, y volvía a pasar
  unos 35–45 min después.
- **Causa:** en neonize 0.5.2 cada ciclo de QR de una sesión sin vincular deja un hilo del executor por
  defecto bloqueado dentro de `Neonize()` (Go); `disconnect()`, `stop()` y cancelar `connect_task` no lo
  liberan. Con `min(32, CPU + 4)` hilos (12 en test) el pool se agotaba en unos 9–12 ciclos; entonces
  ninguna llamada al puente arrancaba, el drain del negocio dejaba de refrescar `waweb:ready` (TTL 120 s)
  y la API rechazaba cada envío. Ningún watchdog lo veía: la sesión seguía `connected` en la base y la
  sesión que cicla sí "progresaba".
- **Cambio:** `WaWebConnector._check_executor` lanza un no-op en el executor en cada barrido; si no arranca
  en 10 s dos veces seguidas, alerta (`critical`) y reinicia el proceso (Docker lo levanta y las sesiones
  vinculadas se restauran del blob guardado). El executor por defecto del conector pasa a 64 hilos para
  espaciar esos reinicios. No se arregla la fuga (es de neonize): ver el seguimiento en el PR.

### Resumen: ventana de 7/15/30 días y serie diaria por agente (2026-10-29)

- `GET /api/v1/admin/dashboard/agents?dias=7|15|30` (ADR-0171, adenda). La ventana aplica a los KPIs,
  sus textos (`Últimos N días`, `los N días anteriores`) y la ventana previa; un valor fuera de
  7/15/30 devuelve 422.
- Cada agente trae `serie[]` diaria cero-rellena (`fecha`, `turnos`, `conversaciones`, `costo`) en la
  zona horaria del negocio y `ultima_actividad`. El costo de la serie es `null` sin la sección `costs`.
- Sin tablas ni migración: los campos son aditivos y una app vieja sigue recibiendo 7 días.

### Clientes de Ventas: contadores por etapa, orden y última actividad en `GET /admin/leads` (2026-10-29)

- **HU-CRM-17 (lectura):** la lista de clientes acepta `facetas=<clave>` (repetible) y responde, por cada una,
  cuántos clientes hay por opción (`counts`), cuántos no tienen valor (`sin_valor`) y el `total`. Se calcula con
  una sola consulta agrupada por clave, respeta la búsqueda y los demás filtros pero no el propio (las pestañas
  de un mismo filtro suman contra el mismo total). Una clave sin contadores responde 400 `filtro_invalido`.
- **Orden:** `orden=alta|nombre|nombre_desc|actividad` (por defecto `alta`, como hasta hoy). `actividad` ordena
  por el último mensaje de cualquiera de sus conversaciones y deja al final a quien nunca escribió. Un valor
  fuera de la lista responde 422.
- **Fila:** cada cliente trae `ultima_actividad` (hora del último mensaje o `null`).
- **Contrato de packs:** `lead_registry.register(..., facet_query=...)` permite que un pack declare cómo contar
  sus filtros sin que el core conozca sus tablas. `pack_commerce` lo declara para `estado_lead` y
  `clasificacion`.
- Compatible hacia atrás: sin los parámetros nuevos la respuesta solo suma `facetas: []` y `ultima_actividad`.
  No hay migración.

### Ficha del cliente en la APK (2026-10-29)

- Clasificación general: nueva opción `Nuevo` en el perfil del cliente.
- Avatar del cliente: `POST/DELETE /admin/leads/{lead_id}/avatar`, con almacenamiento por negocio y URL firmada en la ficha.
- Geocodificación inversa: `POST /admin/leads/{lead_id}/geocodificar/reverso` propone dirección, municipio, departamento y país desde un punto del mapa, sin escribir el perfil.

### QR de WhatsApp Web: conector colgado, tmpfs sin permisos y sesión duplicada (2026-10-28)

- El conector ya no queda mudo para siempre: si un tenant pasa 5 min sin publicar QR ni cambiar de
  estado, se alerta y se reinicia el proceso. Neonize puede dejar una llamada al puente Go trabada y
  `disconnect()` esperaba el lock sin fin; cancelar la tarea no recupera el puente.
- `disconnect()` acota solo la espera del lock (no cancela la llamada en curso, que provocaría
  `concurrent map writes` en Neonize).
- El `tmpfs` del connector debe montarse con `mode=1777`. Si la raíz de sesiones no es escribible, se
  usa una raíz efímera en `/tmp` y se registra `waweb_session_dir_no_escribible`; antes el vínculo
  fallaba con `PermissionError` y el panel se quedaba en "esperando el QR".
- `set_status` escribe en la sesión de la conexión activa: con una sesión huérfana el panel leía una
  fila y el connector actualizaba otra. La migración `0118` limpia duplicados y crea un índice único
  parcial para impedir que vuelva a haber dos conexiones `waweb` vivas por negocio.
- El QR se conserva 240 s en Redis para cubrir el vencimiento (90 s) más la reconexión.

### Test de puertos libres hermético (2026-10-28)

- `test_find_free_port_evita_puertos_de_contenedores_parados` ya no depende de que el puerto 49302 esté libre en el host: parchea `port_available`. Fallaba de forma intermitente en el CI al correr `rapido` a la vez que los contenedores de integración.

### Prueba del corte de onboarding: el «mañana» ahora se calcula en UTC (2026-10-28)

- `test_abre_solo_al_propietario_y_los_negocios_anteriores_al_corte_no_aplican` fijaba el corte con
  `date.today() + 1 día` (hora local del runner), pero `aplica_al_negocio` compara con la fecha UTC de
  `created_at`. Entre las 00:00 UTC y el fin del día local el negocio recién creado seguía «aplicando» y
  el job `integracion` salía rojo. Solo cambia la prueba; el código de producción no se toca.

### Tests del catálogo de opciones sin conteos fijos (2026-10-28)

- `test_cfg02_catalogo_api` y `test_cfg02_registro_opciones` calculan cuántas opciones esperan a partir de `OPCIONES` en vez de literales (57/43, 27+16, 32+19+6). Cada opción nueva del registro los rompía y dejó `develop` en rojo tres veces, con una vuelta de CI de ~20 min cada una.

### Runbook: el alta manual desde el panel y su correo de acceso (2026-10-28)

- `docs/runbooks/altas-en-revision.md` gana la § 10 «Negocio creado a mano desde Plataforma» (ADR-0175):
  estados del envío (`enviado`, `sin_smtp`, `fallido`, `ya_emitida`, `ya_es_miembro`), «Reenviar acceso»,
  los errores 409/400 del alta y del reenvío, `PANEL_BASE_URL` y el límite conocido del reenvío por tester.
  Las §§ 5 y 6 y las referencias a «crear el negocio a mano» apuntan a ella.
- ADR-0175 pasa de «propuesto» a «aceptado». Solo documentación: sin cambios de código ni de contrato.

### Promociones de plan en suscripciones: descuento del primer cobro (2026-10-28)

- SPEC-43 · HU-SUB-33 (PR-2b-1). Migración `pack_subscriptions_0011`: tablas `pack_subscriptions_promociones` y `pack_subscriptions_canjes` (RLS forzada, únicos parciales por cliente, persona y cargo) y las columnas `monto_bruto`, `descuento`, `canje_id`, `periodo_n` y `consume_periodo` de `charges`. `monto` sigue siendo el neto que se cobra; `valor_periodo` el precio de lista.
- `inscribir(…, codigo_promocion)` y `cotizar_inscripcion(…, codigo_promocion)` descuentan el primer cobro y reservan el canje en la misma transacción; `change_plan(…, codigo_promocion)` solo aplica a un cambio `desde_cero` (Free → pago), si no responde `promo_no_aplica_a_cambio`. `monto_esperado` se compara con el neto; `monto_alto_inscripcion` sigue sobre el precio de lista.
- El pago del neto confirma el canje; todo camino que anula un cobro (expiración, reemplazo, cancelación, cambio de plan reemplazado o cobrado fuera de período) lo libera y devuelve el cupo. Cancelar una Free con un cambio de plan pendiente ahora anula ese cobro. El último cupo lo gana uno solo (UPDATE atómico). Un código inválido responde siempre `codigo_no_valido`; los intentos tienen tope por hora (`demasiados_intentos`, 429).
- API: `GET/POST/PUT/DELETE /promociones` y `GET /promociones/{id}/canjes`; `POST /suscripciones` acepta `codigo_promocion`. Por ahora la API solo acepta promociones de 1 período (el de N llega con HU-SUB-35).
- `MONEY_KEYS` incluye `precio_lista`, `descuento`, `total_primer_cargo`, `precio_periodo_siguiente` y `monto_bruto`: sin ellas el verificador de montos bloqueaba respuestas con descuento.
- Corrige un error del PR-2: inscribir desde el panel sin `datos_alta` guardaba el JSON `null` y el CHECK de objeto lo rechazaba (500).

### Planes e inscripción en suscripciones (2026-10-28)

- SPEC-43 (D-55, ADR-0177), PR-2. `pack-subscriptions` es dueño del catálogo de planes (`/planes`, CRUD con auditoría, D-16 en el esquema) y de la inscripción de un cliente en un plan: Free nace `trialing`/`activa`, un plan de pago nace `pendiente_pago` con su cobro y link, y se activa con el pago (webhook o confirmación manual del panel) por un único camino. La inscripción repetida o simultánea devuelve la misma; una `pendiente_pago` expira a los 7 días y un pago tardío o a un cargo anulado no activa nada y se avisa.
- `pendiente_pago` no es una suscripción viva: queda fuera de MRR, altas, churn, cohortes, mora y crons. Las métricas suman `pendientes_de_pago`.
- Nuevos endpoints: `GET/POST /planes`, `PUT/DELETE /planes/{id}`, `POST /suscripciones` con `plan_codigo` (el camino de plan libre sigue) y `POST /suscripciones/{id}/confirmar-pago`.
- Alta automática: el pack registra su propia fuente de altas (`DatosDeAltaDeLaInscripcion`); `subscription.activated` lleva `plan_id`, `numero_publico` y `cambio_de_plan`; el consumer pregunta a todas las fuentes del negocio, trata un Free que paga como cambio de plan y manda a revisión el alta con origen panel.
- Siembra de planes desde `planes.json` (idempotente, todo o nada) y cron `sincronizar_precios_de_plan`; el seed genérico lee `archivos_de_seed` de cada pack.
- Migración `pack_subscriptions_0010` (**borra suscripciones y cobros de test**; precheck de conteo en `docs/runbooks/planes-e-inscripcion-despliegue.md`). Aditivo: `activate`, `plan_catalog_registry` y las ramas de commerce siguen vivas hasta el PR de commerce sin planes.

### Pedidos: filtros, paginación y enlace al cliente (HU-CRM-20, 2026-10-28)

- `GET /pack/commerce/orders` acepta `estado`, `pago` (pendiente, pagado, rechazado), `q` (número de pedido o nombre del cliente),
  `customer_id`, `limit` y `offset`, y conserva la forma de lista (sin parámetros devuelve lo mismo
  que antes: hasta 200 pedidos). El total de los que coinciden viaja en la cabecera `X-Total-Count`.
  Un `estado` fuera de catálogo responde 400 `invalid_status`.
- `OrderOut` incluye `customer_id` y `customer_name`, para que el panel enlace a la ficha del cliente.
- Ya no se leen todos los ítems y variantes del negocio en cada listado: solo los de la página.
- Migración `crm_0016`: índice `(tenant_id, customer_id)` en `pack_commerce_orders`.
- `GET /admin/escalations`: cada escalación trae `customer_id`, para que el panel abra la ficha del cliente.
- El aviso «Lead nuevo» apunta al cliente (`target.entity = "cliente"`, id del cliente) y no al id del lead: la ficha se abre por cliente, así que el deep link de la APK no podía abrirla. El id del lead sigue en `data.lead_id`.

### Opciones directas: el texto corto ya no paga la bandera (2026-10-28)

- SPEC-40 · C2 (seguimiento del arquitecto sobre #624). La bandera `opciones_directas.habilitada` sale de la fila de
  `tenants` que `handle_inbound` ya lee para el gate «negocio operativo» (`tenant_operativo`, `cargar_tenant_activo`),
  en vez de abrir una sesión y consultar `tenants.settings` por cada texto corto en web, QR, Messenger e Instagram.
  Bandera apagada: cero consultas extra por mensaje (antes una sesión). Encendida y el último saliente no es un menú:
  una consulta (antes dos). Sin caché ni TTL: apagar la bandera rige desde el siguiente mensaje y no hay estado entre
  negocios. `is_tenant_active` e `is_tenant_operational` conservan firma y semántica. Sin migración ni cambio de API.

### Opciones directas: probar una plantilla con botones en el playground, sin Meta (2026-10-28)

- SPEC-40 / ADR-0172, pista B1. El canal `test` (playground) cuenta como canal con botones: `decidir_via` lo
  manda por la vía interactiva (o texto si no cabe en los topes) sin ventana de 24 h ni plantilla, y
  `send_to_conversation` / `send_interactive_to_conversation` guardan el mensaje como entregado, sin Celery ni
  `ON_MESSAGE_SENT` (mismo patrón que `playground._guardar_mensaje`). `enviar_confirmacion` del toque también
  acepta `test`, para que `optc`/`optn` funcionen. Los demás canales no cambian.
- Rutas nuevas: `POST /api/v1/admin/playground/options` (`{clave}`; envía la versión vigente a la conversación de
  prueba) y `POST /api/v1/admin/playground/tap` (`{payload}`; mismo resolver y misma firma que WhatsApp).
  Devuelven los mensajes producidos para que el panel los pinte. Dueño o master con escritura en `connections` y
  bandera `opciones_directas.habilitada` encendida (404 si no). **La tool se ejecuta de verdad** contra los datos
  del negocio.
- Sin migración.

### Opciones directas: etiqueta `via` en las métricas del toque y journey de Instagram (SPEC-40, C2, 2026-10-28)

- `option_taps_total` y `option_tap_latency_seconds` llevan la etiqueta `via` (`button` | `text`), para separar los toques de botón de las respuestas a un menú numerado. Sin cambio de comportamiento ni de contrato.
- El journey del menú numerado cubre también Instagram (`instagram`/`meta`): «1» ejecuta la opción una vez, con el LLM en cero.

### Opciones directas: «1» o el título contestan un menú numerado (SPEC-40, pista C2, 2026-10-28)

- En los canales de texto con la capacidad `respuesta_numerica` (WhatsApp por QR, chat web, Messenger, Instagram; no Meta, correo ni playground), la respuesta «1» (o «1.», «1)») o el título exacto de una opción de un menú numerado (`option_sends.via='text'`) se resuelve **antes del LLM** como si hubiera tocado el botón. Guardas: bandera `opciones_directas.habilitada` encendida, menú vigente (`expires_at`) y último mensaje saliente de la conversación, coincidencia exacta; cualquier otra cosa sigue al agente como hoy.
- Solo acciones no sensibles: una opción que pide confirmación no se ejecuta por texto (texto fijo `sensible_por_texto`, sin activación ni tool).
- El `opt:` lo firma el servidor; reutiliza el resolver (`Toque.via = "text"`), la idempotencia `dedupe_key` y la auditoría `option_tap`, que ahora lleva `via` (`button` | `text`).
- Sin migración ni cambio de contrato. Con la bandera apagada (por defecto) no cambia nada para nadie.

### Mensajes guardados: respuestas rápidas del operador (ADR-0178, pista D4) (2026-10-28)

- Migración `0116`: tabla `mensajes_guardados` (clave única por negocio entre los vivos, título, texto con variables del dialecto común, canales como `ConnectionType.id` o todos, estado `borrador`/`activo`/`archivado`, borrado lógico; RLS forzada, sin DELETE para `app_runtime`). Aditiva y vacía; no toca `plugin_configs` ni datos existentes.
- API `/api/v1/mensajes/guardados` (lectura con `connections`; escritura con `connections` en escritura y dueño o master, como las plantillas con opciones; sin la bandera `opciones_directas`): listar, crear, leer, editar (`PATCH`), borrar, `vista-previa` y `catalogo-opciones`. El texto conserva los saltos de línea y solo admite `{cliente.nombre}` y `{negocio.nombre}`; `{{n}}` y cualquier otra variable se rechazan con 422 `variable_no_permitida`. Cada escritura queda en `audit_log` (`mensaje_guardado_*`) con la clave y el estado, nunca el texto.
- El catálogo de mensajes (`GET /api/v1/mensajes/catalogo`) suma `tipo=guardado` (estado, canales, variables y `fuente.editor=guardado`). Cambio ADITIVO del contrato: el panel debe ampliar el tipo `tipo`/`fuente.editor` (un `switch` exhaustivo sobre los cuatro valores anteriores dejará de compilar).
- Solo almacenamiento: nada envía todavía un mensaje guardado a un cliente. La doble lectura de los textos de los packs (`plugin_configs`, Fernet) queda como mejora futura (adenda del ADR-0178).

### Aviso por evento → plantilla con opciones, tras banderas (ADR-0178, pista D3) (2026-10-28)

- Un aviso por evento puede apuntar a una plantilla con opciones PUBLICADA del negocio: clave opcional `option_template` en `message_templates_map[<evento>]` (compatible hacia atrás). El aviso sale con sus botones, su lista o su menú numerado según el canal, por la cascada de C1/C2 (`enviar_opciones`): interactivo con la ventana de 24 h abierta, plantilla de Meta aprobada `<clave>_v<n>` con la ventana cerrada, menú numerado de texto en QR, chat web, Messenger e Instagram. El menú ES el aviso (no se manda además el texto de respaldo) y D3 es el primer emisor fuera de turno que C2 esperaba.
- Detrás de las DOS banderas del negocio, ambas apagadas por defecto y solo del master: `avisos_por_canal` (D2) y `opciones_directas`. Con cualquiera apagada el comportamiento es idéntico al de antes (prueba de regresión de los 8 eventos). Nada se enciende en ningún negocio.
- Si no puede salir (plantilla sin publicar, retirada, borrada o de otro negocio; contexto incompleto; sin plantilla de Meta aprobada; QR con la política en contra; Messenger/Instagram sin ventana; conversación con actividad en los últimos 15 min en un canal de texto) cae al texto de D2 o a lo de siempre, sin dejar un mensaje `failed` de más (`enviar_opciones(..., sin_mensaje_de_fallo=True)`) y con el log `event_notice_opciones_fallback` y la métrica `event_notice_options_fallback_total{canal,outcome}`. Un envío ya hecho (reintento del outbox) no se duplica; un resultado ambiguo (`internal_error`) se reintenta en vez de caer al texto.
- Cobranza EXCLUIDA (Ley 2300, HU-SUB-18): `charge_created`, `charge_reminder` y `paused` no pueden mapearse a una plantilla con opciones en ningún canal (el PUT lo rechaza y el envío lo ignora si el mapa se guardó a mano): la entrega no puede garantizar el horario legal por todos los caminos.
- `PUT /admin/settings/message-templates` valida `option_template` (forma, existe en el negocio, publicada, versión vigente no retirada, no borrada, y que los datos que cita la plantilla los tenga el evento); omitir el campo conserva lo guardado y `""` lo borra.
- `RUTAS_DE_DATOS` (plantillas con opciones) deja de estar vacía: son las rutas de los eventos que no son de cobranza (`cita.*`, `suscripcion.concepto`, `prueba.termina`); la invitación del comprador (`provisioning.invitation`) tampoco admite plantilla con opciones (sus datos son del comprador); ninguna es un id. `EVENTOS` y `COBRANZA` pasan a `core/kernel/mensajes/eventos.py` (se reexportan desde `event_notices` y `avisos_canal`). `GET /plantillas-opciones/catalogo-variables` ahora las lista con origen `servidor`.
- OpenAPI: campo `option_template` en `EventTemplateEntry`. Sin migración.

### Avisos por evento por canal de texto, tras bandera (ADR-0178, pista D2) (2026-10-28)

- Los 8 avisos por evento (`EVENTOS`, ADR-0108) solo salían por conversaciones de `whatsapp`: en chat web, Messenger o Instagram se perdían en silencio, y por QR dejaban un `sin_plantilla` engañoso. Con la nueva bandera por negocio `tenants.settings.avisos_por_canal` (APAGADA por defecto; solo la enciende el master con `PUT /admin/platform/tenants/{id}/avisos-por-canal`, auditada como `avisos_por_canal_bandera`) el aviso también sale como texto por QR, chat web, Messenger e Instagram.
- Texto por canal: clave opcional `fallback_text_by_channel` en `message_templates_map[<evento>]`, por tipo de conexión (`whatsapp_waweb`, `webchat`, `messenger_meta`, `instagram_meta`), con `{ruta}` del evento (mismo dialecto y saneo que `fallback_text`, módulo `core/kernel/mensajes`). El texto general de WhatsApp NO se usa en otros canales. `PUT /admin/settings/message-templates` lo valida (claves, 1000 caracteres, rutas permitidas del evento) y lo conserva si el cuerpo no lo manda.
- La vía sale de la tabla de capacidades por canal de C1 (`core/gateway/avisos_canal.py`): WhatsApp Meta, igual que siempre; sin plantillas y con texto definido → texto; sin texto definido → no se envía, `event_notice_omitido_por_canal` en el log y la métrica `event_notice_omitted_total{evento,canal,motivo}`.
- Políticas que no se saltan: QR exige `waweb_mode = allow_outbound` (un aviso no es una respuesta; el modo por defecto es «solo respuesta»), cupo del día y sin pausa (se mira antes de encolar: el conector difiere un mensaje bloqueado hasta ~24 h); los eventos de cobranza (`charge_created`, `charge_reminder`, `paused`) NO salen por QR porque no se puede garantizar el horario legal de HU-SUB-18 en la entrega. Messenger e Instagram solo con la ventana de 24 h abierta. El horario de emisión de la cobranza sigue siendo el de `pack_subscriptions`.
- Con la bandera apagada el comportamiento es idéntico (el cuerpo de `send_event_notice` no cambió; solo cuesta una lectura de `tenants.settings` por aviso). `send_event_notice(..., por_canal=False)` es el valor por defecto.
- OpenAPI: ruta nueva `/admin/platform/tenants/{id}/avisos-por-canal` y campo `fallback_text_by_channel` en `EventTemplateEntry`. Sin migración.

### Módulo común de variables y render de mensajes (ADR-0178, pista D1) (2026-10-28)

- Nuevo `core/kernel/mensajes/` (puro: sin base, red ni imports del gateway, el orquestador o los packs): `clean_param`, resolución de rutas con lista permitida (`resolver_ruta`, `RutaNoPermitida`, `DatoFaltante`), el render de cada dialecto (`render_fallback`, `render_llaves`, `render_meta`), la lectura de componentes de una plantilla de Meta y un conversor entre `{a.b}`, `{x}` y `{{n}}` (`variables_de`, `a_meta`, `a_llaves`).
- `gateway/event_notices.py` y `kernel/opciones/{contexto,mapeo,argumentos,definicion,envio}.py` lo usan; los nombres públicos de siempre (`clean_param`, `MAX_PARAM`, `render_fallback`, `render_body`, `template_placeholders`, `RUTA_VARIABLE`, `LLAVES`, `mapeo.renderizar`…) siguen funcionando. `kernel/opciones` deja de importar del gateway para sanear (salvo `build_components` y `render_body` en `mapeo.py`, que trabajan sobre la fila `MessageTemplate`).
- Sin cambio de comportamiento, de OpenAPI ni migración. Los textos de los packs (`str.format`) no se tocan. Tests golden contra copias textuales del código anterior.
- Única diferencia consciente: `mapeo.cuerpo_para_meta` ahora deja intacto un `{{1}}` que ya viene en formato Meta (antes lo convertía en `{{{1}}}`); es inalcanzable porque el lint rechaza las llaves sueltas del cuerpo. Un test la fija.

### Integración en paralelo con pytest-xdist (2026-10-28)

- `pytest tests/integration -n 3 --dist loadfile` en el CI: cada worker clona la base migrada (`agent_core_gwN`) y usa su propia base de Redis y su `TENANTS_ROOT` (`tests/integration/_aislamiento_xdist.py`). La corrida serial no cambia. Medido: 23 min en serie → 5–10 min según la carga del host.
- Adenda de ADR-0120.

### Importar datos existentes por agente desde un CSV (2026-10-28)

- En test (`QA_TESTERS_ENABLED`), `GET …/admin/agents/{id}/importar-datos` (+ `/plantilla.csv`), `POST …/validar`,
  `POST …/importar-datos` y `DELETE …/importar-datos` importan productos, tipos de cita, clientes y préstamos
  (sin teléfono) y planes desde un CSV, con vista previa que muestra los errores fila por fila (HU-QA-12, ADR-0180).
- Idempotente por clave natural: lo que ya existe se omite. Lo importado es un lote propio
  (`test_data_records.origen = 'importacion'`) que «Generar datos de prueba» no toca.
- Migración 0119: el CHECK de `test_data_records.origen` admite `importacion`.
- Retirar datos de prueba libera el SKU de las variantes de Comercio (`<sku>~retirado-<id8>`), para poder volver a importar.
- Una cartera de cobranza retirada ya no deja clientes vivos huérfanos.

### Cobranza: el deudor cargado a mano se reconoce por teléfono (2026-10-28)

- HU-CRM-15 (SPEC-33, ADR-0179 §7). `Cliente.customer_id` ahora se enlaza con el `Customer` de WhatsApp cuando
  coinciden los dígitos del teléfono (`pack_cobranza/enlazar.py`): antes ningún código lo asignaba y un deudor
  creado en el panel o en la app del gestor no lo encontraban las tools `cob_*` cuando escribía por WhatsApp.
- Tres entradas: perezoso en `_cliente_de_customer` (caché negativo de 10 min en Redis; si Redis falla se
  intenta igual), al crear el cliente (panel, app del gestor y alta SSR; el teléfono se guarda en E.164) y el
  backfill `python -m pack_cobranza.enlazar [--dry-run] [--tenant] [--desde-id] [--lote]`, idempotente y reanudable.
- Una coincidencia enlaza con `UPDATE … WHERE customer_id IS NULL`; dos deudores con el mismo teléfono no se
  enlazan (`ambiguo`) y un `Customer` ya enlazado no se reasigna. El log `cobranza_enlace{resultado, origen}`
  no lleva teléfono, documento ni nombre.
- Limitaciones: solo se enlazan Customers de WhatsApp (Instagram, web y otros canales no), y un número local
  solo cruza con un `wa_id` internacional si es del país por defecto (CO; aún no hay país por negocio).

### Datos de prueba por módulo

- `GET/POST/DELETE /api/v1/admin/datos-de-prueba/{modulo}` generan y retiran el lote de Ventas,
  Agenda, Cobranza o Suscripciones sin depender de la ficha de un agente (sirve también para el
  onboarding).
- Suscripciones crea planes en Comercio y, con su snapshot, clientes, suscripciones y cargos en
  el pack de Suscripciones.
- El retiro por módulo alcanza también los lotes generados desde los agentes.
- Comercio ahora hace borrado lógico de productos: antes el botón desactivaba variantes y
  borraba categorías, pero los productos seguían apareciendo en el panel.

### Aviso de tratamiento de datos y consentimiento del cliente (HU-CRM-16, ADR-0179 decisión 8)

- Migración 0117: `customer_consents` suma `version_aviso` y acepta el propósito `tratamiento_datos` con los estados `informado`, `otorgado` y `revocado` (la tabla sigue siendo de solo inserción). `downgrade` borra las filas de ese propósito (el CHECK anterior no las admite; exportarlas antes con `pg_dump -t customer_consents`).
- Nuevo `core/consentimiento.py`: estado del cliente frente a la versión vigente del aviso (`sin_aviso`, `informado`, `otorgado`, `revocado`). `informado` solo dice «ya se le mostró»; no es consentimiento. Quien dijo que no no recibe el aviso de nuevo aunque cambie el texto.
- Aviso por negocio en `behavior_config["datos_cliente"]["aviso"]` (activo + texto propio; sin texto usa un borrador **sin validar legalmente** de `playbooks/kernel/aviso-datos.md`). `GET/PUT /api/v1/admin/datos-cliente/aviso`: activarlo exige `confirmo_revision_legal` (P-37) y deja auditoría con el texto completo y su versión.
- **Apagado por defecto.** Con el aviso apagado nada cambia: el perfil se escribe igual que antes y el loop no hace ninguna consulta extra.
- Con el aviso activo: el sistema (no el modelo) anexa el aviso a la primera respuesta útil de cada conversación, una vez por versión y bajo candado por cliente; el canal de prueba no lo muestra. La tool `responder_aviso_datos(decision)` solo registra la respuesta si hay un aviso pendiente de esa conversación y el último mensaje real del cliente dice «acepto» o «no» sin ambigüedad («ok» no basta).
- Puerta única de escritura `core/datos_cliente/guardar.py`: sin aceptación se guarda solo lo operativo (los datos `requerido` de la acción en curso y los que el módulo declara) y el resto espera; `update_customer_profile` responde `requiere_aceptacion`, y `crear_lead` / `actualizar_lead` pasan por la misma puerta. Un test estático impide que otra tool de agente escriba el perfil por fuera. Las ediciones del operador desde el panel nunca se bloquean.
- Ficha del cliente: `GET /api/v1/admin/leads/{id}/consentimiento-datos` (estado e historial) y `POST …/revocar` (solo dueño o master): registra la revocación, retira los hechos del perfil, deja auditoría y avisa que la supresión dentro de los módulos queda pendiente de ADR-0164.
- Esto no afirma cumplimiento de ninguna ley: encender el aviso en un negocio real depende de la revisión legal del texto (P-37).

### Ficha de la relación del cliente en el prompt y tool de historial (HU-CRM-14, ADR-0179)

- Cada módulo (Ventas, Agenda, Suscripciones, Cobranza) aporta `LeadRelacion.ficha` y `detalle`. La ficha es una línea corta por módulo (p. ej. «2 pedido(s); el último OC-0012 está entregado»), sin montos, documento, correo ni dirección.
- Apagado por defecto (misma bandera `herramientas_del_agente` de la política de datos): ofrecer una tool nueva o cambiar el prompt invalida las grabaciones del gate de replay y cambia lo que ve el modelo en cada turno. El negocio lo enciende.
- `PromptContext.relacion`: la ficha entra al bloque dinámico después de lo que sabemos del cliente, nunca al prefijo cacheado. Topes: 4 módulos, 240 caracteres por línea, 1.600 el bloque, 500 ms por módulo (un módulo lento o roto se omite y el turno sigue). Solo módulos habilitados para el negocio. Rótulos en el playbook del kernel `relacion-con-el-negocio`; cada línea se escapa y delimita como dato.
- Nueva tool de solo lectura `consultar_historial_cliente(modulo?, limite ≤ 10)`: detalle del cliente de la conversación (nunca un id que mande el modelo), con importes en las claves de dinero del core donde existen.

### Política de datos del cliente por módulo (HU-CRM-13, ADR-0179)

- Nuevo `core/datos_cliente/`: cada módulo registra sus acciones (`ventas.pedido_domicilio`, `ventas.pedido_retiro`, `ventas.plan_legado`, `suscripciones.inscribirse`, `agenda.agendar_cita`) con los datos del cliente que pide y el nivel de cada uno (`requerido`, `recomendado`, `no_pedir`). Precedencia: ajuste del dueño > configuración del módulo (`checkout_fields`, `campos_de_inscripcion`) > defecto de la acción. El correo de las citas es un piso técnico que el negocio no puede bajar.
- `GET/PUT /api/v1/admin/datos-cliente/politica` (sección Configuración): lista la matriz del negocio y guarda solo lo que difiere del módulo, con bloqueo de fila y auditoría del antes y el después. Los ajustes viven en `behavior_config["datos_cliente"]["politica"]`.
- Ventas y Suscripciones aplican el ajuste sobre su lista de campos requeridos; sin ajustes la lista no cambia. Las respuestas de Ventas, Agenda y Suscripciones suman el sobre `faltan_datos` (`faltan`, `recomendados_pendientes`, `ya_registrados` solo con claves) junto a los campos de siempre.
- Nueva tool de solo lectura `consultar_datos_cliente(accion?)`: la política llega al agente por la tool y no por el prompt, así que el prefijo cacheado no cambia cuando el dueño ajusta un nivel. Apagada por defecto: el negocio la enciende con `herramientas_del_agente` en el PUT (ofrecer una tool nueva cambia lo que ve el modelo en cada turno).

### HU-CRM-12 · iconos veraces y filtro por agente en Clientes (backend)

- `LeadRelacion` en el `lead_registry` (ADR-0179): un pack declara `con_datos(ids)`, el SQL que define «este cliente tiene datos reales del módulo». Commerce = pedido, cotización o un lead con contenido (nombre, teléfono, correo o interés: el borrador vacío de `auto_create_lead` no cuenta, pero uno ya completado sí); Agenda = cita no borrada; Suscripciones = suscripción no borrada; Cobranza = cliente de cartera enlazado. `ficha` y `detalle` quedan declaradas para HU-CRM-14.
- La columna `agentes` de `GET /admin/leads` y de su CSV deja de mentir: Ventas ya no sale en todos los clientes (el resolver devolvía «Nuevo» y 0 pedidos a cualquiera). Con `relacion` manda `con_datos`; sin ella se conserva la heurística de las columnas. General sigue siempre presente. `agents[]` no cambia.
- `agente=<key>` (aditivo) en `GET /admin/leads` y `GET /admin/leads/export.csv`: solo los clientes con datos de ese agente. `general` no filtra; una clave desconocida, de un pack no habilitado o sin `con_datos` responde 400 `filtro_invalido`.

### CI del backend sin el job `nivel` (2026-10-28)

- `rapido`, `integracion` y `evals-replay` arrancan sin esperar a un job intermedio que hacía cola de runner por 10–30 s de trabajo. El nivel se lee del evento y las rutas relevantes las calcula el primer paso de `integracion`. Adenda de ADR-0120.

### Replay de evals dentro de `integracion` (2026-10-28)

- El job `evals-replay` desaparece: el gate (12 s) ocupaba un hueco de runner entero por ~190 s. Ahora es el último paso de `integracion`, contra la base plantilla que los workers de xdist no tocan. Adenda 4 de ADR-0120.

### CI del backend con menos espera (2026-10-28)

- Job `nivel` aparte: `rapido`, `integracion` y `evals-replay` arrancan a la vez en vez de en cadena.
- Gitleaks con binario cacheado en el host (~30 s menos por corrida).
- Unitarios con `pytest -n 4` (pytest-xdist). Los ids de `parametrize` de `test_opciones_payload`, `test_purga_redis_claves` y `test_referencias_vigentes_cotizacion` pasan de `uuid4` a `uuid5` fijo para que la colección sea idéntica en cada worker.
- La integración sube `--durations=40` y el junit como artifact para decidir cómo repartirla. Adenda de ADR-0120.

### Catálogo unificado de mensajes, de solo lectura (2026-10-28)

- `GET /api/v1/mensajes/catalogo` lista juntos, con una forma común y un puntero al editor de origen, los mensajes del negocio: plantillas de Meta, avisos por evento, plantillas con opciones y textos de los packs. Cada tipo solo se incluye si quien consulta puede leer su sección (`connections`, `settings`, `plugins`); lo omitido se informa en `omitidas`. No migra datos, no cambia comportamiento y no sincroniza con Meta.
- Los packs marcan sus campos de mensaje con `json_schema_extra={"mensaje": "<canal>"}` (calendario, suscripciones y comercio); el core no conoce sus nombres. Un test falla si un campo nuevo con `{variables}` queda sin marcar.
- Servicio `core/admin/catalogo_mensajes_service.py`; `canales_de(tipo, marca)` es el único punto que sustituirá la tabla de capacidades por canal (pista C1).
- ADR-0178 (propuesto) en solvershub-docs.

### Dos plantillas a la vez ya no pierden la habilidad de la otra (2026-10-28)

- Elegir dos plantillas a la vez en un negocio sin habilidades ya no deja un agente sin su habilidad ni
  responde `template_unavailable`: `asegurar_habilidades` leía el spec, y si la otra petición confirmaba su
  versión justo después, la segunda guardaba encima y borraba la habilidad de la primera. `save_spec_version`
  acepta `base_id` (guarda solo si la fila de la que se partió sigue activa) y la habilitación lo usa; quien
  pierde relee y reintenta, o responde el 409 `concurrent_edit` reintentable de siempre.

### Tabla de capacidades por canal (2026-10-28)

- Pista C1 de SPEC-40: `core/gateway/connection_types.py` declara por tipo de conexión qué admite (`botones`, `lista`, `plantilla_aprobada`, `ventana_24h`, `respuesta_numerica`) y expone `capacidades_de(canal, proveedor)`. Sustituye los `if channel == ...` de `decidir_via`, `send_interactive_to_conversation`, `opciones_toque.enviar_confirmacion` y de la vía de plantilla en `opciones_envio`, y el catálogo de mensajes (`canales_soportados`) la consulta en vez de su constante. Sin cambio de comportamiento ni de contrato; sin migración. Un tipo de conexión con canal que no figure en la tabla hace fallar `tests/test_capacidades_canal.py`.

### ADR-0179: datos del cliente por módulo (2026-10-28)

- Propone el ADR-0179 (SPEC-33, HU-CRM-12 a 16): política de datos requeridos por módulo y acción con ajuste del negocio, respuesta única `faltan_datos`, `LeadRelacion` para iconos veraces y ficha de relación en el prompt, enlace de cobranza con el cliente del chat por teléfono y aviso de tratamiento de datos que no bloquea la respuesta. Enmienda ADR-0153, ADR-0114 y ADR-0171 (resumen por agente) y generaliza ADR-0161. Solo documentación: no cambia código ni esquema.

### Resumen por agente: KPIs del negocio, de cada agente y de su módulo (2026-10-27)

- `GET /api/v1/admin/dashboard/agents` (ADR-0171). Devuelve los KPIs del negocio (conversaciones,
  escalaciones pendientes, tasa de escalamiento, consistencia, costo) y, por cada agente, su actividad
  (turnos, conversaciones, escalados, tareas abiertas, costo) con el valor de los últimos 7 días, el de
  los 7 previos, el delta y un destino semántico (`ruta` + `params`) para ver el detalle.
- Cada pack aporta los KPIs de su módulo con un `ResumenProvider` en `core/admin/resumen_registry.py`:
  Ventas (pedidos, ventas pagadas, por despachar, clientes nuevos), Agenda (citas de hoy, próximas,
  agendadas, canceladas), Cobranza (recaudo, cartera en mora, promesas) y Suscripciones (MRR, altas,
  bajas, cobros vencidos). Son del módulo completo, no solo de lo que creó el agente.
- Un KPI sin el permiso de su sección se omite (no sale en cero); un módulo que falla sale con `error`
  sin tumbar el resto. Sin tablas ni migración.

### Se quitan los «Cobros al propietario» de Cobranza (2026-10-27)

- Esa facturación la maneja el agente de suscripciones de solvershub. Se eliminan los endpoints
  `GET/POST /pack/cobranza/owner-charges`, `POST /pack/cobranza/owner-charges/{id}/payment` y
  `POST /pack/cobranza/owner-charges/{id}/links` (con sus schemas `OwnerCharge*`, `OwnerPayment`,
  `PaymentLink*`), las rutas y la plantilla HTML del admin viejo (`/cobranza/cobros*`) y el item «Cobros»
  del menú. **Mergear después de los PRs de panel y APK** (sus checks de schema leen este openapi).
- Se conservan: las tablas `pack_cobranza_cobros_propietario` y `pack_cobranza_links_pago` (los links de
  pago de cuotas y suscripciones siguen usando `LinkPago`), el hilo con el propietario, `core/billing`
  (Wompi y checkout de Suscripciones) y la clave RBAC `pack_cobranza:cobros`, que ahora se declara en
  `section_keys` del módulo porque sigue guardando «Pagos» (`GET /pack/cobranza/payments`).
- Quedan sin consumidor (no se borran): `dias_tolerancia_cobro`, `dias_anticipacion_cobro` y
  `propietario_whatsapp` de la config del pack; el escenario QA `cobranza-completa` sigue sembrando un
  cobro al propietario pendiente (no tiene dónde verse).

### Elegir el tipo de negocio habilita sus habilidades (2026-10-27)

- El onboarding ya no exige una habilidad habilitada de antemano. `GET /admin/agent-templates` trae
  `will_enable` (las habilidades que crear el agente habilitará) y `available` solo es falso si el despliegue
  no trae la habilidad (`missing_skills`); antes un negocio nuevo en Free (`spec.packs == []`) veía las 4
  plantillas bloqueadas y no podía elegir el tipo de negocio.
- `POST /admin/agents/from-script` y `/admin/agent-templates/{key}/agents` habilitan las habilidades de la
  plantilla (`core/agents/habilitacion.asegurar_habilidades`) en UNA versión de spec, sin validar `min_plan`
  (el límite comercial es el tope de agentes del plan) y devuelven `enabled_skills`. Una habilidad que no
  activa por falta de configuración queda habilitada y se avisa en `warnings`/`requirements`. Con habilidades
  por habilitar se exige además `plugins:write` (403 `skills_forbidden`); si otra edición del negocio gana la
  carrera, 409 `concurrent_edit` (reintentable). Auditoría `skills_enabled_by_template`.
- El paso `activar_preset` del checklist no aparece si las habilidades del preset ya están habilitadas.
- Contrato aditivo (`will_enable`, `enabled_skills`); sin tablas ni migración.

### Opciones directas: el resolver del toque (SPEC-40, HU-OPC-03, 2026-10-27)

- Un toque en una opción (`opt:`/`optc:`/`optn:`) ejecuta la acción de su definición **sin pasar por el LLM**
  y responde con el texto fijo de la opción. El gancho está en `consumer.py`, tras persistir el mensaje y
  las políticas de entrada y antes de `entrada_sin_llm`. Va detrás de la bandera `opciones_directas.habilitada`
  (apagada por defecto): apagada, el mensaje sigue el camino de siempre.
- `core/kernel/opciones/resolver.py`: firma y pertenencia a la conversación (un payload que no valida se
  descarta con warning y métrica, sin responder), vencimiento y versión retirada con texto fijo, modo
  exclusivo, una sola ejecución por reclamo atómico de la fila (`option_activations`, `UNIQUE(tenant_id,
  dedupe_key)`), reintento de webhook idempotente y `running` sin terminar que no se reejecuta.
- La tool corre por `_execute_tool_safely` (nuevo parámetro `ya_confirmada`) con `idempotency_key =
  <conversación>:<activation_id>`. Antes de ejecutar se revalida fail-closed: tool apta, pack activo,
  binding explícito y habilitado (sin el fail-open del loop), no gobernada por un procedimiento; los
  argumentos salen solo de `args_fijos` y del contexto del envío y se validan contra el schema de la tool
  (deriva → respuesta de error + log `option_args_drift`).
- Acciones con confirmación: botones `optc:`/`optn:` con `confirmacion.armar` (nuevo parámetro
  `instrucciones`); solo `optc:` ejecuta, con los argumentos que el cliente vio (hash firmado).
- Textos del sistema en el playbook del kernel `opciones_directas.md`. Auditoría `option_tap`
  (`actor="option:<key>"`, ids y claves sin valores), log por toque y métricas `option_taps_total{outcome}`,
  `option_tap_latency_seconds`, `option_tool_errors_total{tool}`.
- Sin migración ni rutas nuevas.
- `fijar_release_del_turno(..., enrutar=False)`: asigna release y dueño sin correr el router ni consumir el
  payload de botón del turno diferido (lo usa el toque). `send_interactive_to_conversation(...,
  sin_degradar=True)`: devuelve False en vez de mandar el texto de respaldo (lo usa la confirmación, que no
  puede conformarse con un texto). Contrato de import-linter `opciones-sin-orquestador`.

### Opciones directas: publicar una versión como plantilla de Meta (SPEC-40, HU-OPC-04, 2026-10-27)

- **Pendiente de verificar con Meta (sin cambio):** el payload del QUICK_REPLY al enviar y los límites de 25, 10 y 256
  siguen sin verificarse contra el sandbox real; la bandera `opciones_directas.habilitada` sigue apagada y es la
  puerta (ADR-0172, adenda del PR 5). Este cambio **no envía nada a ningún cliente ni llama al sandbox**.
- Servicio nuevo `core/gateway/opciones_meta.py::publicar_en_meta(negocio, versión, conexión, categoría)`: crea la
  plantilla `<clave>_v<n>` (BODY con parámetros NAMED, FOOTER y botones QUICK_REPLY) en la WABA de la conexión.
  Valida ANTES de reservar o llamar (categoría solo `UTILITY` o `MARKETING`, topes de Meta, `validate_template_components`
  y `compatibilidad_plantilla`, la misma prueba que hace el envío del PR 5). Errores como resultado con un `outcome`
  cerrado, nunca excepción.
- **Nunca se edita una aprobada:** la versión `n + 1` es la plantilla `_v<n+1>` y la anterior sigue ligada y válida
  para los envíos que la usan; no hay llamadas de edición y no se gasta ninguna de las 10 ediciones mensuales.
- **Reintento seguro sin migración:** se reserva la fila de `message_templates` en `DRAFT` (con la categoría y los
  componentes) y se liga `option_template_versions.message_template_id` ANTES del POST; el
  `UNIQUE (waba, nombre, idioma)` existente es la clave de idempotencia. Un reintento tras un corte de red consulta
  a Meta por ese nombre antes de crear: si la plantilla ya existe la adopta (sin otro POST ni otro cupo del límite de
  creación). Candado Redis por (negocio, versión) con token y liberación atómica; sin Redis no se publica. Los `ejemplos` de los parámetros se acotan a 60 caracteres sin saltos de línea.
- El estado (aprobada, rechazada, pausada) es el de `message_templates`, que ya escribe el webhook de estado de
  plantillas; `estado_de_publicacion(negocio, versiones)` lo lee en una consulta para el panel. Meta no recategoriza
  en silencio (`allow_category_change=False`).
- `core/gateway/meta_templates.py`: `create_template` se parte en `post_template` (valida, límite de creación y POST,
  sin tocar la base) y `create_template` (sin cambio de comportamiento para el panel); nueva `find_template_by_name`.
- Observabilidad: log `option_meta_publish`, métrica `option_meta_publications_total{outcome}` y auditoría
  `option_meta_publish` con ids y resultado, sin el texto de Meta.
- ADR-0172: adenda del PR 7. **Limitación conocida:** una versión se liga a UNA plantilla de Meta (una WABA); un
  negocio con dos WABAs envía por la de la versión (`plantilla_de_otra_cuenta` en la otra).
- **API:** `POST /plantillas-opciones/{clave}/publicar` acepta un cuerpo opcional `meta` (conexión, categoría, idioma, ejemplos) y
  publica la versión en Meta tras congelarla; si Meta falla la versión sigue publicada y el reintento es
  `POST …/versiones/{n}/publicar-en-meta`. El detalle de cada versión muestra su estado en Meta. Sin `meta` el comportamiento es el de antes.
  Mismas puertas que el resto de la API (bandera apagada → 404, `connections` con escritura y dueño o master).

### Opciones directas: el envío de la plantilla con opciones (SPEC-40, HU-OPC-02, 2026-10-27)

- **Pendiente de verificar con Meta:** el payload de un botón QUICK_REPLY de plantilla se manda **al enviar**
  (`components` con `sub_type: "quick_reply"` y un parámetro `payload`). Es una suposición del diseño que no se
  pudo confirmar contra la documentación oficial; la construcción vive en un solo punto
  (`envio.componentes_con_payload`) y hay una prueba real omitida por defecto
  (`tests/test_opciones_envio_meta_sandbox.py`, solo con `OPCIONES_META_SANDBOX=1`). Hasta verificarla, la bandera
  `opciones_directas.habilitada` se queda apagada: con ella apagada **no sale nada** a ningún cliente.
- `sender.send_options_to_conversation(conversación, clave)` crea el `option_sends` (versión vigente publicada,
  contexto resuelto por el servidor, `expires_at` según `ttl_horas`, un payload `opt:` firmado por opción) y envía
  por la cascada: ventana de 24 h abierta en WhatsApp Cloud → interactivo (`button` hasta 3 opciones, `list` de 4
  a 10); ventana cerrada o desconocida y plantilla de Meta de esa versión `APPROVED` en la WABA de la conversación
  y compatible → plantilla con sus botones y payloads; canal sin botones → texto numerado **sin ejecución
  directa**; ventana cerrada sin plantilla utilizable → no sale nada y queda un mensaje `failed` con
  `sin_plantilla`. Con la bandera apagada se niega (`flag_off`) sin crear ni enviar nada.
- Contexto del envío (`core/kernel/opciones/contexto.py`): `cliente.nombre` y `negocio.nombre` los resuelve el
  servidor; el código que dispara el envío puede aportar más rutas (`datos`). Si falta un dato que el cuerpo o una
  acción necesitan, no se envía. Nunca de lo que el LLM pasa a la tool; `cliente.nombre` (perfil del canal) se acota a 60.
- Nueva tool del core `enviar_plantilla_opciones(clave)`: solo envía a la conversación del turno una plantilla ya
  publicada (no crea ni edita), se ofrece únicamente con la bandera del negocio encendida y **no** es elegible como
  acción de una opción. Idempotente por llamada de la tool.
- Observabilidad: log `option_send` por envío (negocio, conversación, `send_id`, clave, versión, canal, vía,
  resultado), métricas `option_sends_total{canal,via}` y `option_send_failures_total{canal,outcome}` y auditoría `option_send` con ids y claves, sin valores.
- `send_interactive_to_conversation` acepta `client_message_id` estable; el interactivo lleva el pie
  (`footer`) de la definición, que el adaptador descartaba; el texto de respaldo numera las opciones.
- Un `Message` `failed` (pausa, sin plantilla) ya no cuenta como enviado en la idempotencia, el `send_id` incluye plantilla y versión, la pausa por uso de la API se comprueba antes del alta y el interactivo pausado no deja el id estable.
- ADR-0172: adenda del PR 5 (desviaciones de la cascada, huérfano aceptado, TTL único, superficie de la tool).

### Opciones directas: núcleo del modelo (2026-10-27)

- SPEC-40 / HU-OPC-01 / ADR-0172, PR 3 de la secuencia. **Nada llega a clientes**: la bandera por negocio
  (`tenants.settings.opciones_directas.habilitada`) nace apagada y aún no hay consumidor ni API.
- Migración `0112`: `option_templates`, `option_template_versions` (inmutables, `prevent_mutation`),
  `option_sends` y `option_activations`, con RLS forzada, FKs compuestas (también hacia `message_templates`, que gana `UNIQUE(tenant_id, id)`) y sin
  `DELETE` para `app_runtime` (`option_sends` tampoco recibe `UPDATE`).
- `core/kernel/opciones/`: definición pydantic, lint (`{path, code, severity, mensaje}`), mapeo puro a
  interactivo (`button` ≤ 3, `list` 4–10) y a componentes de Meta, payload `opt:`/`optc:`/`optn:` firmado.
- `MCPTool.apta_para_opcion` y `sensible` (por defecto `False`) y `option_safe` en las tools declarativas;
  **ninguna tool existente queda apta**. Una declarativa con método distinto de GET nace `sensible`.
- El linter de procedimientos reserva `opt:`, `optc:` y `optn:` (disparador y `setBy: interactive:`).

### Opciones directas: ruta de plataforma (master) para encender la bandera y primera tool apta (SPEC-40, PR 10a, 2026-10-27)

- **Nada se enciende con este cambio:** la bandera `tenants.settings.opciones_directas.habilitada` sigue apagada en
  todos los negocios. Solo existe ahora quién la puede fijar. El interruptor en el panel es el PR 10b; el despliegue a
  test y la prueba real del QUICK_REPLY de Meta (puerta para encenderla) siguen pendientes del dueño.
- `GET|PUT /api/v1/admin/platform/tenants/{tenant_id}/opciones-directas` (solo el admin master; el tester y el dueño
  reciben 403): lee y fija `{habilitada: bool}` del negocio de la ruta, dentro de su `tenant_scope`, con
  `bandera.fijar` (único escritor; `jsonb_set` atómico) y auditoría `opciones_directas_bandera` con el actor
  `admin:<email>`. 404 si el negocio no existe o está borrado, 422 si el cuerpo trae algo distinto de
  `habilitada: true|false`. Idempotente. Esquemas `OpcionesDirectasBandera` y `OpcionesDirectasBanderaIn`; el negocio se
  elige con el listado de plataforma que ya existe (`GET /admin/platform/tenants`).
- `bandera.leer(tenant_id)`: lectura que distingue «apagada» de «negocio inexistente» para el GET.
- **Riesgo de `tenants.settings` evaluado: no es real con los caminos actuales.** Todo camino que reescribe el dict
  completo (suscripciones, presets de agentes, aplicación de preset, origen del alta) lee la fila con `FOR UPDATE` y
  el `UPDATE … jsonb_set` del master espera ese candado y se aplica encima; una prueba estática
  (`tests/test_settings_del_negocio_candado.py`) vigila que un escritor nuevo no se olvide del candado. No hace falta
  pasar la bandera a una columna.
- **Primera acción apta:** `list_client_appointments` (pack de agenda) lleva `apta_para_opcion=True`: solo lee las
  citas del cliente de la conversación. Una prueba (`tests/test_opciones_tools_aptas.py`) fija la lista de las
  marcadas, de modo que agregar otra exige revisión. Ninguna es `sensible` (no cambia estado).

### Opciones directas: API admin de plantillas con opciones (SPEC-40, HU-OPC-05, 2026-10-27)

- `core/api/routers/plantillas_opciones.py` bajo `/api/v1/plantillas-opciones` (reglas en
  `core/admin/plantillas_opciones_service.py`): CRUD de borradores y retiro, `POST /validar` (hallazgos
  `{path, code, severity, mensaje}` sin guardar), `POST /vista-previa` (interactivo, componentes de Meta, cuerpo
  renderizado y texto de respaldo, sin enviar), `POST /{clave}/publicar` (congela la versión `n + 1`, inmutable; con
  errores del lint 422 sin cambiar nada; `sin_cambios` 409), `POST /{clave}/retirar`, `DELETE /{clave}` (borrado
  lógico de lo no publicado o retirado, libera la clave), `POST /{clave}/enviar-prueba` (solo a una conversación del
  negocio, con `clave_idempotencia`, por `sender.send_options_to_conversation`), `GET /catalogo-acciones` (solo las
  tools elegibles del negocio, con su JSON Schema, marca de sensible y confirmación obligatoria) y
  `GET /catalogo-variables`.
- Autorización como `meta_ops.py`: lectura con `connections`; escritura con `require("connections", write=True)` más
  `require_owner_or_master`. **Sin la bandera `opciones_directas.habilitada` (apagada por defecto) toda ruta responde
  404 `opciones_directas_no_habilitadas`** y no toca datos; la clave de otro negocio es un 404 igual que una inexistente.
- La publicación a Meta de la versión (HU-OPC-04) NO está aquí: `gancho_tras_publicar` es el punto de enganche del
  PR siguiente y `message_template_id` sale siempre nulo.
- Lista permitida de rutas para `datos` (pendiente del PR 5): `contexto.RUTAS_DE_DATOS`, cerrada y vacía; el catálogo
  de variables del lint y de la API es `contexto.catalogo_de_variables()` (base + esa lista). Una ruta fuera de ella es
  `DatosInvalidos` al enviar y `variable_fuera_de_catalogo` en el lint.
- `GET /admin/settings/message-templates/missing` distingue el `sin_plantilla` de una plantilla con opciones del de un
  aviso por evento: campos nuevos `tipo` (`evento` | `opcion`), `clave`, `total_eventos_7d` y `total_opciones_7d`
  (aditivos; el valor por defecto es `evento`).
- La clase `Opcion` de la definición pasa a llamarse `OpcionDeLaPlantilla` (el esquema OpenAPI `Opcion` ya era del
  registro de configuración y el nombre repetido habría renombrado ese esquema para el panel).
- `docs/runbooks/opciones-directas.md`: la prueba real del payload del QUICK_REPLY contra el sandbox de Meta como
  paso previo a encender la bandera, y cómo encenderla en un negocio de test.

### Onboarding obligatorio pero saltable y primer agente listo para probar (2026-10-27)

- HU-SH-10. El negocio guarda el **recorrido** del asistente de primer uso en `tenants.settings.onboarding`
  (`pendiente` → `en_curso` → `omitido` | `completo`, sin migración): `GET /admin/onboarding` trae `recorrido`
  (`abrir`: solo al propietario, `avisar`: mientras esté empezado o saltado) y se mueve con
  `POST /admin/onboarding/{avanzar,omitir,reanudar,finalizar}`. `finalizar` lo valida el servidor (409
  `faltan_pasos`). Los pasos del checklist siguen calculándose del estado real (adenda al ADR-0124). Los
  negocios anteriores a `ONBOARDING_DESDE` (vacía = función apagada) quedan `no_aplica`.
- HU-SH-10. `POST /admin/onboarding/dejar-listo` deja el agente del guion listo para probar sin abrirlo a
  clientes: chat web cerrado (`enabled=false`), publicación y activación de su versión. Idempotente; con el gate
  de pass^k cerrado la activación queda `pendiente_de_revision`; 409 `canal_en_vivo` si el chat web ya atiende.
- HU-AOS-57. `POST /admin/playground/message`, `GET /admin/playground` y `POST /admin/playground/reset` aceptan
  `connection_id`: la conversación de prueba (canal `test`, fuera de métricas) se ata a esa conexión y el turno corre
  por su release, es decir, por el agente publicado ahí. Sin él, igual que antes.
- HU-AOS-58. Nueva opción `business_hours_mode` (`bloquea` por defecto | `informativo`): con `informativo` el
  horario es el del equipo humano, el asistente responde 24/7 y lo conoce por una línea del prompt (playbook del
  kernel `horario-de-servicio`). Los negocios existentes no cambian.

### Salida interactiva tipo lista de WhatsApp (2026-10-27)

- `sender.interactivo_valido` acepta `{"type": "list", "body", "button", "rows": [{"id", "title", "description"?}]}`
  con los topes de Meta: ≤10 filas, id ≤200, título ≤24, descripción ≤72, texto del botón ≤20, cuerpo ≤1024
  e ids de fila únicos. Una sola sección en esta primera versión.
- `WhatsAppMetaOutbound.send_interactive` arma `action.sections[0].rows[]` para listas (descripción vacía no viaja).
- Sin migración: el CHECK `ck_messages_interactive_objeto` (0082) ya admite `'list'`. La respuesta
  (`list_reply`) ya la parsea el entrante como `interactive_id`. Base de HU-INMO-03.

### La habilitación de packs sale de la capa SSR (2026-10-27)

- Refactor sin cambio de comportamiento: `set_pack_for_tenant`, `configure_plugin_for_tenant` y sus
  auxiliares (activar, cuota, habilitar, deshabilitar, sembrar el flujo) pasan de `core/admin/views.py` a
  `core/agents/habilitacion.py`. `views.py` los reexporta con los mismos nombres, así que
  `/admin/plugins/{id}/action`, `PUT /admin/plugins/{id}/config` y el panel SSR siguen igual.
- Prepara que el editor de agentes habilite las habilidades de una plantilla sin importar de la capa SSR
  (el onboarding deja de exigir la habilidad habilitada de antemano). Sin tablas ni migración.

### El General y el Coordinador nacen con el negocio (2026-10-27)

- HU-AOS-53, SPEC-39. `onboard_tenant` reusa el backfill B2 (idempotente) al final del alta: crea los dos agentes
  reservados con su versión, derivados del AgentSpec v2 recién creado, y las instancias de las conexiones que ya
  existan. No cambia `aos_runtime`: el negocio sigue en `legacy` hasta su migración (P-34), así que no cambia cómo
  atiende. Un fallo se registra (`alta_agentes_base_fallo`) y el alta sigue; el backfill lo repara.
- Un negocio **nuevo** nace con `settings.agent_os.recortar_general = true`: cada especialista que cree el asistente
  le quita al General lo suyo y le deja solo la coordinación. Un negocio que ya existía conserva su bandera (la
  enciende el master) y repetir el alta no la pisa.
- **Un negocio nuevo nace en `aos_runtime = 'v3'`** cuando `AOS_CONNECTION_ROUTING` está encendida en el entorno
  (P-34): así su dueño puede crear agentes con el asistente sin pedirle nada al master. Usa `migrar_a_v3` (rol
  dueño, orden de banderas y auditoría, el mismo camino de «Preparar mis datos») sin construir ni promover
  releases: la release llega con cada conexión y, mientras no la tenga, el turno sigue por el motor de siempre.
  Con la global apagada o ante un bloqueo el negocio queda en `legacy` y el alta lo avisa en sus pasos.
- `recortar_general` se escribe con `coordinacion.fijar_bandera` (auditado como `agent_os_recorte_general`), y el
  nuevo comando `agent-core db recorte-general (--tenant | --todos) [--apagar] [--aplicar]` lo cambia en negocios
  existentes (dry-run por defecto; `--todos` solo recorre los que están en v3). Los negocios ya existentes no se
  tocan hasta que alguien lo corra.

`onboard_tenant(agentes_base=False)` salta esos pasos: lo usan los journeys del gate de pass^k, que montan su propio runtime y chocaban con `uq_agents_tenant_key` al crear un segundo Coordinador.

### El General no cuenta en el tope de agentes del plan (2026-10-27)

- **Planes:** el agente General (incorporado en todo negocio) ya no cuenta en la métrica `agentes` ni ocupa
  cupo del tope del plan; volver a ponerlo en una conexión no se mide. El Free queda con 1 agente propio
  además del General (enmienda del ADR-0142).

### Dispositivos del gestor: un equipo bloqueado ya no abre el acceso (2026-10-27)

- Cobranza. `_enforcement_dispositivo` deniega (403) un equipo en estado `bloqueado` aunque sea el único dispositivo del gestor; antes `activos_imei` quedaba vacío y el bloqueo equivalía a quitar la restricción. El intento queda en «Alertas de acceso» con el motivo `dispositivo bloqueado`.
- Cobranza. `POST /gestor/dispositivo` busca el equipo por `(tenant_id, imei)`, como el índice único: si ya es de otro gestor del negocio responde 409 (antes: `IntegrityError` → 500).
- Pruebas. Nuevo `tests/integration/test_pack_cobranza_dispositivos_enforcement.py` (403 por dispositivo no autorizado, bloqueado, vinculación idempotente, `/devices`, `/devices/{id}/status`, `/alerts` y aislamiento entre negocios). Red de seguridad previa a mover «Dispositivos» al core.

### Caché por versión de lo compilado y prefijo estable (HU-AOS-35, 2026-10-27)

- **Lo compilado de cada release se arma una vez por versión de agente.** Con `aos_runtime = v3`, la vista
  del agente en foco, el flujo de la release y los bloques fijos del prompt se compilan una vez por
  `(negocio, release, versión del agente)` (`core/agents/compiler.py`). Se guardan en la caché de releases y
  cuentan en su tope de bytes. Ya no se relee de disco ni se revalida en cada turno. Los negocios legacy
  arman el prompt como siempre.
- **El flujo de atención fijo ya no cambia cuando falla una conexión.** Es el de la release completa. Si un
  plugin está caído para el negocio, falta una conexión o una herramienta está desactivada, el bloque
  dinámico lo avisa (playbook del kernel `no-disponible`). Las herramientas que se ofrecen al modelo siguen
  filtradas como antes.
- **Anthropic usa una sola marca de caché**, en el último bloque fijo. Antes marcaba todos los bloques,
  también el dinámico, y con 4 playbooks o más pasaba del máximo de la API.
- **Métrica de tokens cacheados (migración 0114).**
  - `usage_ledger.cached_tokens` guarda solo lo leído de la caché, y la columna nueva `cache_write_tokens` lo
    escrito.
  - `input_tokens` pasa a ser el total de entrada con lo cacheado incluido en todos los proveedores.
  - Corte: las filas anteriores tienen `cache_write_tokens` en NULL. Desde el corte, Costos por modelo, la
    consola de conexiones y el editor muestran más tokens de entrada con Anthropic.
  - El costo y los créditos no cambian, porque siguen saliendo de lo que reporta el proveedor.
  - La estimación de costo por modelo del editor de agentes descuenta lo cacheado de las filas nuevas.
  - El proveedor OpenAI-compatible también lee `prompt_tokens_details.cached_tokens`.
- **Costos por agente.** `GET /admin/costs` trae `by_agent`, con los últimos 30 días por agente: turnos,
  tokens de entrada, tokens cacheados, porcentaje cacheado y costo.

### WhatsApp: los toques en botones de plantilla ya no se pierden (2026-10-27)

- Canal Meta (`core/gateway/channels/whatsapp/inbound.py`). Cuando el cliente toca un botón de
  respuesta rápida de una plantilla de Meta, el webhook llega como `type: "button"` (no `interactive`).
  **Cambio de comportamiento:** antes esos toques se descartaban sin rastro; ahora se parsean igual
  que `button_reply`/`list_reply`: `content` = `button.text` e `interactive_id` = `button.payload`
  (recortado a 256).
- Efecto: un quick reply cuyo payload no tenga un prefijo conocido por el motor llega al LLM como
  texto del cliente (el `text` del botón), como si lo hubiera escrito. El resto del flujo
  (webhook → stream de Redis → consumer) no cambia.

### Correo de acceso al crear un negocio desde el panel (2026-10-27)

- ADR-0175. `POST /admin/platform/tenants` ya no pide la contraseña del dueño: la cuenta nace sin contraseña y le llega un correo con un enlace de un solo uso para definirla y entrar (invitación `owner` de ADR-0121). `admin_password` no vacío responde 400 `admin_password_no_soportado`.
- La respuesta trae `invitation { estado, tipo, expires_at }` (`enviado`, `sin_smtp`, `fallido`, `ya_emitida`, `ya_es_miembro`); el negocio se crea aunque el correo no salga. Nuevo `POST /admin/platform/tenants/{id}/owner-invitation/resend` para reenviar el acceso.
- Una cuenta que ya existe recibe la invitación sin enlace (acepta en la app) en vez de quedar vinculada al instante; un master ya no puede ponerse a sí mismo como dueño por este camino (409 `owner_email_rejected`).
- Módulo nuevo `core/alta_acceso.py`; `core/provisioning/owner.py` delega y re-exporta. Sin migración.

### Dispositivos de todos los roles: `device-session` y control en el principal (2026-10-27)

### Añadido
- **`POST /api/v1/access/device-session` (HU-ACC-03, ADR-0174):** la APK registra o consulta su equipo tras elegir negocio y al reanudar, con cualquier rol (dueño, colaborador, gestor) y solo para su propia cuenta. Aplica la política del rol: dueño y colaborador `abierto`, gestor `restringido` por defecto. El master de plataforma nunca se registra ni se bloquea.
- **Hook opcional `PackModule.rol_de_acceso`:** el pack de cobranza informa qué cuentas son `gestor`; el core no importa del pack.
- **Control de equipo en toda la API `/api/v1`:** si la petición trae `X-Device-Id` de un equipo bloqueado o pendiente, responde 403 con `code` (`dispositivo_bloqueado`, `dispositivo_pendiente`). Sin el header no cambia nada (el panel web sigue igual). Exentas `device-session`, `/admin/me`, `/auth/*` y `gestor-token`. Caché de 60 s; con Redis caído se lee la base.

### Cambiado
- El token de campo del propietario también lleva `did` y se revalida en cada petición y en el refresh.
- **Cambio de contrato:** un login de campo sin `X-Device-Id` ni `imei` en política restringida responde 403 `dispositivo_requerido` (antes tokens con `requiereVinculacion`). En política abierta entra con el aviso `dispositivo_sin_id`.

### El token del gestor lleva el equipo y se revalida (2026-10-27)

### Cambiado
- **El token de campo del gestor lleva el equipo (`did`) y se revalida en cada petición y en el refresh (HU-ACC-02, ADR-0174).** Bloquear (o dar de baja) el equipo corta la sesión abierta con 403 `{error, code}` (`dispositivo_bloqueado`, `dispositivo_pendiente`, `dispositivo_no_autorizado`) en menos de 60 s: la vigencia se cachea en Redis 60 s y se invalida al escribir; si Redis no responde se lee la base (nunca se abre el acceso).
- `POST /auth/refresh` vuelve a decidir el equipo: con `X-Device-Id` sobre ese equipo; sin él, con el `did` del token. Un bloqueo corta también la renovación.
- Un token de gestor sin `did` (app vieja con el equipo por vincular) solo alcanza `GET/POST /gestor/dispositivo` mientras la política del gestor sea `restringido`; con política `abierta` pasa como antes.
- **Transición:** los tokens emitidos antes de este cambio no traen `did`. Mientras la cuenta tenga un equipo activo con id, siguen valiendo y el refresh les asigna ese equipo: no se desloguea a nadie al desplegar.
- Un `AccesoError` sin capturar fuera de `/api/v1` responde 403/409 con `{error, code}` en vez de 500.

### El login de campo del gestor decide el equipo en el core (2026-10-27)

### Cambiado
- **El login de campo del gestor decide el equipo en el core (HU-ACC-02, ADR-0174).** `_enforcement_dispositivo`, `POST/GET /gestor/dispositivo` y los wrappers del panel (`GET /pack/cobranza/devices`, `POST /pack/cobranza/devices/{id}/status`, `GET /pack/cobranza/alerts`) usan `account_devices` y `access_events` en vez de las tablas del pack. Un equipo `bloqueado` deniega siempre; la política `abierto/restringido` es por negocio.
- Con el header `X-Device-Id` (app nueva) el 403 lleva `code` (`dispositivo_pendiente`, `dispositivo_bloqueado`). Sin él (app vieja, solo `imei` en el cuerpo) se conserva el contrato anterior: tokens y `requiereVinculacion`.
- **Equipo compartido entre dos gestores:** deja de ser 409; la segunda cuenta queda `pendiente` y se registra el evento `dispositivo_compartido` (el vínculo es por cuenta).
- Los wrappers del panel llevan `Deprecation` y `Sunset` y aceptan el permiso viejo (`pack_cobranza:dispositivos|alertas`) o el nuevo (`devices|access_alerts`).

### Añadido
- `adoptar_legado`: red de seguridad si `pack_cobranza_0018` no corrió; copia al core, de a un gestor, los dispositivos del pack que falten.
- La fila heredada sin equipo (vínculo manual sin `imei`) se completa con el primer equipo con el que el gestor entra.

### Pendiente (siguientes entregas)
- Claim `did` en el token, revalidación en cada petición y en el refresh (HU-ACC-02, parte c).
- Los intentos del login v1 (código + API key) siguen en la tabla del pack y no aparecen en las alertas hasta HU-ACC-06.

### Migración de dispositivos e intentos del pack al core (2026-10-27)

### Añadido
- **Migración `pack_cobranza_0018` (HU-ACC-02, ADR-0174):** copia los dispositivos de los gestores (`pack_cobranza_devices`) a `account_devices` del core, por cuenta, y los intentos de acceso de los últimos 90 días a `access_events` (tipo `legado_pack`). El estado se conserva (incluido `bloqueado`); los activos sin `imei` quedan `legacy` y se rellenan en el primer login. Es una copia (las tablas del pack no se tocan), idempotente y verificada por negocio (migrados + omitidos = origen). Nada lee todavía lo copiado: el enforcement llega en la siguiente entrega.

### Dispositivos de la cuenta y política de acceso en el core (2026-10-27)

- HU-ACC-01 (SPEC-42, ADR-0174). Migración `0113`: tablas `account_devices` (un equipo por cuenta y negocio, baja lógica) y `access_events` (append-only) con RLS forzada, y copia de las concesiones `pack_cobranza:dispositivos` → `devices` y `pack_cobranza:alertas` → `access_alerts` (idempotente, reversible).
- `core/access/`: `evaluar` (regla de decisión: un equipo bloqueado deniega siempre, el alta es `INSERT … ON CONFLICT`), transiciones con estado esperado (409 si cambió) y política por negocio y rol en `tenants.settings.acceso.dispositivos` (default: owner y colaborador abiertos, gestor restringido).
- API `/api/v1/access`: `GET|POST /devices`, `GET /devices/{id}`, `POST /devices/{id}/aprobar|bloquear|dar-de-baja`, `GET /events` (keyset) y `GET|PUT /policy`. Secciones nuevas del RBAC `devices` y `access_alerts`, opt-in para colaboradores; los equipos del dueño solo los administra un dueño o el master.
- Sin cambio de comportamiento en el login ni en las rutas de cobranza: el enforcement llega con HU-ACC-02/03.

### WhatsApp Web: la sesión vinculada no se pierde en reinicios (2026-10-26)

- Conector `waweb` (ADR-0074). Corrige la pérdida del emparejamiento tras reinicios/despliegues
  (test: `licoreria-010cb7` y `solvershub` quedaron en `pending_qr` con un `session.db` vacío).
  Causa confirmada con tests: `run_session` y el watchdog borraban el blob de una sesión que SÍ se
  había vinculado cuando la fila quedaba en `error`/`pending_qr` (o no reconectaba a tiempo), porque
  decidían con `status` y con memoria del proceso; el backend además dejaba la fila en `error` en cada
  `connecting`, y `persist` pisaba el blob con un directorio sin dispositivo.
- «Nunca vinculada» ahora sale de un dato durable (`SessionContext.linked_before`: `last_connected_at`/`jid`
  más credenciales guardadas). Toda limpieza registra `waweb_session_cleared` con su `motivo`.
- `persist` solo guarda un directorio restaurado del blob o de una sesión que llegó a `connected`.
- Watchdog de QR vencido (`qr_expired_s`, 90 s): recicla para emitir un QR nuevo sin tocar el blob.
- `waweb:ready` se refresca solo con el socket conectado (los envíos fallan rápido sin conexión).
- Sesión vinculada que vuelve a pedir QR: `last_error` «Sesión perdida: re-vincular por QR» + alerta.
- `docs/despliegue-coolify.md`: despliegue del conector sin solape y con `stop_grace_period`.

### El verificador revisa horarios y preguntas (HU-AOS-20, parte 3)

Cierra HU-AOS-20 (adenda HU-AOS-20 §7 de ADR-0116). Solo con el motor de procedimientos activo.

- **Horarios sin respaldo (`horario_sin_evidencia`):** toda hora o fecha que el asistente afirma tiene que salir de lo que hizo el sistema.
  - **Respaldan:**
    - los `free_slots` de la agenda;
    - cualquier instante o fecha ISO que devolvió el servidor (la cita creada, el próximo cargo);
    - los datos de las tareas que fijó una acción;
    - el horario del negocio que trae el prompt;
    - hoy.
  - **No respaldan nunca:**
    - las horas ocupadas (`busy_slots`);
    - el texto de un error;
    - lo que dijo el cliente, salvo como eco de un dato que la tarea tiene fijado del cliente.
  - **No cuentan como afirmación:** las preguntas, las negaciones ni los condicionales.
  - **Formatos que reconoce:** «3 p. m.», «a las 3», «15:00», «3 de la tarde», «mediodía», «el jueves 29», «30 de octubre», «mañana».
- **Más preguntas que `composition.maxQuestions` (`demasiadas_preguntas`).** Pedir datos en serie («¿Tu nombre? ¿Y tu correo?» o una lista numerada) cuenta como una sola pregunta.
- **Modo por negocio:** `PUT /api/v1/admin/platform/tenants/{id}/colaboracion` con `verificar_horarios` o `verificar_preguntas`.
  - `shadow` (por defecto) solo cuenta, como `unverifiable` en `turn_outcomes.verifier`. La sombra ahora se registra aunque el turno no tenga otras extracciones.
  - `active` rechaza la respuesta y aplica el reintento del negocio, con el texto de las secciones nuevas del playbook `verificador-estado`.
- **Eval del verificador:**
  - `medir` admite los modos, el reloj y el contexto del sistema y del cliente de cada caso;
  - se suman 18 casos `sintetico-horario` y `sintetico-preguntas`;
  - con las dos validaciones en `active`, las respuestas reales siguen bajo el umbral (1 falso positivo de 217).

### Permisos de colaboradores y vigilancia de las tablas sin RLS (2026-10-26)

- `GET /api/v1/admin/collaborators/{id}/permissions`: un owner o tester solo lee cuentas vinculadas a su negocio activo;
  para cualquier otra responde 400 «admin no encontrado», igual que para un UUID inexistente. Antes, con el UUID de
  cualquier admin de la plataforma obtenía su acceso efectivo, incluidos sus permisos globales (hallazgo del auditor
  multitenant). El master no cambia.
- Test de integración nuevo (`aislamiento`): toda tabla de `public` con `tenant_id` y sin RLS habilitada y forzada debe
  estar en una lista explícita (hoy: `tenant_user`, `webhook_events`, `admin_permissions`, `tenant_invitations`,
  `qa_tester_datasets`). Una tabla nueva en ese estado hace fallar el CI.
- Documentación: `MULTITENANT.md` y el SKILL de `db-migration` listan las 5; ADR-0081 corrige que `tenant_invitations`
  no usa funciones SECURITY DEFINER.

### Se quita el tab «Propietario» de Cobranza (2026-10-26)

- El hilo de mensajes con el propietario (reclamos y preguntas hacia SolversHub, ADR-0064) lo cubre ahora el
  asistente configurado. Se eliminan la sección de permisos `pack_cobranza:propietario`, los endpoints
  `GET /pack/cobranza/owner-thread` y `POST /pack/cobranza/owner-thread/messages`, y las rutas y plantilla HTML
  del admin viejo (`/cobranza/propietario`). Las tablas del hilo y las notificaciones de cobro se conservan;
  los permisos ya otorgados sobre esa sección quedan huérfanos y no tienen efecto.

### Un teléfono recibe los avisos de un solo negocio y una sola cuenta (ADR-0168, migración 0109)

Al registrar el token de push (`POST /api/v1/me/push-token`), el servidor da de baja (soft delete) las filas activas de ese mismo token en otros negocios y a nombre de otras cuentas, con la función `SECURITY DEFINER` `push_token_release_elsewhere` (solo marca `deleted_at`, solo por token exacto, solo `app_runtime`). Antes, un teléfono con sesión en un negocio seguía recibiendo los avisos de otro bajo la cuenta anterior (visto en test el 2026-10-26). Se registra `push_token_liberado` con el conteo. La migración exige que el rol que la corre pueda saltar la RLS (como 0104).

### El token de push se reasigna al cambiar de cuenta en el teléfono (ADR-0168)

`POST /api/v1/me/push-token` ya no responde `409 token_de_otra_cuenta` cuando el token lo tenía otra cuenta del negocio: el token identifica al dispositivo, así que pasa a la cuenta que lo registra ahora (y la anterior deja de recibir). Antes, cambiar de cuenta en el mismo teléfono lo dejaba sin push. Se registra `push_token_reasignado`. Enmienda ADR-0099.

### Cada agente con solo sus poderes (2026-10-26)

- HU-AOS-51, SPEC-39 (D-49). Un agente creado desde una plantilla ya no hereda tools de otro enfoque: las `sub_*`
  (que declara el core pero son de Suscripciones) solo llegan a un agente de Suscripciones, y la familia
  `cob_*_suscripcion` de Cobranza solo si Cobranza es el proveedor de suscripciones del negocio. Siguen en todos
  los especialistas las esenciales del core (escalar a humano, perfil del cliente, memoria). La regla vive en
  `herramienta_propia` (`core/presets/agentes.py`); `derivar_contenido` recibe `proveedor_suscripciones`.
  Solo afecta a agentes nuevos: los ya publicados no se republican.
- `GET /api/v1/admin/agent-templates` suma `section` (sección del menú: Ventas, Agenda, Cobranza, Suscripciones)
  y `tools` (tools de pack que tendrá el agente). `PlantillaAgente` suma `seccion`.
- `POST /api/v1/admin/agents/{id}/versions` también respeta el enfoque del agente: un superpoder fuera de su
  plantilla responde 422 `superpoder_fuera_de_enfoque` junto al campo, igual que el editor (el CLI sigue sin la
  guarda).

### Dos pedidos en un mensaje: detección y pila en sombra (HU-AOS-20, parte 1)

El diseño está en la adenda HU-AOS-20 de ADR-0116. Esta parte solo **mide**: no cambia cómo se atiende ningún turno.

- **Detección:** el Coordinador parte el mensaje por conectores (sección `conectores` del playbook `disparadores`) y signos, sin romper «1.500» ni «p. m.». Puntúa cada parte con los disparadores de la release. Hay un pedido por parte cuando su ganador es fuerte y también es candidato del mensaje completo, sin repetir lo que ya tiene una tarea abierta. Solo en español.
- **Sombra:** con la bandera `pila` en `shadow` y el ruteo encendido, lo que la pila habría abierto queda en `core_decisions.shadow_route.pila` (`abre`, `pausa` y `sobrantes`, según `procedures.maxStack`). Se registra también con el ruteo activo. Con una tarea en foco, un pedido nuevo se anota en pausa (digresión).
- **Reporte:** `agent-core db reporte-ruteo` suma el bloque «Pila en sombra», que compara lo que habría abierto con lo que se inició en las 12 horas siguientes. El reporte del ruteo ignora las filas que solo traen `pila`.
- **Banderas** en `tenants.settings.agent_os`: `pila` (`off` por defecto), `verificar_horarios` y `verificar_preguntas` (`shadow`).
  - Endpoint del master: `GET/PUT /api/v1/admin/platform/tenants/{id}/colaboracion`, auditado como `agent_os_colaboracion`.
  - Lo que todavía no está construido (`pila = active`, verificación `active`) responde 409 `no_disponible`.
  - Las tres banderas están registradas en el catálogo de opciones (grupo «Agent OS»).
- `ReleaseCongelada.procedimientos_kernel()` lee `procedures` del KernelSpec congelado.
- Golden nuevo `tests/fixtures/ruteo/golden_pila.jsonl`, con dos pedidos y negativos con «y»: precisión 1,00 y 14 de 15 aciertos exactos.

### Dos pedidos en un mensaje: la pila de tareas activa (HU-AOS-20, parte 2)

Con la bandera `pila` en `active` (`PUT /api/v1/admin/platform/tenants/{id}/colaboracion`), que además exige `aos_procedures` y el ruteo activo, el Coordinador abre la pila. El diseño está en la adenda HU-AOS-20 de ADR-0116.

- **Dos pedidos sin tarea en curso:** atiende el dueño del primero, que se abre en foco. Los demás se abren en pausa con su propio dueño, el especialista que declara el procedimiento. Si la estabilidad del dueño frena el cambio, no hay pila en ese turno.
- **Digresión:** con una tarea en foco, el pedido nuevo se encola en pausa.
- **Tope:** lo que no cabe en `procedures.maxStack` no se abre y queda anotado en la tarjeta. `maxStack = 1` apaga la pila.
- **Atomicidad:** todo se abre en una sola transacción con la conversación bloqueada.
- **Retomar:** sin tarea en foco y con tareas en pausa, `asignar` retoma la primera (después de completar, cancelar, escalar, cambiar de versión o un cierre fuera del turno). El router la lleva a su dueño.
- **Tarea retomada de otro agente en el mismo turno:** si al cancelar se retoma una pausada que es de otro agente, ese turno no ofrece sus acciones y la tarjeta avisa que la sigue ese agente.
- **Tarjeta:**
  - con tareas en pausa, guía de una sola pregunta (solo la de la tarea en foco);
  - de cada tarea en pausa, lo que falta, sin pedirlo;
  - los sobrantes;
  - el tope de pausadas pasa a ser `maxStack - 1`.
- **Handoff:** las tareas en pausa de otro especialista conservan su dueño. Ya no bloquean `pasar_a_agente` ni se reasignan en la fase 2.

### Avisos de handoff y «cliente esperando respuesta» (ADR-0167)

- **Handoff:** tomar una conversación escalada (la primera vez) y resolverla o devolverla al agente avisan al equipo con acceso a Escalaciones (categoría Conversaciones). Quien hizo la acción no recibe el push y el aviso le nace leído.
- **Cliente esperando:** un job cada 5 min avisa (warning, con push inmediato) cuando un cliente con escalación abierta lleva más de 15 min sin respuesta humana. Un aviso por episodio; si la escalación está asignada, va dirigido a esa persona. Ignora episodios de más de 24 h.
- Sin migración ni cambio de contrato de API: los clientes (panel y app) ya muestran la categoría Conversaciones.

### Agendamiento: varias agendas por negocio (2026-10-26)

- HU-CAL-08 / ADR-0170. Una conexión de Google = una agenda; también hay agendas internas. Cada
  agenda tiene nombre visible (el que el asistente le dice al cliente), descripción, alias, color,
  zona y horario opcionales (heredan del negocio).
- Migración `0006` del pack: tabla `pack_calendar_calendars` (RLS forzada), `calendar_id` en citas y
  bloqueos con backfill a «Agenda principal», y solape por agenda. Reversible; el downgrade falla si
  ya hay solapes entre agendas.
- Nueva bandera `multi_agenda_modo` (`cliente_elige` | `desactivado`): la elección de agenda solo
  aplica con 2 o más agendas. Con una, las tools responden igual que antes.
- Tools: `list_calendars`; `calendar` y `desired_time` en `check_availability` y
  `find_next_available` (disponibilidad por agenda y por hora deseada); `calendar` en
  `create_calendar_event` (sin él, `needs_data`). Playbook `citas` con la sección «Varias agendas».
- API `/api/v1/pack/calendar`: `GET/POST /calendars`, `PUT /calendars/{id}`, `POST /calendars/{id}/delete`,
  `calendar_id` en citas y bloqueos, `calendar_ids` en `/events`, `/oauth/start?connection_id=`.
- OAuth de Google por conexión: una cuenta nueva ya no hereda el `refresh_token` de otra y reconectar
  con otra cuenta responde `otra_cuenta` sin pisar la conexión.
- Pendiente (requiere OK del dueño): regrabar la cassette `evals/cassettes/journeys/suscripciones/01-informar-planes.json`
  (cambió el esquema de las tools) y aplicar la migración en test.

### Crear un agente respondiendo un guion (2026-10-26)

- HU-AOS-48, SPEC-39 (D-49, ADR-0171). Cada plantilla de agente trae su guion de preguntas
  (`PlantillaAgente.preguntas`, en `core/presets/plantillas/*.json`): Ventas pregunta qué vende el negocio
  (productos, planes o ambos) y deja solo `tomar_pedido`, solo `contratar_plan` o los dos; todas preguntan el
  largo de las respuestas (`persona.maxLength`). Sin responder se aplica el valor por defecto del guion. Las
  respuestas solo cambian superpoderes, largo y nombre: ningún texto llega a un prompt.
- `GET /api/v1/admin/agent-templates` suma `questions` (`id`, `text`, `help`, `type`, `options[]`, `default`).
- Nuevo `POST /api/v1/admin/agents/from-script` `{template_key, answers, name?}`: crea el borrador (sin publicar)
  con los superpoderes de las respuestas y devuelve `agent`, `draft`, `warnings` y los `requirements` que le
  faltan al negocio. 422 `invalid_answers` con un campo por pregunta (no crea nada); 409 `key_taken` con el
  `agent_id` existente. Reutiliza `crear_desde_plantilla` (nuevo parámetro `respuestas`) y su guarda de enfoque.
- Sin migraciones. `docs/openapi.json` al día.

### El General enlaza a cada especialista y, con bandera, deja sus poderes (2026-10-26)

- HU-AOS-52, SPEC-39 (D-49, ADR-0171 §5). Al publicar un especialista de una plantilla (`POST /admin/agents/{id}/publish`)
  se escribe el permiso A2A General → especialista (`handoff`, idempotente) en el grafo vigente, para que el General
  pueda pasarle la conversación. Antes un especialista nuevo nacía sin enlaces.
- Con `tenants.settings.agent_os.recortar_general = true` (solo el master; apagada por defecto, a la espera del OK
  del dueño) la misma publicación deja al General sin los superpoderes del especialista y, cuando de un pack no le
  queda ningún superpoder, sin sus procedimientos, tools y playbooks (`core/agents/coordinacion.py`). Se publica como
  versión nueva del General y se ajusta su borrador; entra en la release candidata, que no se activa sola: la reversa
  es republicar la versión anterior del General y quitar el permiso con `PUT /admin/a2a/graph`.
- La respuesta de `publish` suma `general_updated` (versión nueva del General, o `null`).
- Un especialista nuevo sigue derivando sus tools del General aunque este ya esté recortado: `_base_del_general`
  recupera de sus versiones anteriores las tools y playbooks que ya no tiene.
- Sin migraciones.

### Escenarios de datos de prueba (2026-10-26)

- HU-QA-13 (adenda de ADR-0135). `GET /api/v1/admin/qa/datos/escenarios` y
  `POST /api/v1/admin/qa/datos/escenarios/{slug}` crean, en el negocio `qa-` del tester y solo en test, datos
  deterministas y sin LLM para probar cada módulo del panel: conversación con turnos (acción con datos personales,
  consulta y traspaso entre agentes, rechazo del verificador), escalación en curso con trámites, calidad con muestra,
  versión candidata, prueba con una parte (canary al 20 %), clientes y pedidos, suscripciones, avisos por categoría,
  mensajes fallidos y cobranza completa. Los turnos corren en el ledger del kernel; las tablas de los packs las
  escribe cada pack con el gancho opcional `BaseModule.sembrar_escenario`. Repetir crea otro caso sin borrar nada.

### «En curso» y la toma humana en la tarea (HU-AOS-28, parte 2)

El diseño está en ADR-0169 §5.

- **`GET /api/v1/admin/conversations/{id}/in-progress`:** por tarea (la que está en foco primero), los datos ya dados (pagos enmascarados), lo que falta para salir de la etapa (estructurado, con el rótulo humano del dato), la confirmación pendiente, la última acción y la escalación activa. Incluye, en solo lectura, las tareas cerradas como `escalated` desde esa escalación. La definición es la EXACTA de la tarea (los congelados de la release de la conversación o el binario, nunca por major); si no está, `missing_reason = definicion_no_disponible`, y sin tareas `empty_reason = sin_procedimientos`. 404 en el playground.
- **Toma humana:** tomar una conversación escalada escribe `taken` (actor `human`) en cada tarea abierta, una sola vez por toma; devolverla al agente o resolverla escribe `released`. Son anotaciones: no cambian la tarea. `take`, `return-to-bot` y `resolve` bloquean primero la conversación.
- **Migración 0111:** el CHECK de `task_events.type` pasa a `ck_task_events_type` con los dos tipos nuevos (en la práctica no se revierte).

### Depurador del turno y sección de observabilidad (HU-AOS-28, parte 1)

El diseño está en ADR-0169.

- **`GET /api/v1/admin/turns/{turn_id}`** junta en una lectura lo que pasó en un turno: decisión, pasos (comandos, acciones, motor, escalación) con su resultado sin valores, cambios de estado de las tareas, comprobaciones del verificador, operaciones entre agentes, mensajes enviados y costo. `MessageItem` gana `turn_id` (mensajes del agente) para abrirlo desde el chat. Los turnos del playground no se depuran aquí.
- **Sección `observability`:** las entradas y salidas de las acciones solo se ven con ella y siempre con los datos personales enmascarados (`core/shared/redaccion.py`, redacción al leer). El dueño la tiene; el colaborador no la hereda y solo la recibe con un permiso explícito. El costo sigue exigiendo `costs`.

### Probar una versión con una parte de los clientes: canary sticky (HU-AOS-32, parte 1)

El diseño está en la adenda HU-AOS-32 de ADR-0117, que enmienda §5. **Esta parte no se enciende sola:** `iniciar` responde 409 `canary_no_disponible` hasta la parte 2 (el job de reversión automática y los avisos), porque sin el job un canary vivo nunca se revertiría.

- **Reparto sticky:**
  - La cubeta sale de `sha256(canary ‖ conversación) % 100`. Solo se sortea en la primera asignación o al empezar un episodio; dentro del episodio la conversación conserva su versión.
  - Una versión retirada a mitad de episodio va a la activa, sin volver a sortear.
  - `conversations.release_lane` se escribe también cuando cambia solo el carril.
- **Servicios** (`core/releases/canary.py`), con el lock de la conexión:
  - **Iniciar:** 1 a 50 %, mismas reglas que publicar (gate aprobado si está cerrado). Errores 409: `requiere_v3`, `sin_release_activa`, `canary_en_curso`.
  - **Cambiar el porcentaje.**
  - **Detener:** la detenida por el dueño se puede volver a probar.
  - **Publicar para todos:** el veredicto se decide una sola vez y se guarda en `canary_evaluated`. Exige `listo`, o 7 días sin muestra, y que no haya señal dura. Hereda el gate guardado al iniciar.
- **Con canary vivo:**
  - publicar otra versión o volver atrás la activa responde 409 `canary_en_curso`;
  - las exenciones internas lo detienen con `reemplazado`;
  - volver a legacy detiene los canaries del negocio.
- **Versiones bloqueadas:** solo las retiradas por el sistema por ir peor (`no_inferioridad`, `senal_dura`) quedan bloqueadas para siempre, y tampoco se reconstruyen.
- **Evaluación** (`core/releases/canary_evaluacion.py`), sobre `core/metricas/fiabilidad.py`:
  - **Ventana:** los dos carriles se comparan desde el inicio; el estable es la versión base.
  - **Muestra:** 200 conversaciones y 72 h.
  - **Revierten:** completitud o escalamiento por conversación fuera de ±2 pp. La completitud solo cuenta con 100 tareas por carril.
  - **Solo avisan:** costo (+20 %) y p95 (+30 %).
  - **Señal dura:** lo que llegó mal en las últimas 20 conversaciones frente al estable.
- **API:**
  - `POST .../releases/{rid}/canary`;
  - `PATCH .../canary`;
  - `POST .../canary/stop`;
  - `POST .../canary/promote`;
  - `GET .../canary`.
- **Migración 0110:** `canary_evaluated` en los eventos de release.

### El canary se retira solo si empeora y avisa (HU-AOS-32, parte 2)

Con esto, «Probar con una parte» queda disponible (`CANARY_DISPONIBLE = True`). El diseño está en la adenda HU-AOS-32 de ADR-0117 (§5 y §6).

- **Job** (`core/releases/canary_job.py`), dos entradas en el beat:
  - **Cada 5 min** (`core.canary_senal_dura`): si la versión en prueba usa una habilidad retirada, se detiene (`habilidad_retirada`, no bloquea la versión). Si hay señal dura, se retira (`senal_dura`, bloquea la versión).
  - **Cada hora** (`core.canary_no_inferioridad`): el veredicto se decide una sola vez.
    - **Peor:** se retira la versión (`no_inferioridad`).
    - **Listo:** avisa que se puede publicar para todos, más los avisos de costo y lentitud si corresponden.
    - **A los 7 días sin muestra:** avisa que se puede publicar a mano.
  - **Aislamiento y concurrencia:** recorre solo negocios v3, cada uno en su `tenant_scope` y con un `try` por negocio. Por conexión, todo pasa bajo su `FOR UPDATE` y se vuelve a leer el canary. Es idempotente.
- **Avisos** en el centro de notificaciones (categoría «sistema», sección Conexiones, deduplicados por versión y motivo, en la misma transacción que la acción):
  - `core.canary_revertido` (crítico);
  - `core.canary_listo`;
  - `core.canary_sin_muestra`;
  - `core.canary_costo`;
  - `core.canary_lentitud`.

### Versiones de una conexión: diferencias, detener y permisos en el gate (HU-AOS-27, backend)

Es la base del panel de versiones. El diseño está en la adenda HU-AOS-27 de ADR-0117.

- **`GET /api/v1/admin/connections/{id}/releases/{rid}/diff?against=`.** Compara contra otra versión de la misma conexión (por defecto, la activa) y agrupa los cambios en:
  - agentes: alta, baja, otra versión o cambio de agente por defecto, con las secciones del contenido que cambiaron (nunca textos) y las herramientas y procedimientos añadidos o quitados;
  - superpoderes, con su nombre;
  - permisos A2A;
  - otros: ajustes, playbooks y flujo;
  - qué medirá el gate.

  Si la otra versión es de otra conexión, responde 404.
- **`POST .../releases/{rid}/stop`.** El dueño detiene una candidata y deja de poder activarse. Errores 409: `ya_activa`, `es_canary` o `ya_detenida`. Como el build es idempotente, reconstruir el mismo contenido responde 409 `version_detenida`.
- **Gate:**
  - Cambiar un permiso A2A ahora toca los procedimientos de los agentes del enlace (`permiso:a>b`).
  - Un veredicto aprobado solo vale si cubrió todo lo que se toca hoy.
- **Listado y detalle:** suman `lifecycle` (último evento de ciclo de vida) y `detenida`.

### `tenant purge` y `tenant create` en un solo event loop (2026-10-25)

- `agent-core tenant purge` fallaba siempre con `RuntimeError: ... attached to a different loop` y no borraba
  nada: validaba con un `asyncio.run` y purgaba con otro, y el pool asyncpg quedaba atado al primer loop. Ahora
  valida, pide la frase (`questionary` con `ask_async`) y purga en un solo `asyncio.run`, y al final cierra Redis
  y desecha el engine. `tenant create` tenía el mismo patrón (alta y seed de packs en loops distintos) y se
  corrige igual. Test de integración nuevo que corre los dos comandos por `CliRunner`.

### Tablero de fiabilidad por conexión y versión (HU-AOS-29, backend)

Es la base de la página «Calidad» del panel. El diseño está en la adenda HU-AOS-29 de ADR-0117. Todas las rutas exigen la sección `agents`; el dinero solo sale con `costs` y el `conversation_id` solo con `conversations`.

- **`GET /api/v1/admin/reliability`.** Filtra por periodo, conexión, versión y carril, y devuelve:
  - turnos, conversaciones, escalamiento (por turno y por conversación), fallidos, turnos sin resultado, p95 de latencia y costo, en total y por agente;
  - rechazos del verificador por tipo, con los falsos positivos ya marcados;
  - completitud por procedimiento (tareas cerradas en el periodo);
  - consistencia vigente de cada conexión conversacional: el pass^4 por procedimiento de su versión activa, heredado por la cadena de versiones mientras no se vuelva a tocar.

  Bajo 20 turnos o 10 tareas, las tasas salen en `null` («sin datos suficientes»).
- **`GET /api/v1/admin/reliability/verifier`** lista los turnos con un rechazo. **`POST .../verifier/{turn_id}/false-positive`** (escritura en `agents`) lo marca como falso positivo: es idempotente, no se desmarca y responde 404 si el turno no tiene ese rechazo.
- **`GET /api/v1/admin/usage?group_by=connection|agent|a2a|day`.** Gasto del ledger sin evals ni generación. El modo `a2a` agrupa por quién pide, a quién y en qué modo.
- **`GET /api/v1/admin/evals/runs`.** Últimas corridas de evaluación con su versión y `pass_k_average`, sin su costo.
- **Migración 0108:** índices concurrentes para las métricas por versión y por fecha de cierre de tareas, y el índice único de los falsos positivos.

### Cada guardrail del catálogo de Configuración trae su nombre legible y qué evita (SPEC-37) (2026-10-25)

- `register_guardrail(name, fn, priority, *, etiqueta="", explicacion="")`: el pack declara el texto para personas de su guardrail
  (`Guardrail` lo guarda); sin texto, el catálogo muestra el nombre como antes. Los cuatro guardrails de pack-cobranza, pack-calendar y
  pack-subscriptions ya lo declaran.
- Los tres interruptores del verificador (ADR-0119) los explica el core (`INFO_VERIFICADOR`).
- `GET /api/v1/admin/settings/catalog`: `disabled_guardrails.valores[]` trae `etiqueta` (legible) y `nota` (qué evita). La forma de la
  respuesta no cambia, así que `docs/openapi.json` tampoco. Son textos para personas: nunca entran al contexto del modelo.

### Registrar el pago por transferencia de un cobro de suscripción (HU-SUB-26)

El dueño puede marcar pagado el cobro que el cliente transfirió. Sin esto el cargo seguía `pendiente`, el cliente recibía recordatorios y terminaba en pausa por mora.

- `POST /api/v1/pack/pack_subscriptions/cargos/{id}/registrar-pago` con `{referencia}` (número del comprobante o texto): reusa `registrar_pago_externo` del proveedor (canal `transferencia`), reactiva la suscripción si estaba vencida o pausada por mora y emite `subscription.charge_paid` como un pago por link. Idempotente por referencia; un cargo ya pagado con otra referencia, anulado, de cambio de plan o de una suscripción cancelada responde 409. Permiso de escritura y auditoría (`subs_pago_transferencia_registrado`).
- `paga_por_otro_canal` ya no trata la transferencia como «lo cobra otro canal»: el pack sigue generando el link y los avisos del período siguiente. Solo el canal de la plataforma los apaga.
- `registrar_pago_externo` acepta `emitir_pago` (por defecto falso: el cobro de la plataforma no cambia).

### Probar una frase contra el router de una conexión (HU-AOS-26)

`POST /api/v1/admin/a2a/route-preview` recibe una conexión y una frase. Responde qué agente y qué procedimiento la atenderían con la release activa, y por qué: una regla, un disparador con su puntaje y sus candidatos, o el agente por defecto. Lo usa el panel de colaboración.

- **Cómo funciona:**
  - Usa el mismo router del turno (`router.decidir`), que es léxico y sin LLM, así que probar no tiene costo.
  - No escribe nada ni registra la frase.
- **Qué devuelve:**
  - En `mode`, si hoy el router decide en esa conexión (`off`, `shadow` o `active`).
  - En `not_evaluated_rules`, las reglas que no se pueden evaluar sin conversación (por datos del cliente, etapa u horario).
- **Errores:** 409 `sin_release_activa` si la conexión no tiene versión activa y 404 si la conexión es de otro negocio. Requiere la sección `settings`.

### Mínimo del pago en línea para el panel (HU-SUB-25)

El panel debe avisar, al guardar un plan, que un cobro bajo el mínimo no podrá tener link de pago, y ese mínimo depende de la pasarela y del ambiente (150.000 en sandbox, 1.500 en producción): el panel no puede llevar la cifra.

- `GET /api/v1/pack/commerce/pago-en-linea/minimo`: devuelve `minimo` (el que el negocio fijó en `pago_en_linea_monto_minimo`, o el que declara la pasarela; `null` si no hay ninguno), su `fuente` (`negocio`, `pasarela` o `ninguna`) y cuántas cuentas de transferencia tiene configuradas.
- `crear_pedido` no cambia: conserva su política de 150.000 cuando ninguna pasarela declara un mínimo.

### El mínimo del pago en línea lo declara la pasarela según su ambiente (HU-SUB-13, adenda de ADR-0067) (2026-10-25)

El 150.000 COP de Wompi es el mínimo del **sandbox**; en producción es 1.500. El gate de `crear_pedido` lo tenía como default de `pack_commerce` y, en producción, rechazaba un plan de 80.000 pagado en línea salvo que el negocio lo bajara a mano.

- `core.payments.monto_minimo(capacidad)`: operación opcional de la capacidad (molde de `procesar_evento`). `WompiPaymentCapability.monto_minimo()` devuelve 150.000 en sandbox y 1.500 en producción.
- `pack_commerce`: `pago_en_linea_monto_minimo` pasa a `None` por defecto (usa el de la pasarela). Un valor explícito manda, y 0 apaga el gate (antes `or 150_000` lo volvía a prender). Sin pasarela que lo declare, queda 150.000.
- Playbook de ventas sin la cifra: `pack_commerce` **2.5.0** y lock regenerado. Cambia el prompt: hay que **regrabar los cassettes del gate** (lo hace el dueño con su clave).
- Los negocios de test no cambian (su pasarela es sandbox); producción deja de bloquear tickets bajo 150.000.

### API para las claves sin pantalla: palabras de baja y política de WhatsApp Web (HU-CFG-05, SPEC-37) (2026-10-25)

- Nuevo `GET/PUT /api/v1/admin/settings/bajas` (sección Configuración): el dueño edita las palabras con las que un cliente se da de
  baja de los envíos a segmentos (ADR-0150). Se guardan sin vacíos ni repetidos (sin importar mayúsculas, tildes ni signos), hasta
  50 de 60 caracteres. Lista vacía = restablecer las de por defecto. Cada cambio queda en `audit_log`.
- Nuevo `GET/PUT /api/v1/admin/settings/whatsapp-web` (solo master; 403 al resto): modo, pausa mínima y máxima, tope diario y
  calentamiento, con los rangos de `core/config_limites.py`. Una pausa máxima menor que la mínima responde 422 con el campo (antes
  el runtime las igualaba en silencio). Pasar a `allow_outbound` exige `acepta_riesgo: true` y la `risk_version` del aviso vigente (`RISK_ACK_VERSION`, la que devuelve la lectura) y deja una
  fila `settings.waweb_allow_outbound` en `audit_log` con quién, antes, después y la versión del aviso.
- Sin migraciones; sin cambios de comportamiento para quien no toque estas opciones.
- El 403 de la política de WhatsApp Web sale antes de validar el cuerpo (dependencia), y los PUT/GET sin negocio activo responden 400 `no_tenant`.

### Una sola validación de la configuración (HU-CFG-03, SPEC-37) (2026-10-25)

- Nuevo `core/config_limites.py` (módulo hoja): el rango de cada opción numérica se escribe una sola vez. De ahí lo leen el
  registro de opciones, `IdentityUpdate`, `save_identity`, el panel SSR, `loop.py` y la política de WhatsApp Web (antes los
  umbrales estaban escritos cuatro veces).
- `PUT /api/v1/admin/settings/identity` rechaza con 422 (campo marcado y lista de valores válidos) un `debounce_mode`,
  `split_style`, `persona_tone` o `persona_emojis` desconocido. Antes `debounce_mode` aceptaba cualquier texto y se comportaba
  como «sin espera», y `split_style` caía en silencio a `un_mensaje`. Un campo de voz vacío sigue significando «no cambiar».
- `auto_escalate_after_turns`: la API aceptaba hasta 1000 pero el runtime siempre recortó a 20 (ADR-0051). Ahora el rango de
  la API, del panel SSR y del runtime es 0–20 y un valor mayor responde 422. Cambia `docs/openapi.json` (`maximum` 1000 → 20).
- La lectura del runtime sigue siendo tolerante: un valor guardado fuera de rango, no numérico o `null` se recorta o cae al
  defecto en vez de tumbar el turno (`leer_entero`).
- Un preset con una clave de `app_settings` desconocida o un `split_style` inválido falla al cargar nombrando el valor.
- `GET /api/v1/admin/settings` devuelve los valores como los aplica el runtime: una fila vieja fuera de rango se recorta (umbrales,
  reintentos, `auto_escalate_after_turns`, calidez, longitud) y un `debounce_mode` o `split_style` desconocido se muestra como lo trata el
  runtime (`inmediato` y `pocos_cortos`). Así el panel y la APK, que reenvían lo que leyeron, nunca dejan al dueño sin poder guardar
  por un 422 que no puede ver ni corregir. La calidez 0 ya no se muestra como 3.

### Registro de opciones de configuración del negocio (HU-CFG-02, SPEC-37, ADR-0166) (2026-10-25)

- Nuevo `core/config_registro/`: un modelo `Opcion` (`extra="forbid"`) y las 31 opciones de Identidad y comportamiento
  (columnas de `AppSettings`, `behavior_config`, `escalation_config` y persona del spec), cada una con etiqueta,
  explicación en español, valores con etiqueta, rango, valor por defecto, dependencia, quién la edita y dónde se guarda.
  Es una vista sobre el almacenamiento actual: sin tablas ni migraciones.
- Los valores salen del runtime (`DEBOUNCE_DELAY_MS`, `CAPS`, `TOGGLES`, umbrales de dificultad, política de
  WhatsApp Web, contrato `Persona`); los guardrails apagables se calculan por negocio con los packs habilitados.
- Nuevo `GET /api/v1/admin/settings/catalog` (sección `settings`): el master recibe las 31; el resto, las 26 que no son de
  WhatsApp Web. Incluye `version` (huella del contenido) para que la APK sepa si su copia sigue vigente.
- Prueba de consistencia con `ast`: toda clave literal que el código lee de `behavior_config`/`escalation_config` debe estar
  registrada, y toda registrada debe tener lector. Otra prueba vigila que el runtime de turnos no importe el registro (las
  etiquetas son texto para personas, nunca prompts).
- ADR-0166.

### Limpieza de claves muertas de la configuración (HU-CFG-01, SPEC-37) (2026-10-25)

- `tono` (columna de `app_settings` sin lector) deja de escribirse: el alta de negocio, `agent-core init` y los
  presets ya no lo guardan. `PUT /api/v1/admin/settings/identity` sigue aceptándolo (una APK instalada lo envía)
  pero lo ignora; la lectura lo conserva marcado `deprecated` en el OpenAPI y `IdentityUpdate.tono` pasa a ser
  opcional. El tono real es `persona.tone`; el CLI `configure tone` lo escribe ahí (`cambiar_tono_persona`).
- `DebounceMode` incluye `corto` (espera 3 s), que ya existía en `DEBOUNCE_DELAY_MS` pero no se podía elegir.
- Se corrigen comentarios del modelo que citaban una clave fantasma y se quita `tono` del panel SSR heredado.
- Sin migraciones.

### HU-AOS-21 parte 3b: corredor del gate de pass^k (2026-10-25)

- Nuevo corredor que mide una release candidata contra la activa y escribe el veredicto (`approved` /
  `rejected`) que lee el gate: `core/releases/corredor.py` (reclamo idempotente, cierre, gasto parcial en
  fallos, cinco fallidas por día) y el subproceso `evals/gate_release.py`, con entorno propio contra la base
  `agentcore_eval`, un negocio efímero por set de escenarios y borrado al terminar.
- `core/releases/snapshot.py`: foto de lo que congeló la release (sin datos del cliente) para montarla en la
  base de eval; `core/releases/escenarios.py`: elige los journeys por procedimiento tocado y su huella.
- CLI: `agent-core db migrate --eval` y `agent-core db evaluar-release` (el LLM real exige `--confirmo-costo`;
  `--grabacion replay` corre sin red). El `entrypoint.sh` migra la base de eval si está configurada; el
  `Dockerfile` copia `evals/`.
- Configuración nueva: `EVAL_DATABASE_URL`, `EVAL_MIGRATIONS_DATABASE_URL`, `EVAL_REDIS_DB`,
  `EVAL_TIMEOUT_SEGUNDOS`. `agentcore_eval` se crea a mano una vez (`CREATE DATABASE agentcore_eval`).
- Las grabaciones normalizan también el slug `gate-eval-*` de los negocios efímeros.
- **El gate no se cierra todavía**: sin veredicto una candidata sigue pasando con `gate = sin_evaluar`.
  Falta el encolado (tarea de Celery, endpoint «Evaluar», 409 `gate_pendiente`). No cambia `docs/openapi.json`.
- ADR-0117: adenda «Parte 3b realizada»; `docs/MULTITENANT.md`: el borrado de los efímeros del gate.

### Handoff entre agentes en dos fases (HU-AOS-19, parte 1)

Primer uso real de la guarda A2A de HU-AOS-18: un agente puede pasarle la conversación a otro y el cliente no tiene que repetir nada. Diseño en la adenda HU-AOS-19 de ADR-0116, que trae también la parte 2 (encargar y consultar, con contratos 3.2.0, adenda de ADR-0113).

- **Comando `pasar_a_agente(agente)`.**
  - Se ofrece solo con el motor activo, una release con grafo congelado y especialistas alcanzables con `handoff` que declaren el procedimiento de cada tarea abierta. Sin grafo, el prompt queda igual.
  - `agente` es un enum de keys; los textos viven en el playbook `comandos`.
- **Fase 1 (en el turno):** la guarda decide (`owner_frozen` frena el ping-pong con el mismo límite del router) y el turno guarda el handoff pendiente.
- **Fase 2 (`core/kernel/handoff.py`, al cerrar el turno y antes de `turn_outcomes`).**
  - Si el turno terminó `replied` o `silent`, bajo lock y si el dueño sigue siendo el mismo:
    - cambia el dueño de la conversación;
    - escribe `owner_changed` en cada tarea abierta (la tarea viaja intacta);
    - registra `handoff_applied` en el ledger y pone `final_instance_id` = destino.
  - Si el turno falló, escaló o terminó `neutral`, o si otro turno ya cambió el dueño, se escribe `cancelled`.
  - Nunca lanza, así que un fallo no duplica la respuesta.
- **Otros cambios:**
  - `ArtefactosDeRelease.a2a_graph`;
  - `Limites.del_kernel`;
  - `a2a.liberar`, `a2a.cerrar_handoff` y `GrafoCongelado.sin`;
  - la conversación A2A del turno se cierra al terminar el turno.

### Gate de release por pass^k, parte 3a: veredictos, forzado y gasto de evals (HU-AOS-21)

El gate ya lee veredictos. Todavía no hay corredor que los produzca (eso es la parte 3b), así que sin veredicto la candidata pasa como antes, con `sin_evaluar`.

- **Migración 0105:** `eval_runs.release_id` (FK compuesta a `connection_releases`), `eval_runs.origen` y `eval_results.procedure_id`.
- **`core/releases/veredicto.py`:**
  - calcula el pass^4 por procedimiento: fracción de sus escenarios estables, umbral 0,85 y sin bajar frente a la activa con los mismos escenarios;
  - registra en una sola transacción la corrida, los resultados, el gasto (`usage_ledger` con `kind = 'eval'`, a cargo de la plataforma) y el evento `approved` o `rejected` con su `base_release_id`;
  - expone el tope mensual: `tenants.settings.evals.tope_usd_mensual` o `EVAL_TOPE_USD_MENSUAL`, 10 USD por defecto.
- **El gate de `promover`:** un veredicto `rejected` medido contra la activa de ahora da 409 `gate_rechazado`, y uno `approved` pasa. Un veredicto medido contra otra base no cuenta.
- **Forzado:** solo el master puede saltar el gate, con `forzar` y `motivo_forzado` en `POST .../promote`. El motivo queda en el evento y en `audit_log`.
- **Gasto de evals:** queda fuera de toda lectura de gasto del negocio: cuota de costo, costos, tablero, conversación, consola de conexiones, data por agente y primeros pasos.
- Trae también el ajuste del auditor de la parte 2 que no alcanzó a entrar: `hotfix_yanked` queda reservado y la exención `qa_datos` solo vale para negocios no borrados.

### Gate de release por pass^k, parte 2: qué tocó la candidata y exenciones (HU-AOS-21)

El gate de promoción ya calcula qué procedimientos tocó la candidata frente a la release activa y lo anota en el evento `promoted` (`gate`, `gate_tocados`). Todavía no bloquea: hasta que exista el corredor de pass^k contra `agentcore_eval` (parte 3), una candidata que toca procedimientos pasa con `gate = 'sin_evaluar'`, decisión del dueño del 2026-10-25.

- `core/releases/diferencias.py`: procedimientos tocados por contenido, agente, Coordinador (voz, reglas, `routing`), playbooks, `app_settings`, plugins del negocio y habilidades.
- **Exenciones que no se pueden falsificar desde la API:**
  - release 1, cuando la conexión no tiene activa;
  - `migrar-a-v3`;
  - «Preparar mis datos», solo en negocios `qa-`;
  - hotfix `yanked`.

  Se pasan por el parámetro interno `exencion` de `promover`. La API además rechaza los motivos `migrar_a_v3` y `qa_datos`.
- **Escenarios:** los journeys de procedimientos declaran `procedimientos: [id]`, y un test exige al menos 2 por procedimiento, con una lista de pendientes que solo se achica.
- **Adenda propuesta** de ADR-0117 con el diseño del gate, revisado por el `arquitecto`, y de ADR-0119 (k = 4).

### Seguridad: la foto de un producto ya no llega a archivos de otro negocio (2026-10-25)

- Hallazgo del auditor multi-tenant (severidad alta, solo en develop): `image_key` aceptaba
  `<mi-slug>/productos/../../<otro-slug>/…` porque solo se comprobaba el prefijo de texto. El producto
  guardaba la key ajena, el panel recibía una URL firmada del archivo del otro negocio y, al reemplazar
  o quitar la foto, `borrar` podía eliminarlo. Bastaba un admin con escritura en el catálogo.
- `core/almacenamiento`: `abrir`/`borrar` rechazan keys con segmentos vacíos, `.` o `..`, barras
  invertidas, NUL o ruta absoluta (antes un NUL daba un 500). Nuevo `dentro_de=` opcional (con
  `carpeta_de(kind, slug)`) que exige que la ruta ya resuelta quede dentro de la carpeta del negocio,
  también frente a enlaces simbólicos. `borrar` solo borra archivos, nunca carpetas.
- `pack_commerce`: `_key_producto_valida` valida la forma (`<slug>/productos/<archivo>`) y abre con
  `dentro_de`; el borrado de la foto anterior también. `url_foto` no firma una `image_key` mal formada
  (cae al link externo), así que las filas viejas con una key sospechosa no se sirven ni se borran.
- Sin migración de datos. Para detectar filas ya contaminadas:
  `SELECT id, tenant_id, image_key FROM pack_commerce_products WHERE image_key IS NOT NULL AND image_key !~ '^[a-z0-9][a-z0-9-]*/productos/[A-Za-z0-9_-]+\.[A-Za-z0-9]+$';`
  (con un rol que salte la RLS; las fotos legítimas son `<slug>/productos/<uuid>.<ext>`).
  Y las que tienen una key bien formada pero de otro negocio (solo posible por escritura directa en la base;
  `url_foto` valida solo la forma, el slug se exige al guardar y en `borrar`):
  `SELECT p.id, p.tenant_id, p.image_key FROM pack_commerce_products p JOIN tenants t ON t.id = p.tenant_id WHERE p.image_key IS NOT NULL AND split_part(p.image_key, '/', 1) <> t.slug;`
- Tests: `tests/test_storage_key_traversal.py` y `tests/integration/test_pack_commerce_foto_traversal.py`.

### Encargo y consulta entre agentes (HU-AOS-19, parte 2)

Cierra HU-AOS-19 con la parte 2. El diseño está en la adenda HU-AOS-19 de ADR-0116, §4 y §5, y el contrato en la adenda 3.2.0 de ADR-0113.

- **`agentcore-contracts` 3.2.0:** `ExposedQuery.action`, opcional. Es la acción de lectura que responde la consulta. El linter exige dos cosas:
  - que la acción esté entre las tools del agente (`consulta_sin_accion_del_agente`);
  - que el `outputSchema` use solo el subconjunto que valida el kernel (`schema_de_salida_no_soportado`, en `core/kernel/esquema_salida.py`).
- **Comando `encargar(agente, procedimiento, entradas)`**, con tarea en foco.
  - Solo acepta procedimientos que el destino expone, publica y que se completan con datos (`core/kernel/colaboracion.py`). Hoy ninguno de los packs lo es.
  - Crea una sub-tarea del destino, valida los datos igual que `actualizar_tarea` y avanza las etapas sin LLM, todo en una sola transacción. Si falta un dato, no queda nada.
  - Vuelve un resultado tipado; los datos `pii` solo si el permiso los deja pasar.
- **Comando `consultar(agente, consulta, parametros)`.**
  - La acción corre con el spec y la identidad del destino, sin escribir memoria, con auditoría y una clave idempotente.
  - La salida se valida contra el schema y entra como dato.
  - No se puede consultar una acción que aparece en alguna etapa de la release ni una que pide confirmación.
- **Solo lectura, fail-closed:** una consulta solo puede usar una acción marcada `MCPTool.solo_lectura` que no crea registros. Hoy la única marcada es `sub_consultar`; el linter avisa con `consulta_con_accion_que_escribe`.
- **Límites del resultado de una consulta:**
  - solo vuelven las propiedades que declara el schema;
  - un schema abierto exige que el enlace permita `pii`;
  - la acción tiene el tope de tiempo del enlace;
  - se audita como `query:` y no cuenta como evidencia de que el agente del turno llamó la tool.
- **`_execute_tool_safely`** acepta `identidad` y `solo_lectura`.

### Registro de opciones: el Coordinador y las banderas del Agent OS (HU-CFG-07, 2026-10-25)

- `GET /api/v1/admin/settings/catalog` suma 19 opciones del Coordinador (`KernelSpec`, grupo `coordinador`; 3 topes de
  costo solo master) y las 3 banderas del Agent OS (grupo `agent_os`, solo master). Rangos y defectos salen del
  contrato `kernelspec/1`; el valor sigue donde vivía y solo se describe. Cada opción trae `ambito`
  (`negocio` | `conexion`), derivado del almacén. Los clientes actuales no cambian (ignoran grupos que no conocen).
- Prueba de consistencia: toda hoja del `KernelSpec` está registrada o excluida con motivo (voz, ids de modelo,
  playbooks, reglas de enrutamiento…). Adenda en ADR-0166.

### Gate de release por pass^k, parte 4: cerrar el gate (HU-AOS-21)

Con la bandera del negocio encendida, una versión que toca procedimientos no se activa hasta que se mide contra la versión activa. Diseño en la adenda «Parte 4» de ADR-0117.

- **Bandera por negocio,** apagada por defecto: `tenants.settings.evals.gate`. Solo el master la cambia, con `GET/PUT /api/v1/admin/platform/tenants/{id}/gate`, junto con el tope mensual (acotado a 100 USD). El cambio queda en `audit_log`.
  - La escritura es atómica dentro de `settings.evals`, sin pisar el resto.
  - Exige la base de evaluación configurada (409 `sin_base_de_eval`).
- **Activar una versión** que toca procedimientos y no tiene veredicto:
  - si la bandera está encendida, la respuesta es 409 `gate_pendiente`. En `details` vienen el estado de la última medición y `encolado`, y la medición se encola;
  - si la medición no encontró escenarios, pasa con `sin_medir` (decisión del dueño);
  - un veredicto aprobado que dejó procedimientos sin medir los anota en la promoción.
- **Tarea del worker** `core.releases.evaluar_candidata`:
  - corre `corredor.medir`, con límites de tiempo por encima del de la medición y sin `acks_late`;
  - un candado de Redis (`gate:corredor`) asegura que no corran dos mediciones a la vez: si está ocupado, la tarea se reprograma;
  - sin base de evaluación configurada, deja `fallida/sin_base_de_eval`.
- **Endpoints nuevos:** `POST .../releases/{rid}/evaluate`, que encola o responde sin medir, y `GET .../releases/{rid}/evaluation`, que devuelve el veredicto por procedimiento, lo que quedó sin medir y la última medición.
- **`core/provisioning/consumer.py`** bloquea la fila del negocio al reescribir `settings`, para no perder la bandera.

### Centro de notificaciones del negocio: núcleo, API y push (2026-10-25)

- Nuevas tablas `notifications`, `notification_reads` y `notification_state` (migración 0107, RLS forzada): una fila por
  evento del negocio, con el estado de lectura y las preferencias por admin. ADR-0167 (enmienda ADR-0099 y ADR-0107 §1).
- `core.notifications`: `NotificationSpec` (categorías conversaciones/ventas/cobros/agenda/sistema; severidades
  info/success/warning/critical), `notify()` en savepoint que nunca propaga y `notify_standalone()`. Visibilidad por
  permiso explícito de sección; el master ve el negocio seleccionado.
- API `/api/v1/me/notifications`: lista con cursor, contador de no leídas, marcar leídas o todas, archivar,
  preferencias de push por categoría y silencio temporal.
- Push: handler core de `notification.created` idempotente por `push_sent_at`; `enviar_push` ahora envía en lotes de
  100 con `badge`, `priority`, `ttl` y canal de Android por categoría (sin categoría, el canal general de siempre).
- Tarea beat de retención: baja lógica a los 180 días.
- Disparadores del negocio migrados al centro (el push sale del mismo evento): escalación y escalación fallida,
  conexión pausada, token de Meta vencido o por vencer, calidad de número en rojo, mensaje fallido (DLQ), plan
  pagado sin activar, pagos sin aplicar o con monto distinto, suscripción pausada por mora o sin forma de pago,
  actividad de campo de cobranza y citas (a la persona asignada). Eventos nuevos: lead nuevo y pedido nuevo.
- Para `warning` y `critical` el push sale de inmediato (Celery, idempotente por `push_sent_at`); el outbox queda
  como red de seguridad (≤30 s). `notify_owners_push` se conserva solo para los avisos de facturación de la
  plataforma al vendedor.

### Aviso de cobro con datos de transferencia cuando el cargo no tiene link (HU-SUB-24, adenda de ADR-0067)

Un cargo sin link (monto bajo el mínimo de la pasarela, negocio sin pasarela o link caído) avisaba al cliente «págalo aquí:» con el link vacío; el fallo solo quedaba en el log.

- `core.payments.transferencia`: las cuentas de transferencia son una capacidad del core (`TransferDetailsProvider`, registro por pack habilitado, `formatear_cuentas`). `pack_commerce` registra las suyas (`transfer_accounts`) y suscripciones las consulta sin importarlo. El chat de ventas imprime el mismo bloque.
- Avisos de cobro de `pack_subscriptions` (cargo nuevo, recordatorio, pausa por mora): sin link llevan las cuentas, con textos de respaldo propios (`aviso_cargo_transferencia_texto`, `recordatorio_transferencia_texto`, `aviso_pausa_transferencia_texto`).
- Variables nuevas para plantillas de Meta: `cargo.datos_transferencia` y `cargo.forma_de_pago` (link o cuentas). Una plantilla que mapea `cargo.link` falla en un cargo sin link: hay que pasarla a `cargo.forma_de_pago` (`docs/plantillas-suscripciones.md`).
- Sin link y sin cuentas no sale el aviso al cliente: el cargo queda en Pagos sin link, se audita (`subs_cargo_sin_forma_de_pago`) y el dueño recibe una notificación. Cambia el comportamiento de negocios sin pasarela ni cuentas, que antes mandaban el aviso con el link vacío.
- Sin migración ni cambio de playbook (no hay que regrabar cassettes; el gate de evals en replay pasa).

### Colaboración entre agentes: permisos, guarda y ledger A2A (HU-AOS-18)

Base de la colaboración entre agentes del Agent OS (adenda HU-AOS-18 de ADR-0116). Todavía no hay operaciones que la usen (`encargar`, `consultar` y el handoff llegan con sus HU), así que no cambia la conducta de ningún turno.

- **Grafo de permisos versionado** (`core/agents/grafo_a2a.py`):
  - quién puede pasar la conversación, encargar o consultar a quién, con límites por permiso;
  - publicar es idempotente por hash y está serializado por negocio;
  - `broadcast` y `collaborate` se rechazan (son internos del Coordinador), igual que la autollamada y los agentes de otro negocio;
  - los ciclos de handoff se devuelven como advertencia.
- **Congelado en la release:**
  - el manifiesto lleva `a2a_graph` solo si el negocio tiene grafo (sin grafo, el hash de las releases no cambia);
  - `connection_releases.graph_version_id` se escribe desde el manifiesto;
  - el build falla con `a2a_invalido` si un permiso pide más profundidad o presupuesto que el KernelSpec del Coordinador.
- **Guarda determinista** (`core/kernel/a2a.py`):
  - razones: `turn_closed`, `cycle`, `no_link`, `depth`, `repeat`, `calls` (las denegaciones cuentan), `owner_frozen` (el freno de estabilidad del router), `data_class` y `budget` (reserva el presupuesto del permiso);
  - cada decisión va a `a2a_events` y a la proyección `a2a_conversations` en una transacción propia;
  - `payload_hash` es un HMAC con una clave derivada de `APP_SECRET_KEY`, y del payload solo se guarda la forma;
  - un ciclo emite `a2a.loop.detected`;
  - sin cabecera del turno no se escribe nada.
- **Gasto:** `liquidar` lo registra como `usage_ledger` con `kind = 'agent_run'` y `a2a_call_id`. Cuenta como gasto del negocio y del agente, pero no como turno: `NO_SON_TURNOS` se separa de `NO_ES_ACTIVIDAD`.
- **Migración 0106:** CHECK de `a2a_events.reason` e índice parcial `(tenant_id, a2a_call_id)` en `usage_ledger`.
- **API** `/api/v1/admin/a2a`: `GET`/`PUT /graph`, `GET /graph/versions` y `GET /calls`. Requiere la sección `settings`; en negocios legacy, solo el master puede escribir.

### Avisos de «falta configurar» al crear un agente (2026-10-24)

- HU-AOS-50 entrega 2, ADR-0155. `agent-templates` trae `requirements[]` y nace `GET /api/v1/admin/agents/{id}/requirements`:
  qué le falta al negocio para que el agente funcione (cobro en línea sin habilitar, catálogo, tipos de cita o cartera
  vacíos). Es un aviso: publicar no se bloquea. Sale del hook `contenido_cargado` de cada pack y de las habilidades
  opcionales de la plantilla, sin lógica por habilidad.

### Playbook de suscripciones: `vencida` ya no es «uso futuro» (pack_subscriptions 0.3.0) (2026-10-24)

El playbook decía que `vencida` era de uso futuro, pero el cron `marcar_vencidas` ya la usa (cargo impago pasados los días de gracia, o Free que termina su prueba sin pasar a un plan pago). El texto ahora lo explica: no genera cargos nuevos y pagar la reactiva a `activa`.

- Cambia el contenido de `playbooks/suscripciones.md`, así que sube la versión del pack a **0.3.0** y se regenera `packages/pack-versions.lock.json` (ADR-0118 §1).
- Sin cambios de código, de esquema ni de API.

### Instagram Login, refresco del token y prueba de humo (ADR-0152, HU-CHAN-04, entrega 4b) (2026-10-24)

Segunda ruta de conexión de Instagram: la persona autoriza su cuenta profesional directamente con Instagram (sin Página de
Facebook). **Apagada por defecto** (`INSTAGRAM_LOGIN_ENABLED`): se prende tras probarla con una cuenta real en test.

- **Alta** (`POST /api/v1/admin/meta/instagram/login/start` y `/login/complete`, propietario o master): `state` = intento de
  un solo uso atado al negocio y al admin; canje del código → token de 60 días → cuenta; una cuenta, un dueño; conexión
  canónica `instagram_meta`; suscripción verificada. Errores: `instagram_login_unavailable` 409, `invalid_attempt` 400,
  `asset_claimed`, `manual_connection_conflict`, `plan_channel_not_included`.
- **Envío** por `graph.instagram.com/{ig_user_id}/messages` con el token de la cuenta; un token vencido no envía.
- **Refresco diario** (`core.instagram_token_refresh`, 03:30): solo tokens de ≥ 24 h a ≤ 10 días de vencer; si Meta lo rechaza,
  la cuenta queda `degraded` sin reintentar en el ciclo y vuelve a `connected` cuando el refresco funciona.
- **Cuentas y prueba de humo:** `GET /accounts` (estado, ruta, vencimiento del token; sin tokens) y
  `POST|GET /accounts/{ig_user_id}/diagnostic` (¿llegó un mensaje desde que empezó la prueba? si no, `likely_cause`).
- **Nuevas variables:** `INSTAGRAM_APP_ID`, `INSTAGRAM_APP_SECRET`, `INSTAGRAM_LOGIN_REDIRECT_URI`, `INSTAGRAM_LOGIN_ENABLED`
  (documentadas en `docs/despliegue-coolify.md`). El webhook acepta además el secreto de la app de Instagram como secreto de
  plataforma.
- `meta_token_store.get_token(respect_expiry=True)`: el token del usuario del alta de Messenger e Instagram ya no sirve vencido.
- `GET /api/v1/admin/meta/instagram` suma `login_available` y `login_missing`.
- **Cambio de ruta de la misma cuenta** (Facebook Login ↔ Instagram Login): la conexión canónica se religa solo con el token
  ya guardado; la fila de la ruta que dejó de usarse queda `superseded` (sin borrado) y no se lista ni se refresca.
- **Auditoría (`auditor-multitenant`, sin hallazgos altos), corregida:** el secreto de la app de Instagram solo autoriza
  eventos de Instagram; la enumeración del refresco está aislada por negocio; un error transitorio (límite de uso, 5xx, red) no
  degrada la cuenta; el identificador de la cuenta es el de `GET /me`; los cortes de red y las respuestas ilegibles son errores
  controlados; dos altas simultáneas comparten la cuenta de Meta; los IDs solo aceptan dígitos ASCII (también en Messenger);
  el diagnóstico solo cuenta mensajes y acota `since` a 24 h.

### Agent OS: toda conversación nace con conexión (requisito del contract, HU-AOS-15)

Prepara el NOT NULL de `conversations.connection_id` (ver adenda 2026-10-24 de ADR-0115). Sin cambios con `AOS_CONNECTION_ROUTING` apagada, salvo la conexión interna del Centro de pruebas.

- **Centro de pruebas:** cada negocio tiene una conexión interna `playground` (tipo nuevo, oculta, sin credenciales ni release, no conversacional), creada al primer uso. La conversación de prueba va por ella; la de antes sin conexión se liga al usarla. `backfill-conexiones` suma el paso 5 para ligar las de prueba que ya existen (también cerradas).
- **Consumer:** con `AOS_CONNECTION_ROUTING` encendida, un número de la cuenta sin fila canónica la recibe con su primer mensaje y la conversación nace con conexión. Nuevo log `conversacion_sin_conexion` para lo que quede sin ella.
- La conexión interna no aparece en conexiones manuales, consola de conexiones ni filtros de Conversaciones.
- El consumer relee bajo el candado: no religa una fila que otra alta dejó viva, no crea para un número liberado ni de una cuenta dada de baja, crea con instancias y solo para WhatsApp. El turno de prueba no es un `release_missing`; la conexión interna no se guarda a mano ni envía por ningún proveedor.

### Agent OS: runbook de migración y comando del *contract* (HU-AOS-15)

- **Runbook** `docs/runbooks/migracion-agent-os.md`: orden de banderas, backfills, `migrar-a-v3`, enrutamiento, pass^4 antes de encender `aos_procedures`, apagado en orden inverso, el *contract* y el checklist de cierre.
- **`agent-core db contract-aos`** (`core/releases/contrato.py`, adenda 2026-10-24 de ADR-0115): cierra el expand/contract de la conversación por conexión. En seco por defecto; `--aplicar --confirmo-sin-vuelta-atras` aplica solo si todo se cumple (`AOS_CONNECTION_ROUTING`, todos los negocios en `v3`, ninguna conversación sin conexión, 2 releases estables sin rollback). Deduplica sin cerrar, crea el índice único, `connection_id` NOT NULL, CHECK de `core_decisions` y valida lo `NOT VALID`. **No es una migración de Alembic: no corre sola al desplegar.**
- `docs/despliegue-coolify.md`: fila de `AOS_CONNECTION_ROUTING`.

### Motor de procedimientos: «Sí, confirmo. Todo bien.» confirma, y el modelo no puede darla por hecha (HU-AOS-15)

La medición pass^4 del 2026-10-23 (corrida #8) falló un intento de `cambia-de-plan-antes-de-confirmar`: el cliente escribió
«Sí, confirmo. Todo bien.», el motor no lo tomó por confirmación (con razón: «bien» no estaba en el léxico y el clasificador
exige que todas las palabras estén), el modelo recibió tres veces «falta la confirmación», aun así contestó «ya quedó
registrada tu confirmación… en un momento te comparto los datos de la cuenta» y el pedido nunca se creó.

- **Léxico de confirmación** (`core/playbooks/kernel/confirmacion.md`): «bien» pasa a `cortesia`. Solo acompaña a una marca de
  confirmación: «confirmo, todo bien» confirma; «todo bien», «sí, todo bien» o «bien, gracias» siguen sin confirmar (decisión del
  dueño: lo ambiguo no confirma). Set etiquetado con 14 filas nuevas, precisión 1.0.
- **Mensaje al modelo** (`core/playbooks/kernel/motor.md`, `falta_confirmacion`): ahora dice que NO hay nada confirmado ni
  registrado y le prohíbe decir que quedó confirmado o prometer pasos siguientes.
- No se tocó la regla del clasificador ni las frases de las afirmaciones del procedimiento (cambiarlas exige subir la versión que
  los agentes publicados fijan).

### Motor de procedimientos: confirmar nombrando lo que dice el resumen, y el modelo no puede decir «quedó registrada tu confirmación» sin pedido (HU-AOS-15)

La segunda medición pass^4 del 2026-10-24 (corrida #10, tras el arreglo de «Todo bien.») bajó otra vez a 75 % con motor: el cliente
escribió «Sí, confirmo. Todo bien con el anual.», el clasificador no lo tomó por confirmación («anual» no está en el léxico), el
modelo dijo «Ya quedó registrada tu confirmación del Plan Premium anual…» y `crear_pedido` nunca se llamó. Dos arreglos:

- **Eco del resumen** (adenda 2026-10-24 de ADR-0113): `clasificador.es_confirmacion(..., eco=)`. Las palabras del mensaje del kernel
  que el cliente contesta (el ÚNICO mensaje de salida posterior a la petición) acompañan a la marca de confirmación como cortesía,
  nunca como núcleo. «Sí, confirmo. Todo bien con el anual» confirma ante un resumen de «Premium anual»; «…con el mensual» ante ese
  mismo resumen NO (el cliente nombra lo que no vio), ni «todo bien con el anual» sin marca, ni una reserva, negación o pregunta.
  Las palabras de negación del léxico nunca se vuelven eco. Solo para acciones y etapas; el dato personal sigue con el asentimiento.
  Léxico: `con`, `el`, `la` pasan a `cortesia`; `sin`, `salvo`, `solo`, `rechazo`, `rechazar`, `anular`, `anulo` a `negaciones`.
  El eco exige además que el mensaje del cliente sea posterior al resumen.
- **Afirmaciones de `contratar_plan` 1.3.0** (pack-commerce 2.4.0): «registrada tu confirmación», «tu confirmación quedó
  registrada», «confirmación registrada», «registrado tu pedido» y «pedido registrado» exigen `crear_pedido` (`slot:order_id`);
  sin él el verificador devuelve `status_sin_evidencia` y el modelo corrige.
- **Al desplegar:** los agentes publicados fijan `contratar_plan` 1.2.0 en su release (siguen con las frases de antes hasta
  republicar); las tareas abiertas de 1.2.0 sin release dejan de estar gobernadas (flujo v2 de respaldo), como en la 1.2.0.

### Avisos de cobro en horario legal (HU-SUB-18, Ley 2300 de 2023)

Los avisos de cobro de pack-subscriptions (cargo nuevo, recordatorios, pausa por mora, reenvío del panel) y la oferta de fin de prueba solo salen de lunes a viernes de 7:00 a 19:00 y los sábados de 8:00 a 15:00, en la zona horaria del negocio, nunca domingos ni festivos de Colombia, y como máximo uno por cliente y día.

- `avisos_de_cobro` corre cada hora y salta los negocios fuera de su ventana. Lo que no cabe hoy sale el siguiente día hábil.
- El aviso del cargo nuevo ya no sale a las 22:15: `generar_cargos` lo deja en el outbox con `entregar_desde` en la próxima ventana legal, en un día en que el cliente no tenga otro aviso de cobro. `emit_domain` acepta `entregar_desde`.
- Configuración nueva `ventana_cobro` (se puede achicar, nunca ampliar) y `pais_festivos`. Los festivos de Colombia se calculan con la Ley Emiliani.
- Migración `pack_subscriptions_0009`: registro `pack_subscriptions_avisos_cobro` (un aviso por cliente y día, RLS forzada).
- El reenvío manual del link desde el panel responde 409 fuera de horario o si el cliente ya tuvo su aviso del día.

### Vertical inmobiliaria: ADR-0156 a ADR-0165 propuestos (SPEC-28, HU-INMO-01)

Decisiones de arquitectura de la vertical inmobiliaria, propuestas hasta que se firme el piloto. Sin cambios de código.

- Pack `pack_inmobiliario` con inventario geoespacial (0156), avisos declarados por los packs (0157), agenda por responsable (0158) y como capacidad del core (0159), suscripciones de plazo fijo (0160), autorización de tratamiento de datos (0161), herramientas de análisis (0162), ruteo de plataforma (0163), supresión por titular en los packs (0164) y salida estructurada del canal (0165); adenda de ADR-0145 para el servicio de embeddings.

### Superpoderes por enfoque en el editor de agentes (2026-10-23)

- HU-AOS-50 entrega 1, ADR-0155 (enmienda ADR-0139). `GET /api/v1/admin/agent-superpowers?agent_id=` devuelve solo
  los superpoderes de la plantilla del agente (Ventas, Agendamiento, Cobranza, Suscripción); sin `agent_id` no cambia.
  Publicar un agente de plantilla con un superpoder de otro enfoque responde 422 `superpoder_fuera_de_enfoque` junto
  al campo. Las keys que no son de una plantilla (`general`, `coordinador`, especialistas por API) no se restringen.

### Administración de suscripciones en la API (2026-10-23)

- SPEC-35 (HU-SUB-08/19/20/16). `pack_subscriptions` expone lectura de suscripciones (lista con filtros y detalle con
  cargos, historial y acciones válidas), pagos (cargos, con mora), clientes con suscripción y métricas (MRR, altas,
  bajas, churn, mora por antigüedad y cohortes por plan).
- Acciones `pausar`, `reanudar` (nueva en la API), `cancelar` y `reenviar-link`, con confirmación de estado en el
  servidor y fila de `audit_log` con el usuario en la misma transacción. Las tools del LLM no cambian ni duplican auditoría.
- Reenviar link encola el recordatorio por el outbox con clave de aviso propia (`sub_resend:`); sin WhatsApp devuelve el link.
- `LeadProvider` del pack: columnas, filtro por estado de suscripción (`mora`, `ninguna`) y línea de tiempo en Clientes.
- Migración `0008`: índices por `estado` y `fecha_vencimiento`. `admin_menu` apunta a `/suscripciones`.
- Enmienda de ADR-0106. Pruebas: `tests/integration/test_subscriptions_admin_ui.py` (aislamiento, permisos, auditoría).

### Una Página de Facebook o una cuenta de Instagram pertenecen a un solo negocio (ADR-0152, HU-CHAN-01) (2026-10-23)

Primer paso del rescate de Messenger e Instagram: cerrar, antes de que existan altas, el mismo defecto que
ADR-0095 corrigió para la WABA y el número.

- Migración `0104`: `resolve_tenant_for_page` y `resolve_tenant_for_ig_user` cuentan los negocios que
  reclaman el activo (solo conexiones vivas, no borradas y fuera de `draft`) y devuelven NULL con cero o con
  más de uno, en vez de `LIMIT 1` sin orden. Nueva `meta_messaging_asset_claimed_by_other_tenant`. Las tres
  fijan `search_path`, sin EXECUTE para PUBLIC. La migración aborta si el rol no puede saltar RLS y lista en
  el log los activos que hoy tienen dos dueños (hoy nada escribe en `fb_pages` ni `ig_accounts`).
- `webhook_route_tenant.py`: un evento `page` o `instagram` cuyo activo reclama más de un negocio queda como
  conflicto (`tenant_conflict`) y no va a ninguno, como con la WABA.
- `meta_asset_claims.py`: `claim_messaging_assets` (candado por activo en la misma transacción del insert) y
  `messaging_asset_claimed_by_other_tenant`, para las altas de Messenger e Instagram que vienen después.
- ADR-0152 pasa a aceptado. Sin cambios de API ni de contratos.

### Messenger: salida por la Página, ventana de 24 h y etiqueta HUMAN_AGENT (ADR-0152, HU-CHAN-01) (2026-10-23)

Segundo paso del rescate de Messenger: la parte de envío, sin el alta ni la UI (siguen).

- `core/gateway/channels/registry.py`: adaptadores de salida por `(proveedor, canal)`. `("meta", "messenger")` usa
  el nuevo `MessengerMetaOutbound`; `meta` sin canal sigue siendo WhatsApp. **Instagram sin adaptador ya no cae al
  de WhatsApp** (mandaría el identificador del cliente como si fuera un teléfono): falla explícito y permanente.
- `core/gateway/meta_messenger.py` + `channels/messenger/outbound.py`: Send API de la Página con el token de la
  PROPIA Página. `plan_de_envio` decide antes de llamar a Meta: `RESPONSE` dentro de las 24 h; fuera, `HUMAN_AGENT`
  solo si lo envía una persona y hasta 7 días desde el último mensaje del cliente; si no, error permanente sin
  llamada ni reintentos. Solo `HUMAN_AGENT` (verificado contra la Send API el 2026-10-23): no se admiten
  `CUSTOMER_FEEDBACK` (exige su plantilla) ni las tres etiquetas retiradas en 2026.
- `core/gateway/sender.py`: `resolve_credentials` resuelve la Página de la conexión canónica (pausa, borrado y
  tenant como WhatsApp; Página viva de esa cuenta; sin respaldo a otra Página, a WhatsApp ni al `.env`).
  `validate_service_window` distingue al asistente de una persona en Messenger e Instagram.
- **Corrección:** `consumer.py` solo renovaba la ventana de 24 h en WhatsApp; en Messenger e Instagram quedaba sin
  ventana y el validador la trataba como abierta para siempre. Ahora la renuevan los tres canales de Meta.
- Sin migración ni cambios de API.

### Messenger disponible en el catálogo de conexiones (ADR-0152, HU-CHAN-01) (2026-10-23)

`messenger_meta` pasa a `available = true`: aparece en «Agregar conexión» y el panel lo lleva a
`/conexiones/messenger` (solvershub-panel). El catálogo se espeja a la base en el arranque
(`sync_connection_types`), sin migración. Instagram sigue `available = false` hasta HU-CHAN-04.

- **`META_MESSENGER_CONFIG_ID` ya no tiene respaldo en `META_CONFIG_ID`.** Esa es la Login Configuration de
  Embedded Signup de WhatsApp: abriría el registro de WhatsApp y no concede los permisos de Páginas. Sin la variable
  el alta responde `meta_configured = false` con `missing_settings = ["META_MESSENGER_CONFIG_ID"]` y la pantalla lo
  explica. **Antes de desplegar a test hay que crearla en Meta y ponerla en el entorno.**
- Quien no tiene Messenger en su plan (Free) ve el tipo en el catálogo; la pantalla le explica que es de los planes de
  pago y la API rechaza el intento con `plan_channel_not_included`.
- Prueba nueva: todo tipo de mensajería de Meta disponible debe tener adaptador de salida.
- El panel clásico (en retiro) muestra Messenger deshabilitado con «Solo en el panel nuevo»: su alta vive allí y la
  tarjeta no puede llevar a un enlace vacío.
- **Los ecos del webhook ya no se procesan como mensajes del cliente.** La Página se suscribe a `message_echoes`
  (lo que la propia Página envía vuelve al webhook con `message.is_echo`) y el parser lo tomaba como un mensaje
  entrante cuyo remitente es la Página: el asistente se habría respondido a sí mismo. Ahora se descarta.

### Messenger: alta con Facebook Login, Páginas elegidas y plan (ADR-0152, HU-CHAN-01) (2026-10-23)

Tercer paso del rescate de Messenger: el alta por la API. Sin la UI del panel (siguiente), así que el tipo
`messenger_meta` sigue `available = false`.

- **API** (`/api/v1/admin/meta/messenger`, propietario o master): `GET` (app configurada y plan), `POST /attempt`
  (intento de un solo uso, 15 min), `POST /discover` (canjea el `code` y lista las Páginas con su elegibilidad; sin
  tokens) y `POST /connect` (las Páginas elegidas). Errores con código estable (`invalid_attempt`,
  `plan_channel_not_included` 409, `insufficient_role`, `asset_claimed`, `page_not_accessible`, `user_token_expired`).
  Contrato en `docs/openapi.json` (+4 rutas, +9 esquemas).
- **Servicio** `core/gateway/meta_messenger_alta.py`: por cada Página, en una transacción, reclamo con candado
  (un solo dueño), fila, conexión canónica `messenger_meta` e instancias de agentes; luego token cifrado de la
  Página y suscripción del webhook VERIFICADA con un GET. La cuenta nace en `draft` y sale de ahí con la primera
  Página. El token del usuario dura una hora y se revoca al terminar.
- **Plan:** Messenger e Instagram en todos los planes de pago (decisión del dueño); Free solo WhatsApp.
  `features.canales` de Basic, Premium y Empresarial.
- **Nueva variable `META_MESSENGER_CONFIG_ID`** (opcional hasta activar Messenger): Login Configuration de Facebook Login for Business con
  los permisos de Páginas. Distinta de `META_CONFIG_ID` (Embedded Signup de WhatsApp); sin ella el alta de Messenger no está disponible.
  Documentada en `docs/despliegue-coolify.md`.
- `meta_onboarding._fetch_exchange_token` acepta `redirect_path` (el reintento con `redirect_uri` apuntaba siempre a
  la ruta de WhatsApp); por defecto no cambia.

### Evals: los journeys muestran el borrador que bloquea un guardrail (2026-10-23)

- `evals/journeys.py` anota, por turno, el borrador y el motivo de cada bloqueo de `run_guardrails`
  (línea `borrador bloqueado (...)` en el registro del intento). Solo observa: no cambia el veredicto ni el
  loop, y el texto no se escribe en logs ni en `audit_log` de producción (en un journey es de un cliente
  simulado). Sin esto, un intento que escalaba por `calendar_completion_check` no decía qué frase lo
  bloqueó (pass^4 de los procedimientos, 2026-10-22: `agendar-una-demo` 3/4).

### Instagram con Facebook Login: salida y alta (ADR-0152, HU-CHAN-04) (2026-10-23)

Entrega 4a del rescate de Instagram: cuentas profesionales vinculadas a una Página, con la ruta de Facebook Login.
`instagram_meta` pasa a `available = true`. La ruta por Instagram Login (otra app, token de 60 días con refresco) y la
prueba de humo con diagnóstico quedan para la entrega 4b.

- **Salida** (`channels/instagram/`): `POST /{page_id}/messages` con el token de la Página vinculada y el IGSID como
  destinatario; ventana de 24 h y `HUMAN_AGENT` como en Messenger; texto de hasta 1000 bytes; sin imágenes todavía.
  Las credenciales salen de la cuenta (`ig_accounts`) y de la custodia de tokens, con las mismas defensas que
  Messenger (pausa y borrado de la cuenta, negocio explícito, sin respaldo a otra cuenta ni al `.env`).
- **Alta** (`/api/v1/admin/meta/instagram`: `GET`, `POST /attempt`, `POST /discover`, `POST /connect`; propietario o
  master): lista las cuentas de Instagram de las Páginas que la persona administra y conecta las elegidas, con una
  cuenta = una conexión canónica. Una cuenta pertenece a un solo negocio. Contrato en `docs/openapi.json`.
- **Clientes por cuenta:** la clave del cliente de Instagram es `instagram:<ig_user_id>:<IGSID>`; el mismo IGSID en
  dos cuentas del negocio no mezcla conversaciones.
- **Suscripción sin pisar Messenger:** se piden la unión de los campos ya suscritos de la Página y los de Instagram.
- **Nueva variable `META_INSTAGRAM_CONFIG_ID`** (propia, sin respaldo en las de Messenger y WhatsApp). Sin ella el alta
  responde `meta_configured = false`. **Hay que crear la Login Configuration en Meta antes de probar en test.**
- Plan: Instagram ya estaba en los planes de pago (decisión del dueño); Free ve el motivo.
- El panel clásico muestra Instagram deshabilitado con «Solo en el panel nuevo» (la pantalla vive en solvershub-panel).
- **Corrección también para Messenger (auditoría):** su suscripción ya no reemplaza los campos de la Página (pide la unión,
  como Instagram); el token de la Página/cuenta se guarda después del reclamo y no antes; y un identificador con una
  conexión manual del mismo negocio se omite con `manual_connection_conflict` en vez de quedar «conectado» sin responder
  por la Página.

### Un toque del botón que no confirma ya no vence la petición (2026-10-23)

- Ciclo de confirmación visto en test: el resumen único sale con el botón «Confirmar»; si el toque no
  se acepta, el título del botón llegaba como un mensaje del cliente posterior al resumen y vencía la
  petición, así que el «Sí, confirmo» que pedía el propio aviso salía vencido, el kernel reenviaba el
  resumen (con el mismo botón) y el cliente volvía a tocarlo, sin crear el pedido nunca.
- El kernel ahora vuelve a registrar la MISMA petición (mismo hash y huella del resumen, marca
  `renovada`) cuando rechaza un toque de la petición vigente (`ejecucion._renovar_peticion`), en el
  consumer y en el turno del toque. Una petición ya vencida no se revive, y un «sí» a secas sigue
  venciéndola (la regla de vigencia del turno anterior no cambia).
- El rechazo de un toque deja de ser invisible: `motor_boton_confirmar_rechazado` (warning) con el
  motivo (misma tarea, seq del botón y de la petición). Falta aislar por qué el toque no se aceptó
  en test; ese log lo muestra en la próxima prueba.

### `calendar_completion_check`: un paso futuro no es una afirmación (2026-10-23)

- `pack-calendar`: "cuando confirmes el resumen, la cita queda agendada" y "…y ahí queda agendada" ya no
  cuentan como afirmar que la cita quedó creada. Con el motor de procedimientos el asistente describe la
  confirmación antes de que exista la cita, y la regla de "quedó/queda agendada" lo escalaba a un humano
  (pass^4 de los procedimientos, 2026-10-23: `agendar-una-demo` 7/8, borradores capturados con #519).
- Solo aplica a las frases con "quedó/queda" (`agendada`, `reagendada`, `cancelada`) y a oraciones donde el
  marcador (`cuando`, `una vez que`, `en cuanto`, `apenas`, `luego/después de`, `al confirmar`, `para que`,
  `ahí`) viene ANTES de la afirmación. "Si todo está bien, tu cita quedó agendada" y "Tu demo quedó agendada
  y cuando llegue el día…" siguen bloqueando sin la tool. Sin cambio de contratos ni de la evidencia exigida.

### Promesas de consultar (misma fecha)

- `calendar_completion_check`, claim `disponibilidad`: "si me dices el día, **reviso los horarios libres**" ya no
  cuenta como afirmar que un horario está libre; anuncia que se va a consultar (pass^4 del 2026-10-23, modo sin
  motor: `agendar-una-demo` 3/4 con el borrador capturado). Solo verbos en presente, futuro o infinitivo
  (`reviso`, `consulto`, `verifico`, `busco`, `miro`, `voy a ver`…) antes de la frase y sin una hora concreta en
  la oración. "Revisé los horarios libres…" (pasado), "Tenemos horarios libres el martes" y "reviso y los horarios
  libres son a las 10:00" siguen bloqueando sin `check_availability`/`find_next_available`.

### ADR-0152: Messenger e Instagram con salida, portados sobre la conexión canónica (2026-10-23)

- Propone rescatar el código de `feat/messenger-connection` y `feat/instagram-connection` (PRs #45 y #46, nunca
  llegaron a `develop`) portándolo en dos entregas: Messenger (HU-CHAN-01) e Instagram (HU-CHAN-04).
- Documenta el defecto de propiedad de Páginas y cuentas de Instagram que hay hoy (`resolve_tenant_for_page` e
  `…_for_ig_user` con `LIMIT 1` sin orden) y la corrección previa a abrir la salida. Sin cambios de código.

### Perfil propio: nombre, teléfono, usuario, contraseña y avatar (SPEC-34)

- `GET /admin/me` incluye `nombre`, `telefono` y `avatar_url` (URL firmada resuelta desde la
  `key` del almacenamiento).
- `PATCH /admin/me` actualiza nombre, teléfono y usuario (único, con validación).
- `POST /admin/me/password` cambia la contraseña pidiendo la actual; `POST/DELETE
  /admin/me/avatar` suben y quitan la foto por el controlador único (perfil `avatar`, 2 MB).
- `CollaboratorItem.avatar_url` también resuelve la URL firmada para la sección Equipo.

### Notas de voz e imágenes entrantes (HU-MEDIA-01/02/03/04/06/09)

- **ADR-0151:** la media se procesa en el consumer, no en el webhook. El webhook solo verifica,
  aplica la cuota de audio/OCR y encola; el consumer descarga de Meta, guarda con el controlador
  único, transcribe/describe y recién después persiste el inbound.
- **Descarga y storage (HU-MEDIA-02):** `core/media/meta_media.py` valida hosts de Meta, tope de
  16 MB, timeout y hash; `core/media/ingesta.py` guarda la `key` en `messages.media_url` y usa
  caché Redis por `external_message_id` para no repetir la llamada pagada.
- **Audio (HU-MEDIA-03):** transcripción con Groq/OpenAI, idioma del negocio, `[Nota de voz] …`;
  si el audio es ilegible se responde el texto configurado y no se gasta LLM.
- **Visión (HU-MEDIA-04/06):** descripción con DeepSeek/Anthropic/OpenAI, `[Foto] …`, modo
  `describir` o `sin_descripcion` por negocio y respuesta fija si no se identifica nada.
- **Configuración (HU-MEDIA-09):** migración `0103` (`app_settings.audio_*`/`vision_*`), endpoints
  `GET/PUT /admin/settings/media*`, prueba de conexión con muestras mínimas y pestaña **Multimedia**
  del panel nuevo. Las llaves se cifran y nunca se devuelven; sin llave propia se usa la de
  plataforma.
- **Costos:** cada llamada deja una fila `usage_ledger` `kind='stt'|'vision'` en modo sombra
  (`credits=0`); el cobro actual por fee de `messages` no cambia.
- **Fuera de alcance:** OCR/conciliación de comprobantes, WhatsApp Web, chat web con adjuntos, TTS
  y retención automática (documentados en SPEC-04 y ADR-0151).

### Equipo: perfil del colaborador, listado por negocio y alta en dos pasos (SPEC-34)

- `admins` gana `nombre`, `telefono` y `avatar_url` (migración 0102). El avatar se editará desde
  el perfil del propio usuario; la API ya expone el campo.
- `GET /admin/collaborators` devuelve **solo el negocio activo**; el master puede pedir
  `?todos=1` para ver todas las cuentas de la plataforma. `CollaboratorItem` incluye el perfil.
- Endpoints nuevos `GET /admin/collaborators/existe?email=` y
  `GET /admin/collaborators/username-disponible?username=` para el alta en dos pasos.
- El alta guarda `nombre` y `telefono`, valida el `username` con un error claro y, sin slug, cae
  en el negocio activo (el negocio deja de elegirse).

### Clientes: perfil base y agentes en la ficha (tanda 1A, ADR-0153)

- `core/memory/perfil.py` define el perfil canónico del cliente (`name`, `apellido`, `alias`,
  teléfono en E.164, correo, documento, dirección, municipio, departamento, país, coordenadas y
  clasificación), lee los datos viejos por alias (`full_name`, `contact_phone`, `cedula`, `city`…) y
  valida lo que llega del panel sin migración.
- `PATCH /api/v1/admin/leads/{id}/perfil` escribe por `core/memory/facts.py` con `source="human"`;
  `otros` conserva la posibilidad de guardar un dato libre con las mismas reglas. El `PATCH /{id}`
  actual sigue disponible.
- `LeadProvider` suma `agent_key`, `agent_name` e `icon` (enmienda de ADR-0056). La API devuelve
  `agents`, `agentes` y `campos_perfil` de forma aditiva; los packs de Ventas, Cobranza y Agenda ya
  declaran su área.
- El CSV de Clientes suma nombre, apellido, alias, clasificación, teléfono con indicativo, correo,
  documento, dirección, ubicación, coordenadas y agentes antes de las columnas de los packs.
- El consumidor guarda el `profile.name` de WhatsApp como alias cuando no hay uno; el nombre real
  sigue mandando. `crear_pedido` escribe el perfil con las claves canónicas.
- Panel: tabla con avatar/iniciales, nombre o alias, icono de canal, clasificación, teléfono,
  ubicación, iconos por agente y acciones; ficha con pestañas General y de cada agente, edición
  estructurada y «otros datos».
- Tests: unitarios de perfil/teléfono, integración de API (perfil, agentes, CSV, aislamiento) y
  helpers del panel. ADR-0153.

## Geocodificación de la dirección (ADR-0154)

- `core/geocoding` con LocationIQ detrás de `GEOCODING_PROVIDER`; la API key es de la plataforma.
- `POST /api/v1/admin/leads/{id}/geocodificar` guarda latitud, longitud, dirección normalizada,
  precisión, proveedor y fecha como hechos `perfil` con `source="human"`.
- Solo se envía la dirección compuesta al proveedor; los logs no llevan dirección. Topes por negocio
  y global en Redis, con 503 si no se puede verificar la cuota.
- El panel geocodifica al guardar una dirección cambiada (best-effort) y ofrece «Geocodificar
  dirección» / «Recalcular ubicación».

## Ventas, Cobranza y Agenda en la ficha (tandas 2-4)

- pack_commerce suma pedidos, total comprado, ticket promedio, último pedido y cotizaciones abiertas.
- pack_cobranza suma cartera, saldo, mora, riesgo, estatus, cuotas vencidas, próxima cuota, promesa y
  último pago; riesgo, tope de deuda y estatus se editan desde la ficha con auditoría.
- pack_calendar suma próxima/última cita, citas, canceladas, no asistió y tipo frecuente.
- La vista previa de plantilla acepta `lead_id` y el panel muestra el icono de enviar plantilla en las
  acciones del cliente (solo dueño/master). La promoción por segmento pasa a Comercio › Promociones.

### Clientes: notas del equipo y línea de tiempo (HU-CRM-04, ADR-0147)

- Notas por cliente (`GET/POST /api/v1/admin/leads/{id}/notes`, `DELETE …/notes/{note_id}`), en la tabla
  nueva `customer_notes` (migración 0099, RLS forzada, borrado lógico). Borra su autor, el propietario o
  un admin de plataforma. La purga de memoria del cliente las olvida.
- `GET /api/v1/admin/leads/{id}/timeline`: conversaciones, escalaciones, datos aprendidos (también los
  reemplazados), notas, cotizaciones y pedidos (pack_commerce) y citas (pack_calendar), del más nuevo al
  más viejo, con cursor compuesto y `partial` si un pack falla.
- `LeadProvider` suma `timeline` (enmienda de ADR-0056); `resolver` pasa a ser opcional.
- El agente ve las últimas notas del cliente en el bloque dinámico (rótulos en el playbook del kernel
  `notas-del-equipo`, texto escapado como dato). Sin notas el prompt no cambia.
- Evals: escenario golden 36 (la nota se respeta y la orden plantada en ella no), sumado al gate.
- Índice `escalations (tenant_id, conversation_id, created_at)`.

### Clientes: estado e interés del lead editables desde la ficha (ADR-0149)

- `GET /api/v1/admin/leads/{id}` suma `editables`: los campos que un pack deja cambiar, con su valor.
- `PATCH /api/v1/admin/leads/{id}/pack/{plugin_id}` cambia esos campos; el core valida claves y valores
  (400 con `fields`) y responde 404 si el pack no está habilitado o no tiene edición.
- `LeadProvider` suma `edicion` (enmienda de ADR-0056). pack_commerce expone el estado y el interés del
  lead más reciente del cliente; si no tiene lead, lo crea sin datos de contacto.
- El perfil es la fuente del nombre, el teléfono y el correo del cliente: `crear_lead` y
  `actualizar_lead` también los escriben en él (su salida no cambia). El seguimiento de leads y la
  vista SSR saludan con el nombre del perfil.

### Centro de pruebas: subsecciones y evolución por versión (HU-QA-08, catálogo v15)

- `qa_items.subseccion` (migración 0101): tercer nivel dentro del área; las pruebas del catálogo v15
  dejan de traer `grupo` (sigue obligatorio en `test`, `prod` y `datos`). El parser acepta catálogos
  anteriores sin cambios.
- Informe de ejecución en formato 2: `secciones` con las pruebas agrupadas por entorno, área y
  subsección y el conteo de cada nivel (OK, Falla, Bloqueada, Sin correr); el Markdown descargable
  incluye la tabla «Resultados por sección».
- `GET /api/v1/admin/qa/evolution`: línea de tiempo de versiones del catálogo con el resultado
  consolidado por sección del ciclo más reciente que usó cada versión.

### Foto de producto subida al almacenamiento propio (HU-CRM-05)

- `pack_commerce_products.image_key` (migración `crm_0013`): la foto del producto se sube
  desde el panel o la APK al **controlador único** (`core/almacenamiento`, ADR-0085
  enmendado). `image_url` sigue siendo el link externo (HU-CRM-01); si hay `image_key`, gana
  y la API, la APK y el agente devuelven una **URL firmada** (60 min, suficiente para que Meta
  la descargue al enviar).
- `POST /pack/commerce/products/image` (multipart) sube la imagen y devuelve `image_key` + URL.
  El alta/edición acepta `image_key` (`""` = quita la subida).

### Clientes: plantilla a un segmento y consentimiento para promociones (HU-CRM-06, ADR-0150)

- `GET /api/v1/admin/leads/acciones/plantilla/conexiones`, `POST …/vista-previa` (con los filtros de
  Clientes), `POST …/{envio_id}/confirmar` (`Idempotency-Key`) y `GET …/{envio_id}`: solo propietario o
  master. Tope de 200 destinatarios, un envío en curso por conexión y 7 días entre promociones al
  mismo cliente. Siempre plantilla `MARKETING` aprobada.
- Envío por lotes de 25 en el outbox (`crm.segment_send.batch`), idempotente por destinatario
  (`client_message_id = segmento:<envio>:<cliente>`); una promoción que no llega no escala.
- Consentimiento (`GET/POST /api/v1/admin/leads/{id}/consentimiento`) en `customer_consents`, solo
  inserción. Baja por palabras clave del cliente (`behavior_config.palabras_de_baja`) y por el error
  131050 de Meta.
- Línea de tiempo: plantillas enviadas al cliente (`kind: "plantilla"`).
- Variable nueva `SEGMENT_SEND_ALLOWLIST`: fuera de producción real solo reciben esos números.
- Migración 0100: `customer_consents`, `segment_sends` y `segment_send_recipients` con RLS forzada.

### Comercio: aliados (proveedores) opcionales por negocio (HU-CRM-03)

- Nueva tabla `pack_commerce_allies` (migración `crm_0012`, solo aditiva) y `ally_id` opcional en
  productos y promociones. Ningún negocio cambia: los aliados vienen apagados.
- El master los enciende por negocio con `PUT /api/v1/pack/commerce/platform/tenants/{id}/aliados`. Con
  la bandera apagada, `/pack/commerce/allies` responde 404. `GET /pack/commerce/modulos` le dice al
  panel si mostrar la sección.
- CRUD `/pack/commerce/allies`, filtro `?ally_id=` (o `none`) en productos, `ally_id` en producto y
  promoción (si no viene, no cambia) y columna `aliado` en el import CSV, con
  `unknown_allies` para los nombres que no existen.
- `aplicar_promocion`: una promoción con aliado aplica solo si todo el carrito es de ese aliado.
- ADR-0148 (propuesto).

### Comercio: foto del producto por URL (HU-CRM-01)

- `GET /api/v1/pack/commerce/products` devuelve `image_url` y `POST` la acepta: solo un link
  `https://` de hasta 2048 caracteres (otro esquema da 422). `null` no la cambia y `""` la quita.
- El import CSV de productos lee la columna opcional `image_url`; una URL que no sea https se ignora
  y el producto se crea sin foto.
- `consultar_inventario` le pasa la foto al core (`_image_url`, que el LLM no ve) solo con un único
  resultado pedido por SKU o por el nombre exacto del producto, como en agent-sale. Una lista, una
  búsqueda por texto o un producto sin foto no la llevan. Por WhatsApp sale como imagen en la última
  burbuja. El resultado con foto trae `tiene_foto: true`; sin foto queda igual que antes.

### Conocimiento: un original que el worker no encuentra falla como `interno`

- Si el worker no encuentra el archivo subido (detalle `archivo_ausente`), el documento queda `fallido`
  con el código `interno` y ya no con `formato`. El panel dejaba de decir «El archivo está dañado o no
  es del tipo que dice ser», que era falso: el archivo del dueño estaba bien y el fallo era nuestro
  (en test, el worker y el beat no montaban el volumen de la API).
- `error_detalle` y el log `knowledge_documento_fallido` siguen diciendo `archivo_ausente`.

### Clientes: ficha, pausa del agente por cliente, filtros y CSV (HU-UX-16, parte backend)

- `POST /api/v1/admin/leads/{id}/toggle-bot` pausa o reanuda el agente para un cliente en todas sus
  conversaciones (`Customer.bot_paused`, que el kernel ya respetaba y no tenía cómo cambiarse).
- `GET /api/v1/admin/leads/{id}`: ficha con los datos del canal, sus conexiones, la última
  conversación y lo que se sabe del cliente (`memory_facts` vigentes con origen y fecha).
- `PATCH /api/v1/admin/leads/{id}` corrige datos del perfil. Escribe solo por `core/memory/facts.py`
  con `source="human"` (el agente los ve desde la siguiente conversación); las claves del sistema
  (`crm_lead_id`, `optin`, `source`) no se pueden editar.
- La lista filtra por `channel`, `connection_id` y por los filtros que declaran los packs
  (`filtro=clave:valor`), devuelve `telefono`, `bot_paused` y `filters`, y busca también por teléfono.
- `GET /api/v1/admin/leads/export.csv`: la lista filtrada en CSV (hasta 5000 filas), con las celdas que
  empiezan por `=`, `+`, `-` o `@` neutralizadas.
- `LeadRegistry` acepta `filters` + `filter_query` (ADR-0056): el pack devuelve un `SELECT` de
  `customer_id` y el core lo aplica sin conocer sus tablas.
- pack_commerce registra por fin su `LeadProvider`: clasificación del cliente (nuevo, ocasional,
  frecuente, fiel, inactivo; la regla de `classify_customer`, calculada en SQL para toda la página) y
  estado, puntaje e interés de su último lead, con filtros por clasificación y por estado del lead.
- `GET /api/v1/admin/conversations/{id}` devuelve `customer_id`, para que la bandeja abra la ficha del cliente.

### Un traspaso a una persona exige la tool del turno (2026-10-20)

- El verificador ya no deja pasar «ya le pasé tu caso a una persona del equipo», «te comunico con una
  persona» o «una persona del equipo te va a contactar» sin `escalate_to_human` en el turno (afirmación
  `asesor_humano` de pack-commerce, adenda 2026-10-20 de ADR-0119). Smoke de test 2026-10-20: con la
  escalación ya resuelta, el asistente lo dijo otra vez y nadie lo iba a atender.
- «Si quieres, te comunico con…» no cuenta como afirmación (el condicional reconoce «quieres»).

### Smoke del dueño en test: un solo resumen, correos mal escritos, lead y pedido de plan (2026-10-20)

- Smoke del dueño en test (2026-10-20, chat web de `solvershub`), adenda 2026-10-20 de ADR-0113:
  - Contratar un plan pedía confirmar dos veces: si el modelo escribe su propio resumen al llegar a la
    etapa de confirmación, el motor manda en su lugar el resumen único con la confirmación
    (`_pedir_accion_debida`). La tarjeta y el texto de `confirmacion_requerida` ya no le piden al modelo
    que arme el resumen.
  - Un correo con el dominio mal escrito (`gmai.com`, `hotmial.com`, `outlok.com`…) ya no se guarda sin
    preguntar «¿quisiste decir @gmail.com?»; si el cliente insiste, se acepta.
  - El lead del cliente se completa con el nombre, el correo y el teléfono que da en un recorrido
    (evento `procedure.slots_set`) y al crear el pedido.
  - `consultar_estado_pedido` y `verificar_pago_pedido` dicen el estado de un pedido de plan
    (`estado_plan`, `acceso`) en vez de «en preparación». Cambia también con el motor apagado (es un
    bug de dominio).
  - `cob_suscripcion_check` (pack-cobranza) ya no toma «No encontré una suscripción activa» como una
    suscripción creada; la polaridad (preguntas, negaciones y condicionales) pasa a
    `core/shared/polaridad.py`. Ese falso positivo era el que dejaba la respuesta neutral «Dejame
    verificar eso y te confirmo».
  - Sin suscripción ligada a la conversación, `sub_consultar` ofrece pasar con una persona que verifique
    la identidad o contratar un plan.
  - La auditoría de los comandos del hilo guarda los códigos de error.

### Pack de conocimiento, parte 1: modelo y embeddings (HU-AOS-49, ADR-0145)

- **`pack-knowledge` 0.1.0, la habilidad «Conocimiento».** Trae las tablas `pack_knowledge_documents` y
  `pack_knowledge_chunks`.
  - Llevan `tenant_id`, RLS forzada, una FK compuesta y `embedding vector(1536)`.
  - Llevan también `tsv` en español sin tildes (`public.es_unaccent`).
  - Todavía no tiene tools ni subida de documentos: llegan en las partes 2 y 3.
- **Servicio de embeddings del core, `core/llm/embeddings.py`.**
  - `KNOWLEDGE_EMBEDDING_PROVIDER` vale `hash` (por defecto, sin red ni costo) u `openai`
    (`text-embedding-3-small`, que solo funciona con `APP_ENV=production`).
  - La ingesta escribe en `usage_ledger` una fila `kind='embedding'` con créditos en 0: la plataforma los
    absorbe.
- **Lectores del ledger que cuentan turnos:** ignoran las filas `embedding`. Son los costos del panel, la
  data por agente y el primer turno de los primeros pasos.
- **Topes por plan `documentos` / `conocimiento_mb`:**

  | Plan | documentos | conocimiento_mb |
  |---|---|---|
  | Free | 3 | 10 |
  | Basic | 20 | 100 |
  | Premium | 100 | 500 |
  | Empresarial | 500 | 2.000 |

  Los cuenta el pack y los aplica `enforce_pack_growth`, con 409 `plan_limit_<clave>`.
- **`agentcore-contracts` 3.1.0:** `AgentSpecV3.knowledge` es opcional, con `mode`, `topK` (1 a 8) y
  `minScore`. Hay adenda de ADR-0113: un campo opcional nuevo es un minor.
- **Postgres:** imagen fija en `pgvector/pgvector:0.8.7-pg17`. La migración del pack exige pgvector ≥ 0.8.
- **Producción:** al activar el pack de conocimiento en producción, poner `KNOWLEDGE_EMBEDDING_PROVIDER=openai`
  (ya existe `OPENAI_API_KEY`). En test no hace falta nada.

### Pack de conocimiento, parte 2: ingesta de PDF, DOCX y TXT (HU-AOS-49, ADR-0145)

- **Nueva API del pack, en `/api/v1/pack/pack_knowledge/agents/{agent_id}/documents`:**
  - **subir** (multipart, hasta 20 MB): PDF, DOCX, TXT o Markdown. Responde 202 y el documento queda
    `pendiente`;
  - **listar** cada documento con su estado: `pendiente`, `procesando`, `listo` o `fallido`, este con
    un código como `sin_texto`, `formato` o `muy_grande`;
  - **borrar:** el borrado es lógico, vacía el contenido y elimina el archivo;
  - **reprocesar.**
  - Errores: 404 desde otro negocio, 409 `documento_duplicado` y 409 `plan_limit_documentos` /
    `plan_limit_conocimiento_mb`.
- **Worker:** el pack registra dos tareas.
  - `pack_knowledge.extraer`: extrae el texto con `pypdf` o `python-docx`, lo trocea en ~400 tokens con
    solape y cuenta datos personales para el aviso.
  - `pack_knowledge.embeber`: embebe lotes de 100, en cadena.
  - Fuera de producción los embeddings son `hash`, sin costo.
- **Core:**
  - `register_task`, para tareas puntuales de un pack;
  - `contar_pii`;
  - mensajes del 409 de los topes de conocimiento.
- **Purga de un negocio:** ahora también borra su carpeta `TENANTS_ROOT/<slug>/`. Si un negocio nuevo
  usa un slug con residuos de documentos o evidencias, esos residuos van a `TENANTS_ROOT/.purgados/`.
- Los agentes todavía no usan los documentos: la búsqueda y la tool `buscar_conocimiento` llegan en la
  parte 3.

### Pack de conocimiento, parte 3: búsqueda y tool `buscar_conocimiento` (HU-AOS-49, ADR-0145)

- **Búsqueda en los documentos de cada agente:** híbrida por defecto (vector + texto en español sin
  tildes, fusionados por RRF) o semántica, con `topK` y umbral del bloque `knowledge` del agente.
  - Es exacta y prefiltrada por negocio y agente, así que un negocio grande no le quita resultados a
    uno chico.
- **Tool `buscar_conocimiento`** (pack-knowledge 0.3.0):
  - la ve solo un agente v3 que la declare en sus herramientas y tenga documentos listos;
  - devuelve los fragmentos con documento, sección y páginas para citar;
  - se suma el playbook `conocimiento`.
- **Core:**
  - `ToolContext` suma `agent_id`, `agent_version_id` y `knowledge` (adenda de ADR-0118);
  - `MCPTool.resumen_de_auditoria`: la auditoría guarda solo ids y puntajes de los fragmentos, nunca
    el texto.
- **Evals:**
  - golden set de recuperación de 50 preguntas sobre un PDF, un DOCX y un TXT (`evals/conocimiento.py`);
  - grabación de embeddings para replay (`core/llm/embeddings.grabacion`);
  - el script `scripts/grabar_conocimiento.sh`, que se corre con el OK del dueño (≈ US$0,001).
- **Golden set grabado y calibrado** (`evals/conocimiento/grabacion.json`, `text-embedding-3-small`):
  - híbrida: recall@4 0,95 y MRR 0,90;
  - `minScore` por defecto según el modo: híbrida 0,45, semántica 0,30. En `AgentSpecV3.knowledge`,
    `minScore` pasa a ser opcional;
  - las preguntas sin respuesta quedan como piso de regresión (≥ 5/10). Que el agente no invente se
    mide en su respuesta.

### Datos de prueba por agente según la descripción del negocio (2026-10-20)

- En test (`QA_TESTERS_ENABLED`), `GET/POST/DELETE /api/v1/admin/agents/{id}/datos-de-prueba` genera
  con el LLM el catálogo, los planes, la agenda o la cartera del agente según la descripción del
  negocio, con la plantilla del pack como respaldo; regenerar reemplaza y borrar retira solo lo sembrado
  (HU-QA-10, ADR-0146).
- Migración 0098: tabla `test_data_records` (RLS forzada) y `usage_ledger.kind = 'generacion'`.
- Los seeds de commerce, calendar y cobranza informan lo que crean; commerce ya no reutiliza una
  categoría borrada.

### Los mensajes del chat web ya no se quedan en `queued` (2026-10-20)

- El adaptador saliente del chat web devolvía el mismo id sintético (`web:<visitante>`) para todos los
  mensajes de una conversación. El índice único `uq_messages_tenant_external_message_id` hacía fallar al
  segundo envío al marcarlo `sent`, y ese mensaje y todos los siguientes de la conversación quedaban en
  `queued` (el widget igual los mostraba). Smoke de test 2026-10-20: en cada conversación web solo el
  primer saliente llegaba a `sent`. Ahora el id es único por mensaje.
- Los mensajes que ya quedaron en `queued` en test no se tocan: es solo su estado; el cliente los vio.

### Agent OS por negocio desde el panel del master (2026-10-20)

- `GET`/`PUT /api/v1/admin/platform/tenants/{id}/agent-os` y `POST .../agent-os/simular` (HU-AOS-15,
  «Orden de banderas»): solo el master (también en test, no el tester) fija runtime, procedimientos y ruteo; simular es un
  dry-run con bloqueos, avisos y pasos. Al pasar a v3 corre los backfills del negocio y `migrar-a-v3`
  (`construir` opcional). 422 fuera de orden, 409 con los bloqueos.
- Migración 0097: `aos_fijar_banderas` (SECURITY DEFINER), escritor único y auditado de las tres banderas,
  y el trigger `tenants_aos_banderas_guard`, que impide al rol de la aplicación escribirlas directo.
- `migrar-a-v3`, `db ruteo` y «Preparar mis datos» escriben con esa función; «Preparar mis datos» y el panel
  comparten el candado por negocio. Adenda HU-AOS-15 de ADR-0112.

### Planes por agentes: agentes publicados, colaboradores y crédito mensual (HU-BILL-05, ADR-0142)

- Dos métricas nuevas en `core/billing/metrics.py`, siguiendo D-35:
  - `agentes`: especialistas activos con al menos una instancia viva en una conexión. El General cuenta y el
    Coordinador no.
  - `colaboradores`: cuentas activas con rol colaborador más las invitaciones pendientes. Desactivar una
    cuenta libera cupo; reactivarla con el negocio al tope da 409.
- Catálogo (`core/billing/plans.py`) con las cifras de `modelo-de-precios.md §5`, aprobadas por el dueño el
  2026-10-19:

  | Plan | Agentes | Colaboradores | Crédito mensual |
  |---|---|---|---|
  | Free | 1 | 2 | 2.500 |
  | Basic | 2 | 5 | 20.000 |
  | Premium | 5 | 15 | 75.000 |
  | Empresarial | ∞ | 50 | 240.000 |

- `plugins` («Habilidades activas») deja de ser tope:
  - ya no hay 409 `plan_limit_plugins`;
  - `core/presets/apply.py` deja de rechazar presets por cantidad de packs.
- Al tope, la API responde 409 `plan_limit_agentes` en estas entradas:
  - `POST /admin/connections/{id}/agents`;
  - `POST /admin/agents/{id}/publish`, antes de publicar la versión;
  - la instancia de un preset, que queda con ese código.
- Al tope, la API responde 409 `plan_limit_colaboradores` en estas entradas:
  - `POST /admin/collaborators`;
  - `POST /admin/invitations`;
  - `POST /admin/collaborators/{id}/toggle` al reactivar una cuenta;
  - el alta de gestores de `pack_cobranza`;
  - en el panel clásico, el mismo mensaje aparece como aviso.
- El bloqueo solo frena el crecimiento: un negocio que ya supera el tope sigue operando con lo que tiene.
- `GET /admin/plan` cambia en tres puntos:
  - `usage` trae `agentes` y `colaboradores`;
  - nuevo `credit: {limit, used: null, percent: null}`, porque el uso en créditos llega con HU-BILL-04/06;
  - `plans[].monthly_credits`.
- Sin migración: `sync_plans` espeja el crédito en `plans.features.credito_mensual`.

### Pausar el chat web desde la consola de conexiones (HU-AOS-23)

- **Consola de conexiones: el chat web se pausa y se reanuda desde ahí** (HU-AOS-23). `POST /admin/connections/{id}/action`
  acepta `activate` / `deactivate` sobre la conexión `webchat`, con el mismo interruptor que Configuración → Chat web
  (`app_settings` y `channel_connections.active` en una transacción) y la misma auditoría `connection.paused` /
  `connection.resumed`, que ahora también deja Configuración al cambiar el interruptor. La fila del chat web sale con
  `pause_scope = "connection"` y `pausable = true`. ADR-0122, adenda 2026-10-19 (b).

### Motivos de escalación legibles (HU-UX-17, SPEC-31)

- `escalate_to_human` recibe `reason` de una lista cerrada (`solicitud_cliente`, `queja`, `fuera_de_alcance`,
  `intentos_fallidos`, `monto_alto`, `otro`) y un `detalle` libre aparte, como en agent-sale. Se guarda
  `tool:<código>`; el detalle va al final del resumen. Un `reason` fuera del catálogo no falla: queda como
  `otro` con el texto como detalle.
- `core/orchestrator/motivos_escalacion.py` normaliza todo `Escalation.reason` (de la tool, de los plugins y del
  sistema: `keyword`, `llm_error`, `llm_refusal`, `guardrail`, `regla:<x>`, `<guardrail>: <motivo>`,
  `persona_forbidden_phrase`, `send_failed`, `turns_unresolved`, `max_iterations`) a un código cerrado y a una
  etiqueta en español. `plugin:<tool>` se muestra como «Acción de <habilidad>». Lo guardado antes con texto
  libre se clasifica como `otro` sin migrar datos.
- `GET /api/v1/admin/escalations` expone `reason_code` y `reason_label` (además de `reason` crudo) y acepta
  `?reason_code=` (filtro en SQL, respeta la paginación; un código fuera del catálogo da 422).
- Cambia el esquema de la tool que ve el modelo: invalida las grabaciones del gate de evals (hay que regrabar).

### Dogfooding de solvershub con especialistas (2026-10-19)

- HU-AOS-22: la plantilla `suscripciones` suma la regla `preset_suscripciones_mi_plan` («cambiar mi plan», «mi
  plan actual», «subir/bajar/cancelar/pausar mi plan») con id nuevo, así que `presets-v2 --aplicar` se la agrega
  también a los negocios que ya tenían el preset. Adenda de ADR-0139: las reglas de una plantilla evolucionan
  con ids nuevos; las frases de un negocio van por el editor (`overrides.examples`).
- Tests: rutas del preset en orden (unitario), golden de ruteo con las frases del dogfooding y un punta a punta
  con el modelo guionado (sombra, luego activo: demo → Agenda, contratar Premium → Ventas, cambiar mi plan →
  Suscripciones, saludo → General).
- Sin migración ni cambios en el loop o el router. Con el ruteo apagado nada cambia; el gate en replay sigue
  verde sin regrabar.

### Crédito mensual medido (HU-BILL-04/06, ADR-0144)

- `GET /admin/plan` trae el uso real del crédito mensual: `credit.used` (créditos con décimas), `percent`,
  `period_start`/`period_end` del mes de consumo, `by_concept` y `by_agent`. Antes venían en `null`.
- Fórmula del tarifario `v1` (`core/billing/tarifario.py`): ⌈costo_usd × TRM × 3 / 10⌉ a 0,1 + fee
  (0,2 turno de IA, 0,5 nota de voz, 1,0 imagen, 0,3 plantilla enviada); con llave propia, solo el fee.
- Migración 0096: `usage_ledger` suma `credits`, `key_mode`, `tarifario_version` y `trm_cop`. El loop congela
  los créditos de cada turno al escribir la fila; si el cálculo falla, la fila se escribe igual.
- Setting nuevo `BILLING_TRM_COP` (COP por USD), por defecto 4000 y pendiente del dueño (HU-BILL-01).
- El mes de consumo sigue el ciclo de la suscripción (día ancla del cobro) y se reinicia cada mes.
- Métrica `creditos` en el registro de `core/billing/metrics.py`. No bloquea: el enforcement es HU-BILL-06.
- Deploy: migración antes que el código. Rollback: código anterior antes del `downgrade`.

### Las citas ya no se escalan como promesas de pago (2026-10-19)

- `cob_promesa_check` (pack-cobranza) solo toma como promesa una afirmación de cobranza: un
  verbo de registro junto a promesa, acuerdo, compromiso, pago, abono o cuota en la misma
  frase, o "acordamos", "prometiste", "quedamos en que". Un "tu cita quedó agendada" a secas
  ya no cuenta. Incidente en test: en un negocio con cobranza y calendario, cada cita bien
  creada con `create_calendar_event` terminaba escalada con `summary=cob_promesa_check`.
- "Agendé el pago/la cuota" lo respalda también una cita creada o movida con éxito en el
  mismo turno (`create_calendar_event`, `update_calendar_event`, por nombre, sin importar
  pack-calendar); "registré tu promesa" o "acordamos" siguen exigiendo
  `cob_registrar_promesa_pago` o `cob_registrar_promesa_saldo`.
- Tests en `tests/test_pack_cobranza_guardrails.py`.

### Bandeja de conversaciones en vivo (HU-UX-15, SPEC-31)

- `GET /admin/conversations`:
  - cada chat trae `last_message` (hasta 160 caracteres), `last_message_at`, `last_message_sender` y su
    conexión (`connection_id`, `connection_label`, `connection_provider`, `connection_type`);
  - se ordena por actividad (último mensaje o, si no hay, el inicio) y acepta `?connection_id=`;
  - el último mensaje sale de un `LATERAL … LIMIT 1` sobre `ix_messages_tenant_conv_created`: sin N+1.
- `GET /admin/conversations/connections`: las conexiones del negocio para el filtro de la bandeja, con el permiso
  de Conversaciones (quien atiende no necesita ver la sección Conexiones).
- `GET /admin/conversations/{id}/messages?after=<created_at>`: lo nuevo del hilo para el polling del panel.
  Re-entrega los 2 s anteriores al cursor (un mensaje commiteado tarde no se pierde) y el panel deduplica por
  `id`. Tope de 200 con `has_more`.
- `ConversationDetail` suma `escalation` (la activa, con `reason_code`/`reason_label` de HU-UX-17, resumen y
  asignado), `notes` (las del agente y las de personas) y `usage` (costo, tokens y turnos de
  `usage_ledger`). Cada `MessageItem` trae `tool_calls`: las herramientas del turno, en el primer mensaje del
  agente de ese turno.
- Tickets sobre la conversación (sección `conversations`, escritura):
  - `POST …/{id}/take`: la escalación activa queda `asignada` a quien la toma (`Escalation.assigned_admin_id`)
    y el agente se pausa en esa conversación. 409 `no_escalation` o `already_taken`;
  - `POST …/{id}/return-to-bot`: resuelve las escalaciones, deja la conversación abierta y reactiva el agente;
  - `POST …/{id}/notes`: nota interna con `source="human"` en `state.notes`, con un append atómico en SQL.
    No se envía al cliente ni entra al prompt.
- La lista y el detalle ya no muestran conversaciones ni mensajes con `deleted_at`.
- Sin migración: el índice `(tenant_id, conversation_id, created_at)` de `messages` ya existía (0003).

### Agenda unificada: color y código por tipo de cita (HU-UX-08, SPEC-12)

- `pack_calendar_appointment_types` suma `color` (clave de la paleta categórica `cat-1`…`cat-4` de SPEC-29,
  con CHECK: nunca un hex libre) y `codigo` (máx. 6, p. ej. «VAL»). Migración `pack_calendar` 0005,
  reversible; las filas existentes reciben un color rotando la paleta por negocio y el código de las 3
  primeras letras del nombre.
- `GET/POST/PUT /api/v1/pack/calendar/types` exponen `color` y `codigo`. Al crear sin color, rota la paleta;
  sin código, sale del nombre. Al editar, no mandarlos los conserva (clientes viejos como la app).
- `GET /api/v1/pack/calendar/events`: cada cita trae `tipo_color` y `tipo_codigo`, y la respuesta trae
  `tipos` (id, nombre, color y código de los tipos activos) para la leyenda del calendario.
- Mover un bloqueo (inicio y fin) ya existía en `PATCH /blocks/{id}`; queda cubierto con un test
  `aislamiento`, igual que los tipos con color.

### El verificador acepta los datos que fijó el motor en el turno (2026-10-18)

- Verificador (ADR-0119) con el motor de procedimientos activo: una afirmación de cierre también la respalda un
  dato de la tarea en foco que el motor fijó en ESTE turno por un `actualizar_tarea` aceptado (todo o nada,
  validado y, si es personal, dicho por el cliente), solo si la afirmación declara ese dato
  (`Afirmacion.datos`, campo nuevo y opcional). No respalda montos, links ni otras afirmaciones; sin el comando,
  o con uno rechazado, sigue escalando.
- `pack_commerce`: `dato_de_contacto_guardado` declara los datos de contacto de sus procedimientos ("Anoté tu
  dirección" tras fijarla escalaba el 75 % de los pedidos a domicilio en v3) y `cotizacion_generada` también la
  respalda `cotizar_plan` (la cotización de una unidad de `contratar_plan`).

### «Preparar mis datos» deja el negocio de prueba listo para el Agent OS (HU-QA-09, 2026-10-18)

- Centro de pruebas (solo con `QA_TESTERS_ENABLED` y `AOS_CONNECTION_ROUTING`): el negocio `qa-` del tester queda
  con chat web activo (clave y enlace `/w/<clave>` de la landing), backfills hechos, `aos_runtime = v3`,
  `aos_procedures`, los agentes General, Ventas y Agenda en el chat web con las reglas del Coordinador,
  la release activa y `aos_routing = active`. Adenda de ADR-0135.
- `POST /admin/qa/datos` con los datos ya listos responde 200 y solo pone al día el Agent OS (antes 409); la
  respuesta y `GET /admin/qa/datos` traen `agent_os` (estado, motivo, chat web y agentes).
- `chat_web_conexion.guardar_widget`: la escritura del widget, compartida por Configuración y el centro de pruebas.
- El negocio de prueba trae el plan `QA-PLAN-MENSUAL` (150.000 COP mensual) para `contratar_plan`; volver a pulsar
  lo agrega a los negocios de prueba anteriores. Sin cobro en línea (no se copian claves de Wompi).

### Pausar una conexión manual o de WhatsApp Web la detiene (2026-10-19)

- Corrección de seguridad (HU-AOS-23, #472; adenda 2026-10-19 de ADR-0122). Una conexión sin cuenta de Meta
  (manual, WhatsApp Web, correo, chat web) desactivada o eliminada ya no responde: lo que entra se guarda sin correr
  la IA (`blocked_by = connection_paused`) y nada sale por ella (respuestas en vuelo, crons de packs, avisos por
  evento, operador). Push «Conexión pausada» a los dueños, una vez al día, con `scope` en el payload.
- La consola de conexiones marca como `pausable` también las conexiones manuales y de WhatsApp Web.

### pass^k de los procedimientos con el LLM real, a demanda (2026-10-18)

- Workflow `evals-procedimientos.yml` (solo `workflow_dispatch`, nunca en PR ni en push): corre el set de
  journeys `procedimientos` con `k` intentos (4 por defecto) sin el motor (`legacy`, línea base de un agente)
  y/o con el motor (`v3` + `aos_procedures`), con `patron` opcional, y sube los dos reportes como artefacto.
  Comparte la concurrencia con `evals-nightly` y usa `secrets.LLM_API_KEY` (ADR-0112, ADR-0119).
- `evals/journeys.py --runtime` en un set con motor de procedimientos: `legacy` ya no enciende el motor y
  `v3` lo enciende (antes `v3` salía con error y `legacy` igual encendía el motor). Sin `--runtime`, todo como
  antes. La corrida se guarda como `<set>@<runtime>` y el comando de regrabar incluye el runtime.
- Journey nuevo `04-agendar-una-demo` (procedimiento `pack_calendar.agendar_cita`): el set `procedimientos`
  suma `pack_calendar` con agenda interna. `promesa_de_pago` y el journey de enrutamiento quedan pendientes.

### Fallas de la primera medición de pass^4 de los procedimientos (2026-10-18)

- `pack_calendar`: el `calendar_completion_check` ya no lee como horario libre las formas genéricas
  ("tenemos disponibilidad", "hay cupos", "hay espacio", "queda libre") sin un dato de agenda en la misma
  oración (hora, día, fecha, cita, demo…). En un negocio que vende y agenda, "¡Sí, tenemos disponibilidad! La
  hamburguesa…" bloqueaba dos veces y escalaba el pedido. Con un dato de agenda sigue bloqueando.
- Loop: al escalar por un guardrail o por el verificador queda una fila `core:guardrail / guardrail_escalated`
  en `audit_log` con el identificador y la etiqueta de la afirmación (sin texto del borrador), también en el
  canal `test`.
- Journeys: el intento que termina en «relevo» dice el motivo (`guardrail=…`, `verificador=…`) y el reporte
  trae `runtime` con lo pedido y las banderas efectivas del negocio (`aos_runtime`, `aos_procedures`).
- Set `procedimientos`: `verifica` de `pack_commerce_order_items` con `"orden": "id"` (no tiene `created_at`) y
  `$no_nulo` como objeto en `03` y `04`. El test de journeys rechaza un operador escrito como string.

### Mi agente y su data (2026-10-18)

- HU-AOS-47, ADR-0140. El dueño ve cada agente y lo que hizo: API de lectura por agente
  `GET /api/v1/admin/agents/overview` y `GET /api/v1/admin/agents/{ref}/{conversations,customers,tasks,records,usage}`,
  paginada y filtrable por conexión y período (30 días por defecto, máximo 366). `ref` es el id del agente o `general`.
- Procedencia de los registros que crea un agente (pedidos, leads, citas, promesas) en la tabla nueva
  `agent_record_origins` (migración 0095, append-only, RLS). La escribe el loop solo con el runtime v3; cada pack declara
  en la tool qué crea (`MCPTool.crea_registro`) y cómo reconocer un registro nuevo.
- El General absorbe lo no atribuido (turnos legacy o de antes de v3): un negocio sin agentes ve un General con lo que haya.
- Sección de permisos nueva `agents` («Agentes»). Conversaciones y clientes exigen además `conversations`; los registros
  se filtran por el permiso del pack. El costo en USD solo sale con la sección `costs` (sin ella va en null).
- Índice `ix_conversation_tasks_owner` (CONCURRENTLY) para las tareas por dueño.
- Pendiente: `/admin/plan` con `agentes` y `colaboradores` (depende de HU-BILL-05) y pass^k por agente (HU-AOS-21).

### El horario de atención documenta su índice de días: 0=lunes … 6=domingo (2026-10-18)

- API de Configuración: `IdentitySettings.business_ranges` e `IdentityUpdate.business_ranges` describen en el
  openapi que `day` va de 0=lunes a 6=domingo, el criterio de `within_business_hours` (ADR-0051). El panel y la
  app mostraban «0=Dom…6=Sáb», así que un horario cargado como lunes se aplicaba el martes; se corrigen en sus
  repos. El backend no cambia de convención ni de comportamiento.
- Tests: ida y vuelta de «lunes 08:00–18:00» por `PUT /api/v1/admin/settings/identity`, que el runtime abre el
  lunes y no el martes ni el domingo.

### Fuera de horario sin mensaje ya no deja al cliente sin respuesta (2026-10-18)

- Horario de atención (adenda 2026-10-18 de ADR-0051): si el negocio tiene `business_hours` pero no escribió
  `out_of_hours_message`, el cliente recibe un aviso por defecto sin LLM, en el idioma del negocio y con el nombre
  del asistente si lo tiene: «¡Hola! Ahora estamos fuera de nuestro horario de atención. Te respondemos apenas
  volvamos.». Sale una vez por conversación cada 12 h; los mensajes siguientes quedan en silencio
  (`blocked/out_of_hours`, `silent`). El texto vive en el playbook del kernel `fuera_de_horario`.
- Con mensaje configurado no cambia nada.
- API: `GET /api/v1/admin/settings` y `PUT .../identity` devuelven `identity.out_of_hours_default_message`
  (solo lectura) para que el panel muestre qué recibe el cliente.

### Enrutamiento entre especialistas y modo sombra (2026-10-18)

- HU-AOS-17 (#378), ADR-0138. El Coordinador elige qué especialista atiende el turno, sin LLM: tarea en foco
  (sin clasificar) → reglas del KernelSpec (`routing.rules`: botón, palabras, hecho del cliente, etapa, canal,
  horario) → disparadores léxicos sobre los ejemplos de los procedimientos y del agente → el de siempre. Con
  estabilidad del dueño (`maxOwnerChangesPer6Turns`).
- Bandera nueva por negocio `tenants.aos_routing` (`off` por defecto, `shadow`, `active`; exige
  `aos_runtime = v3`), migración 0094. En sombra decide y registra en `core_decisions.shadow_route` pero atiende el
  de siempre; en activo escribe `route_source` (`task`, `rule`, `trigger`), `rule_id`, `candidates`, `confidence`
  y el dueño de la conversación. Una regla `start` hace que el motor inicie el procedimiento al empezar el turno.
- CLI del master: `agent-core db ruteo --tenant X --modo off|shadow|active` y
  `agent-core db reporte-ruteo --tenant X --dias 7` (precisión y recall por procedimiento de la sombra).
- El build de la release rechaza reglas que apuntan a un agente o procedimiento que no está (`ruteo_invalido`).
- Las tareas que inicia el motor llevan como dueño al especialista del turno.
- Golden de ruteo de 80 casos (`tests/fixtures/ruteo/golden.jsonl`). Con la bandera apagada no cambia nada.
- Con los presets v2 (HU-AOS-46) el General también declara los procedimientos de los especialistas: el
  router le da cada procedimiento al especialista más específico (el que declara menos). Probado de punta a
  punta con el preset `suscripciones` v2 y el ruteo en `active`.

### Editor de agentes desde plantillas (2026-10-18)

- HU-AOS-24 (#386), adenda de ADR-0139. API para que el dueño cree un especialista desde una plantilla (Ventas,
  Cobranza, Agenda, Suscripciones) y lo publique sin ayuda técnica:
  - `GET /admin/agent-templates` y `POST /admin/agent-templates/{key}/agents`: plantillas con sus superpoderes y
    frases, y el agente con su borrador derivado (sin publicar; 409 `key_taken` o `template_unavailable`).
  - `GET /admin/agent-superpowers`: superpoderes que ofrece el negocio con sus frases.
  - `GET /admin/agent-models`: modelos de la tabla de precios del core, cuáles sirve el proveedor del negocio, el
    modelo BYOK si manda y el costo estimado por 1.000 turnos con el consumo real (sin consumo, sin estimado).
  - `GET /admin/agents/{id}/publish-targets` y `POST /admin/agents/{id}/publish`: D-35, no se publica sin al menos
    una conexión. Publica la versión, instala el agente y construye la release candidata de cada conexión, sin
    activarla. Errores por campo (superpoderes con menos de 3 frases, modelo de otro proveedor, conexiones).
- Sin migración. El límite de agentes del plan Free queda pendiente (no hay métrica).

### Consola de conexiones (2026-10-18)

- HU-AOS-23 (#385). `GET /api/v1/admin/connections/console` (sección `connections`, paginado): por cada
  conexión del negocio (manuales, la canónica de cada número de Meta y el chat web), su Coordinador y sus
  agentes, con la versión que corre en la release activa y la última publicada. Trae además la release
  activa y la canary, la salud (con cuenta de Meta manda el estado de la cuenta, ADR-0122), el alcance de la
  pausa y el consumo de 7 días (turnos, conversaciones y tokens; `cost_usd` solo con acceso a `costs`).
- En el sobre: `runtime` y `routing` (`tenants.aos_routing`, solo lectura), el pass^k del último run del
  negocio, `can_edit` y los agentes publicados sin ninguna conexión (D-35).
- Activar, desactivar, marcar principal o eliminar una conexión manual o una cuenta de Meta deja una fila
  en `audit_log` (`connection.*` y `meta_connection.*`).
- Solo lectura y sin migración: usa los índices de 0071.

### Confirmar pidiendo el link y relevo por tool en los journeys (2026-10-18)

- Léxico de confirmación (`core/playbooks/kernel/confirmacion.md`, `## cortesia`): pedir el paso siguiente
  ("pasame/mandame/enviame el link (de pago)", "espero el link") ya no impide confirmar. "Sí, confirmo. Pasame el
  link de pago" confirma; el núcleo sigue siendo obligatorio, así que "Pasame el link", "Sí, pasame el link", "no,
  pasame el link" o "¿me pasás el link?" no confirman. Casos nuevos en el set etiquetado (precisión 1,0).
- Journeys: un `escalate_to_human` exitoso termina la conversación simulada como «relevo» (en el canal `test` la
  tool no pausa el bot y el intento llegaba al tope de turnos); el motivo dice `tool=escalate_to_human`.

### Agenda captura el lead del prospecto (HU-AOS-22, adenda de ADR-0139)

- La plantilla `agenda` suma `pack_commerce` como habilidad opcional, acotada a `crear_lead` con el campo
  nuevo `acciones_opcionales` de `PlantillaAgente`. Si el negocio tiene `pack_commerce`, Agenda registra el
  lead de quien se presenta y pide una demo. No recibe la cotización, el pedido, el pago ni el playbook `ventas`.
- `derivar_contenido` aplica el filtro: de una opcional con `acciones_opcionales` solo entran esas acciones (y
  avisa si una no es de esa habilidad). Una opcional sin entrada sigue entrando completa (Ventas con
  `pack_payments` no cambia). El editor de agentes usa la misma derivación.
- Un cliente que ya es lead no se duplica: `crear_lead` responde `ya_existe`.
- **Compatibilidad:** el cambio llega solo a los Agenda que se creen desde ahora (`presets-v2 --aplicar` en un
  negocio sin Agenda, o el editor). Re-aplicar el preset no publica sobre una Agenda existente (ADR-0139 §6).
  En un negocio que ya la tiene, se suma el binding `crear_lead` al borrador de Agenda (por la API: el panel
  todavía no edita `tools`) y se publica.
- Sin migración y sin cambios en el loop ni en el router. Con el ruteo apagado nada cambia.

### Presets v2: agentes especialistas y reglas del Coordinador (2026-10-18)

- HU-AOS-46 (ADR-0139): los presets declaran sus agentes «para un fin» (`agentes`): comercio y restaurante →
  Ventas; cobranza → Cobranza; suscripciones → Ventas, Agenda y Suscripciones. Los cuatro presets pasan a `version` 2.
- Plantillas de agente como configuración del core (`core/presets/plantillas/`), sin prompts.
- En un negocio con `aos_runtime = 'v3'` y `AOS_CONNECTION_ROUTING`, aplicar el preset crea cada especialista
  (versión derivada del General, con sus procedimientos y acciones), su instancia en las conexiones con sus
  habilidades (el General sigue por defecto), las reglas `keywordAny` del Coordinador y la release candidata de
  cada conexión, sin activarla. Idempotente: no duplica ni pisa lo que el dueño cambió.
- CLI `agent-core db presets-v2 --tenant <slug>|--todos [--preset <id>] [--aplicar]` (dry-run por defecto) para
  negocios existentes.
- Con las banderas apagadas no cambia nada. Sin migración.

### P0 de seguridad del ciclo de pruebas: suspensión, permisos, sesión y propietarios (2026-10-17)

- **ADR-0136.** Cuatro defectos reportados por los testers en test (ciclo 2026-10-13).
- **Negocio suspendido sin acceso:** el dueño y los colaboradores de un negocio `suspended` no inician sesión
  (403 `tenant_suspended`, sin crear sesión) y una sesión abierta responde 403 en la API, el panel clásico y la
  app de campo; `POST /auth/logout` sigue funcionando. Con dos negocios solo se corta el suspendido; el master y
  el tester entran igual. `negocios_de_cuenta` (`core/shared/tenant.py`) es el único filtro.
- **Permisos revocados que se aplican:** migración `0090` agrega el nivel `none` (denegado, solo por negocio) a
  `admin_permissions`. «Revocar» una sección a un miembro guarda la denegación en vez de volver al default del rol;
  revocar un pack completo (`pack_cobranza`) deniega todas sus sub-secciones; la fila de una sub-sección manda
  sobre la del pack. `/admin/me` y `require()` lo reflejan en el siguiente pedido; `/me` expone también el acceso
  por pack. `grant` admite `access: "none"`; CLI `admin grant --access none --tenant <slug>`.
- **Cuenta desactivada sin sesión:** desactivar o cambiar la contraseña desde Colaboradores cierra sus sesiones en
  la misma transacción; reactivar no las resucita.
- **Propietarios pares y desvincular:** un owner solo gestiona colaboradores de su negocio (no a otro owner ni al
  real) y no crea otros propietarios; nuevo `POST /api/v1/admin/collaborators/{id}/unlink` (`{tenant}`) con las
  mismas guardas, sin dejar un negocio sin propietario, que además revoca las invitaciones pendientes de esa cuenta a
  ese negocio. El tester sigue la misma regla (gestiona colaboradores; puede crear propietarios de prueba).
- **App de campo (pack-cobranza):** un único validador de tokens para la API de gestores y la de dispositivo:
  rechaza el refresh, exige negocio activo y cuenta habilitada y vinculada; `/auth/refresh` recalcula rol y gestor
  desde la membresía actual; las rutas de invitaciones no aceptan cuentas desactivadas; un token de dispositivo en
  `/gestor/*` cuenta como `gestor`.
- **Migración 0090:** `lock_timeout` de 5 s; el downgrade borra las denegaciones (reabre lo revocado).
- Tests de integración `aislamiento` en `tests/integration/test_p0_seguridad_pruebas.py` y unitarios del resolutor.

### P0 de correctitud del ciclo de pruebas 2026-10-13 (2026-10-17)

- **Formularios ya no aceptan negativos.** Los modelos de entrada y las configuraciones de los
  packs llevan el rango explícito (`ge`/`le`): reglas de cobranza (prórroga, reprogramaciones,
  abono mínimo, umbral) y su config (tolerancia, anticipación, mora, aviso); calendar (duración,
  bloque, margen, recordatorios, duración de tipos de cita); commerce (seguimiento, conteos y
  días de segmentación, montos, mínimo de pago en línea, precio, stock, días de prueba, orden);
  payments (vigencia del link); subscriptions (días de prueba, precio); AppSettings (reintentos
  de guardrails, umbrales de dificultad, turnos de contexto 1–200, top-k 0–20, umbral 0–1,
  temperatura 0–2, tools por turno 1–50, presupuesto); `extend_days` de la plataforma.
  `GET /admin/plugins/{id}/config` ya no revienta con una config guardada antes fuera de rango.
- **Errores de validación con el sobre de la API.** Bajo `/api/v1` un cuerpo inválido responde
  422 `validation_error` con `error.fields` por campo en español (`core/shared/validacion.py`),
  y la config de un pack inválida responde 422 `invalid_config` con los mismos campos (antes
  400 con mensajes en inglés). `ApiError` admite `details` (estado real para el cliente).
- **Habilidad al tope del plan:** 409 `plan_limit_plugins` con `details = {metric, used,
  limit, enabled: false}` (antes 403); la cuota se decide antes de guardar la configuración.
- **Plantillas de WhatsApp:** el botón «Sincronizar» ya no espera el candado horario del cron
  (lo toma al terminar) y se limita a una vez por minuto (429 `sync_rate_limited`). Eliminar una
  plantilla que Meta no permite borrar (las de ejemplo, `(#100) Invalid parameter`) responde 409
  `template_not_deletable` con el motivo y la fila no se marca eliminada; ningún error de red
  termina en 500.
- **Conexiones de Meta:** los números dados de baja ya no aparecen en el listado ni cuentan
  (`deleted_at`); el límite de números al agregar uno se explica como límite de Meta (no hay
  cuota de números por plan); `primary` sobre una cuenta de Meta responde 409
  `primary_not_applicable` con la regla (ADR-0069/0115); el alta BYOK con la misma app y
  entorno devuelve `reused: true` porque actualiza la conexión existente en vez de crear otra.
- **Revisiones de seguridad y multitenant:** topes superiores (lo que cabe en la columna) y sin
  `inf`/`nan` en precio, stock, orden, mínimos y días de segmentación, montos de escalamiento,
  precio de suscripción y umbral de cobranza; el error de config de un pack y el de
  `checkout_fields` ya no repiten el valor recibido; si Redis no responde, «Sincronizar» sigue
  sin límite en vez de dar 500, y un sync fallido no consume el minuto; eliminar una plantilla
  exige que sea de la WABA de esa conexión y solo marca eliminadas las filas de esa WABA.
- **Regla que cambia:** `guardrail_max_retries` fuera de 0–5 responde 422 (antes se recortaba a 5
  en silencio).

### El loop ejecuta la release v3 y migración por negocio (2026-10-17)

- HU-AOS-15 (parte 1, adenda de ADR-0117). Con `tenants.aos_runtime = 'v3'` el turno ejecuta la versión
  congelada del especialista en foco (`agentspec/3`) y el KernelSpec del Coordinador de la release de la
  conversación, no el AgentSpec v2 de `agent_specs`: persona, modelo, tools, guardrails, playbooks y
  `memory.writeNamespaces` salen de `agent_versions` (vista v2 inversa, `core/agents/spec_de_release.py`).
  El rollback de release es completo. Con `legacy` nada cambia (gate en replay sin regrabar).
- `ToolContext.write_namespaces` se aplica; se escriben `conversations.active_agent_instance_id`,
  `usage_ledger.{release_id, agent_instance_id, agent_version_id, connection_id}`,
  `core_decisions.active_instance_id/focus_task_id` y `turn_outcomes.final_instance_id`.
- `agent-core db migrar-a-v3 --tenant <slug>|--todos [--dry-run] [--volver] [--construir] [--apagar-procedimientos]`: enciende
  `aos_runtime = 'v3'` solo con los backfills hechos, `AOS_CONNECTION_ROUTING` y la release activa al día.
- `evals/journeys.py --runtime v3`: el mismo set con el runtime por releases, para comparar pass^k.

### Habilidades por conexión y procedimientos por agente, parte 1 (2026-10-17)

- **HU-AOS-16 (parte 1), adenda de ADR-0118.** Cada agente de una release ve solo los procedimientos
  que declara su versión, resueltos en el build a una versión exacta contra lo que publica la versión
  congelada de cada habilidad (`release/2`: `components[].procedures` e `is_default`).
- **API nueva** `/api/v1/admin/skills/installations` (GET/POST/DELETE): instala una habilidad solo en
  una conexión; esa conexión congela en su próxima release solo sus instalaciones. El pack tiene que
  estar habilitado en el negocio (409 `pack_not_enabled`); el scope de negocio sigue siendo la
  habilitación de siempre.
- **El General del lector tolerante declara** los procedimientos publicados de los packs habilitados
  (`^M.0`): con él por defecto, el modelo ve lo mismo que sin release.
- **Oferta fail-closed por agente y por scope** con el motor activo: nada de un pack fuera de la
  release; `iniciar_procedimiento` solo con los procedimientos del agente en foco.
- **Cambio de release sin escalar:** una tarea sigue si la release nueva trae el mismo procedimiento
  con el mismo major.
- El detalle de una versión de agente (`GET /admin/agents/{id}/versions/{vid}`) trae sus
  `procedures` resueltos.
- **Sin cambio** con las banderas apagadas; sin migración. El primer build tras el deploy crea una
  release nueva por conexión (`release/2` entra al hash).
- **Un solo modelo de agentes de la release** con HU-AOS-15: los procedimientos por agente cuelgan del
  `AgenteCongelado` y se aplican al especialista en foco que elige `elegir_foco`; con `release/2`, el
  `is_default` de la conexión gana sobre el `routing.defaultAgent` del KernelSpec.

### Rol tester: master en la operación y dueño en Colaboradores, solo en test (2026-10-13)

- **SPEC-30, HU-QA-04, ADR-0134.** Un tester actúa como master en la operación y como dueño del negocio activo
  en Colaboradores. Solo existe con `QA_TESTERS_ENABLED=true`, que se carga únicamente en test.
- **`core/admin/roles.py`** separa operar (`actua_como_master`) de gestionar cuentas (`role == "master"`). Pasan
  a la regla nueva:
  - los permisos por sección (`effective_access` y `permissions_for`);
  - las rutas de plataforma y las de Meta reservadas al master;
  - la escritura de agentes y releases;
  - cerrar un ciclo de pruebas.
- **Colaboradores:** el tester crea, invita y gestiona las cuentas exclusivas del negocio activo, como un
  dueño. Gestionar cuentas de plataforma sigue siendo solo del master: crear masters, tocar cuentas de
  master o tester, vincular entre negocios, testers e importar el catálogo. Un owner no puede tocar ni
  invitar a un tester.
- **Migración 0088:** `admin_tenants()` incluye a los testers. Suma la tabla de sistema `qa_tester_invitations`.
- **Invitación de testers:**
  - API (solo master): `GET/POST /api/v1/admin/qa/testers` y `POST /api/v1/admin/qa/testers/{id}/revoke`.
  - El correo lleva un enlace de un solo uso, que también se devuelve al master.
  - Canje en `POST /api/v1/auth/accept-tester-invitation`, con el bloqueo progresivo del login.
- **Sin la variable**, un tester no entra al panel ni a la app de campo, y sus sesiones dejan de valer.
  `/me` expone `qa_testers_habilitados`.
- **CLI:** `agent-core admin create --role tester`, rechazado si el rol no está habilitado.
- **Límites del tester (revisión de seguridad):**
  - el alta de negocios solo admite slug y cuenta nuevos;
  - no purga, no invita gestores existentes ni marca el checklist de producción;
  - sin contraseña no recibe código de recuperación.
- **App de campo:** revalida la cuenta en cada pedido y rechaza el refresh token como acceso. Desactivar a un
  admin ahora corta la app al instante.

### Datos de prueba por tester y catálogo por entorno y sección (2026-10-13)

- **SPEC-30, HU-QA-07, ADR-0135.** «Preparar mis datos» crea, solo en test, un negocio de prueba por tester con
  Comercio, Agenda y Cobranza, datos de ejemplo y cuentas de dueño, gestor y colaborador en el dominio reservado
  `pruebas.solvershub.test`. Las contraseñas se muestran una sola vez; «Contraseñas nuevas» las rota y cierra
  sesiones (`/api/v1/admin/qa/datos`).
- **Las cuentas de prueba siguen al tester:** sin `QA_TESTERS_ENABLED` no entran, y revocar al tester las desactiva.
- **`seed_data` de Agenda y de Cobranza** (tipos de cita, citas y bloqueos; cartera, caja, gestor y clientes al día,
  en mora y sin préstamo) y fragmentos `presets/qa.json` de los tres packs.
- **HU-QA-08.** El catálogo suma `entorno`, `area`, `requiere` y la sección `datos` (guía de datos externos).
  Migración 0089.

### Conversación ordenada con el motor de procedimientos (2026-10-13)

Solo con `tenants.aos_procedures`; con la bandera apagada (y en sombra) nada cambia. Adenda
2026-10-13 de ADR-0113 y de ADR-0125. Refs #368.

- **Sin escalamiento falso por el tope de vueltas:** las vueltas de solo comandos del hilo no gastan
  el tope (hasta 3) y, si se agota con una tarea en foco, el kernel pide lo que falta en vez de
  decir "te comunico con una persona".
- **Datos en orden y en un solo mensaje:** la tarjeta pide todos los datos que faltan como lista
  numerada (negocio, tipo, nombre, documento, correo, teléfono, forma de pago) y, si el cliente da una
  parte, solo lo que falta. Al cambiar de etapa a mitad del turno el modelo ve la tarjeta vigente.
  `contratar_plan` 1.2.0 (pack-commerce 2.3.0).
- **Un resumen único** con el plan, el total, los datos del cliente (el correo ya no se confirma
  aparte) y, en el vendedor de planes, los términos, la privacidad y el DPA, que se aceptan con el
  mismo «Sí, confirmo». `crear_pedido` sigue exigiendo la aceptación registrada.
- **Mensajes del kernel claros:** viñetas, monto como $600.000 y la instrucción en una línea al
  final; rótulos humanos de los datos que publica cada pack (`register_rotulos`).
- **Tipo de negocio por sinónimos** ("tienda" → comercio, "pizzería" → restaurante), metadato de los
  presets.
- **Un solo mensaje con el pedido y el link:** no se repite el link que el texto ya trae ni el
  "✅ quedó confirmado" con pago en línea.

### Centro de pruebas: modelo, API e importación del catálogo (2026-10-13)

- **SPEC-30, HU-QA-01 y HU-QA-02.** Base del centro de pruebas del panel. Este PR no trae el rol tester (HU-QA-04)
  ni la pantalla (HU-QA-03).
- **Migración 0087 con nueve tablas de sistema `qa_*`, sin RLS** (ADR-0133). Guardan catálogo por versión,
  ciclos, ejecuciones por tester con pasos, notas y capturas, informe congelado, configuración de test y de
  producción, e historial de solo agregar.
- **API `/api/v1/admin/qa/*` con la sección global `qa`:**
  - catálogo con el estado consolidado del ciclo;
  - mi ejecución: empezar, marcar pasos, nota, capturas y enviar el informe;
  - informes en JSON y Markdown;
  - historial;
  - ciclos: listar, y cerrar abriendo uno nuevo;
  - configuración de test y prod;
  - importación, solo para master.
- **CLI `agent-core qa validar|importar <catalogo.yaml>`.** La importación es idempotente por contenido.
- **Capturas en `TENANTS_ROOT/.plataforma/pruebas/<run_id>/`:** jpeg, png, webp o pdf, hasta 5 MB. Se sirven
  solo con sesión.
- **Correo de sistema:** al enviar un informe se avisa a los masters, con el enlace `PANEL_BASE_URL/pruebas/informes/<id>`.
- **`pyyaml` pasa a dependencia directa.** Antes llegaba de forma transitiva.

### Agent OS: sin tarea en foco no hay camino de siempre para lo que se confirma (2026-10-12)

- Arreglo del hallazgo de la prueba real en test. Con `tenants.aos_procedures` encendida y sin tarea en foco,
  el modelo cerraba una venta con `confirmar_cotizacion` → `crear_pedido` sin iniciar el procedimiento.
- **Las puertas de confirmación no se ofrecen sin foco.** Son las de los procedimientos disponibles del
  negocio (`motor.acciones_de_puerta`): hoy `confirmar_cotizacion`, `create_calendar_event`,
  `cob_registrar_promesa_saldo` y `sub_aplicar_cambio_plan`. Si el modelo igual las llama, se niegan
  (`audit_log denied`) con un texto que nombra el procedimiento a iniciar con `iniciar_procedimiento`.
- **Lo que sigue ofrecido sin foco:**
  - las consultas;
  - `crear_pedido`, que exige una cotización aceptada;
  - `confirmar_pago_pedido`, para reenviar el link de un pedido que ya existe.
- **Pista en la tarjeta de estado.** Con el motor activo, sin foco y con procedimientos que iniciar, la
  tarjeta lleva la pista de iniciar el recorrido. Sale del playbook del kernel `estado-conversacion`, no del
  negocio. La descripción de `iniciar_procedimiento` también se reforzó.
- **Respaldo v2:** con una tarea activa que el motor no gobierna, las puertas de su procedimiento no se ocultan
  ni se niegan. La decisión se relee siempre bajo el lock.
- **Endurecimiento con foco:** una puerta del core que el turno ya ofrecía, como `sub_aplicar_cambio_plan`, no
  pasa por el atajo de las acciones del core. Fuera de su etapa se niega.
- **Bandera apagada o modo sombra:** sin cambio, y el gate en replay pasa sin regrabar. Sin migración.
- Adenda "sin foco no hay camino de siempre" en ADR-0113.

### Agent OS: confirmaciones de datos visibles y sí a secas para datos (2026-10-12)

- Arreglo de la prueba real en test (`contratar_plan`): la confirmación del correo se pidió en
  `elegir_plan`, el cambio de etapa la venció, nadie la volvió a pedir y la tarea quedó atascada en
  `datos`. Solo con `tenants.aos_procedures`; sin migración. El gate en replay pasa sin regrabar.
- **La confirmación de un dato se pide en la etapa que la exige**, cuando es lo único que falta, y sale
  el mensaje del kernel, siempre como texto y sin montos. Al empezar el turno, una tarea atascada así se
  desatasca.
- **"Sí" a secas confirma un DATO personal** (`pii`) cuando contesta directo el mensaje del kernel que
  lo pidió. Lo confirma el kernel al empezar el turno, o `confirmar_tarea`. Acciones y etapas siguen con el sí explícito. Clasificador: asentimiento
  para datos (set nuevo `dato.jsonl`, precisión 1.0) y mejor recall del estricto sin bajar su precisión
  (100 % sobre 213 frases).
- **La tarjeta de estado y el error `fuera_de_etapa` dicen qué falta para salir de la etapa**, así el
  modelo no inventa que le falta una herramienta.
- **Un dato personal que el cliente no escribió en la conversación no se acepta.** Caso real: el nombre
  del perfil de WhatsApp. El teléfono del canal vale solo para un dato de teléfono.
- **Una confirmación de acción vencida se vuelve a pedir** cuando el modelo repite la acción. Ya no
  queda sin salida en canales sin botón.
- Adenda de ADR-0113, en estado propuesta.

### Verificador: "tu pedido del plan Premium quedó creado" sin `crear_pedido` ya no pasa (2026-10-11)

- La afirmación `pedido` de pack-commerce reconoce el participio a más distancia (hasta 5 palabras
  antes de «quedó/está/fue» + participio masculino), el orden invertido («quedó creado tu/su pedido»)
  y la voz activa en pasado («creé/generé/registré tu pedido»). En test (negocio `solvershub`), el
  modelo respondió «Tu pedido del plan Premium quedó creado» sin haber llamado `crear_pedido` y el
  verificador lo dejó pasar (el tope era de 3 palabras): el cliente quedó creyendo que su pedido existía.
- El tramo corto (hasta 3 palabras, sin auxiliar) no cambia, y los resúmenes previos al pedido siguen
  sin contar («datos confirmados», «va a quedar creado», «quedó generada la cotización»), igual que las
  preguntas, negaciones y condicionales.

### API: pausar o reactivar el asistente sin reenviar la identidad (2026-10-11)

- Nuevo `PUT /api/v1/admin/settings/bot-paused` con cuerpo `{"paused": bool}`: cambia solo
  `app_settings.bot_paused` del negocio activo (exige escritura en `settings`). Fija el valor pedido, así
  que repetirlo no cambia nada, y registra `settings.bot_paused` / `settings.bot_resumed` en `audit_log`
  cuando el valor cambia.
- `PUT /api/v1/admin/settings/identity` ya no toca la pausa si el cuerpo no trae `bot_paused` (antes volvía
  a `false`): quien tenía Configuración abierta desde antes de una pausa no la deshace al guardar. Si lo
  trae, lo aplica como antes.
- El panel (HU-UI-03) lo usa para la pausa de la barra superior. Antes leía la configuración y reenviaba
  la identidad completa por `PUT .../identity`, así que dos personas editando a la vez se pisaban
  (ganaba la última escritura).

### Promesa de pago por lo vencido o por el saldo total (2026-10-11)

- **Decisión del dueño (HU-AOS-08):** el procedimiento `pack_cobranza.promesa_de_pago` (2.0.0) ofrece
  prometer por lo vencido o por el saldo total, con los montos calculados por el servidor. Si lo vencido
  es 0 o igual al saldo, hay una sola opción y queda elegida.
- Tool nueva solo de procedimiento `cob_opciones_promesa(eleccion?)`; `cob_registrar_promesa_saldo` pasa a
  `(fecha, alcance, monto)`: los tres ligados a los datos de la tarea, y un monto distinto del servidor no
  se registra. El botón muestra el alcance y el monto del servidor (resumen del pack).
- Regla por negocio `alcance_promesa` (`ambos` por defecto, `vencido` o `saldo_total`) en las reglas de
  negociación (`GET/PUT /api/v1/pack/cobranza/rules`; omitirla en el PUT la conserva). Las promesas guardan
  su `alcance` (sale en `GET /promises`). Migración del pack `pack_cobranza_0017`.
- pack-cobranza 0.6.0. Con `tenants.aos_procedures` apagada no cambia nada: `cob_registrar_promesa_pago`
  sigue igual y las tools nuevas no se ofrecen. Adenda HU-AOS-08 de ADR-0113 actualizada.

### Procedimientos de comercio: contratar un plan y pedido con entrega (2026-10-11)

- pack-commerce 2.1.0 publica `pack_commerce.contratar_plan` y `pack_commerce.tomar_pedido` como datos (`procedures/*.json`), con resolvers (plan, método de pago, tipo y nombre del negocio), la tool `cotizar_plan` (solo con el motor activo) y el resumen de `confirmar_cotizacion` leído de la cotización vigente (HU-AOS-07).
- Motor de procedimientos: argumentos ligados a los datos de la tarea, invalidación y retroceso de etapa cuando un dato cambia, datos congelados tras el efecto en una etapa de riesgo alto y resumen de confirmación provisto por el pack (`MCPHost.register_resumen`).
- El manifiesto de la versión de un pack incluye sus procedimientos publicados (con el cargador `de_pack`): suben pack-calendar 2.2.0, pack-subscriptions 0.2.0 y pack-cobranza 0.5.0.
- Con el motor activo, las frases (`claims`) de los procedimientos disponibles se verifican.
- Journeys: `motor_de_procedimientos` en `negocio.json` y set `procedimientos` (sin grabación; listo para medir pass^4 con OK del dueño).
- Todo solo con `tenants.aos_procedures`; con la bandera apagada no cambia nada de lo que ve el modelo.

### Agent OS: procedimientos de agenda, cambio de plan y promesa de pago (2026-10-11)

- HU-AOS-08 (#369). Tres procedimientos `procedure/1` publicados por sus packs, detrás de
  `tenants.aos_procedures` (apagada por defecto: sin cambio de conducta ni de lo que ve el modelo).
  - `pack_calendar.agendar_cita`: el resolver `horario_ofrecido` rechaza un horario que la agenda del
    servidor no ofrece y devuelve los libres del día; la cita se crea después del botón.
  - `pack_subscriptions.cambiar_plan`: precio y prorrateo del catálogo del servidor (ADR-0111) con las
    tools del core `sub_cotizar_cambio_plan` y `sub_aplicar_cambio_plan` (solo las ofrece el motor). El
    botón muestra el monto del servidor y `change_plan` lo compara con su cálculo en la misma transacción
    (`monto_esperado`). Con el motor activo, `sub_cambiar_plan` no se ofrece.
  - `pack_cobranza.promesa_de_pago`: la fecha la validan las reglas del pack (prórroga máxima, domingo no
    laborable, promesas pendientes) y la promesa queda por el saldo real (`cob_registrar_promesa_saldo`).
- `core/kernel/procedures/de_pack.py`: carga los JSON de `packages/pack-*/procedures/`.
- `core/kernel/oferta.py`: con `aos_procedures` encendida no se ofrecen `sub_cambiar_plan` ni
  `cob_registrar_promesa_pago` (irían sin botón); las acciones nuevas solo se ofrecen con la bandera.
- `pack_subscriptions.capability.evaluar_cambio`: las reglas del cambio de plan en una función pura (sin
  cambio de conducta de `change_plan`).
- Adendas a ADR-0111 y ADR-0113. Sin migración.

### Crons de seguimiento de pack-commerce: el negocio sin configurar ya no frena a los demás (2026-10-11)

- `pack_commerce.follow_up_quotes` y `pack_commerce.follow_up_leads` saltan el negocio que tiene el pack
  habilitado pero sin configurar, con un aviso `follow_up_sin_config` (nivel warning, con `tenant_id` y
  `kind`), y siguen con los demás. Antes el `PluginConfigError` de `load_config` cortaba el recorrido: el cron
  terminaba en `cron_task_failed` y los negocios siguientes se quedaban sin seguimiento (visto en el entorno de
  test, 4 veces en 7 h).
- Los crons de pack-calendar y pack-subscriptions ya caían a la config por defecto, y los de pack-cobranza no
  cargan config: no cambian.

### Comandos del hilo y verificador sobre el estado (2026-10-11)

- HU-AOS-06: con `tenants.aos_procedures`, el modelo ve los comandos del hilo como tools del core
  (`iniciar_procedimiento`, `actualizar_tarea`, `confirmar_tarea`, `cancelar_tarea`), solo los que
  aplican al estado y con JSON Schema estricto. El motor los aplica y el resultado vuelve con la
  tarjeta de estado actualizada y las acciones de la etapa vigente. Un id de procedimiento fuera de
  los candidatos se rechaza sin crear la tarea.
- El verificador (ADR-0119) usa el estado de las tareas como evidencia: los datos que fijó una
  acción respaldan montos, referencias y los cierres de la tarea en foco. Una referencia afirmada de
  la familia que fijó el servidor (`OC-…`) que no coincide falla con `referencia_sin_evidencia`.
- Con la bandera apagada no cambia nada: ni tools, ni prompt, ni veredictos.
- Adendas en ADR-0113 y ADR-0119 (esta última requiere la aprobación del dueño).

### Contratar un plan y tomar un pedido se completan cuando el pago se confirma (2026-10-11)

- Decisión del dueño: con pago en línea, `pack_commerce.contratar_plan` y `pack_commerce.tomar_pedido` (v1.1.0, pack-commerce 2.2.0) ya no terminan al mandar el link, sino cuando se confirma el pago. Esperan en la etapa nueva `esperar_pago`. Con transferencia, contraentrega o plan Free terminan al crear el pedido, como antes.
- Cuando su handler marca un pedido como pagado (con el monto ya validado), pack-commerce escribe el evento `order.paid` en el outbox. Ese evento cierra la tarea cuyo `order_id` coincide, sin cambiar lo que ve el modelo.
- Tool nueva `verificar_pago_pedido`, de solo lectura y disponible solo con el motor activo. Sirve para "ya pagué" e informa si el pago está pendiente o fue rechazado. En la espera, `confirmar_pago_pedido` reenvía el link o genera uno nuevo si el anterior venció.
- Un pago rechazado no cierra la tarea: el cliente puede reintentar. Un cron horario (`core.kernel_esperas_vencidas`) cancela con el motivo `espera_vencida` las esperas sin actividad de más de 72 h. Solo recorre negocios con `aos_procedures`.
- Cambios en el motor:
  - al salir de la última etapa, la tarea se completa aunque declare eventos de cierre;
  - después de un efecto en una etapa de riesgo alto, los datos quedan congelados por el resto de la tarea;
  - el cierre por evento busca las tareas por el valor de la clave, sin el tope de 200 abiertas.
- ADR-0113: la adenda HU-AOS-07 queda aceptada por el dueño, con la nueva sección "Cierre". Se actualiza la adenda HU-AOS-05.

### CI: Postgres y Redis de los jobs con datos en memoria (2026-10-11)

- Los servicios `postgres` y `redis` de `ci.yml` y `evals-nightly.yml` montan `--tmpfs` en su carpeta
  de datos. En el runner self-hosted, GitHub borra el contenedor de servicio pero no su volumen
  anónimo: quedaban ~70 MB por job y el 2026-10-11 eran 1.628 volúmenes (69 GB) en el disco del
  runner, que es la misma máquina de test. Con `tmpfs` no se crea volumen y la base de cada job vive
  en RAM (más rápida; se pierde al terminar el job, que es lo que se quiere).

### El chat web es una conexión y las altas nuevas nacen con sus agentes (2026-10-11)

- HU-AOS-11 segunda parte (ADR-0115, adenda). El widget de chat web pasa a ser una conexión `webchat`
  (`channel_connections`): clave pública aleatoria generada por el servidor, secreto de Turnstile cifrado en
  `credentials_encrypted`, dominios en `settings`. Guardar Configuración → Chat web escribe `app_settings` como
  antes y, en la misma transacción, la conexión. Nada cambia para el negocio ni para el visitante.
- Migración **0086**: tipo `webchat` en el catálogo y resolvedor público `webchat_resolve` (`SECURITY DEFINER`,
  solo datos no secretos). El endpoint público resuelve el widget por su conexión y cae a `app_settings` solo si
  el negocio nunca tuvo una. El polling del widget filtra por conexión: con dos widgets un visitante no ve la
  conversación del otro.
- `agent-core db backfill-conexiones` convierte el widget de `app_settings` en conexión y liga las conversaciones
  web sin conexión (idempotente, con `--dry-run`). `backfill-agentes` ya no avisa «el chat web… queda sin
  Coordinador»: bloquea ese negocio hasta correr `backfill-conexiones`, y luego el widget recibe Coordinador y
  General como las demás conexiones.
- Con `AOS_CONNECTION_ROUTING` encendida, la conversación web sin conexión del visitante se adopta en la de su
  widget (no abre otra ni gasta cuota). Apagada, no cambia nada.
- Altas posteriores: los números nuevos que trae la sincronización con Meta (`sync_phone_numbers`) nacen con su
  conexión canónica; el widget, las conexiones manuales y `waweb` que se crean o reviven después del backfill
  reciben Coordinador y General si el negocio ya tiene agentes. La release de esas conexiones sigue saliendo de
  `agent-core db backfill-releases` (HU-AOS-15).
- Orden de despliegue: migración 0086 antes del código; después, `backfill-conexiones --dry-run`, revisar y
  correr, y luego `backfill-agentes` y `backfill-releases` en los negocios que ya los tenían.

### Worker y beat de Celery: logs en JSON y tracebacks sin variables locales (2026-10-11)

- El worker y el beat no llamaban a `configure_logging`: structlog quedaba con su default y los
  errores de las tareas (p. ej. `cron_task_failed` de `pack_commerce.follow_up_quotes` en test) salían
  con el traceback de rich y las variables locales de cada frame (`│ nombre = valor`), donde podía
  quedar un token o datos de un cliente. Ahora `celery_app` atiende la señal `setup_logging` (worker,
  beat y cada proceso hijo del worker) y también configura en `worker_init`, que llega antes.
- Con eso Celery ya no reemplaza el handler del logger raíz ni redirige stdout a `celery.redirected`:
  cada línea es un objeto JSON, con el traceback en texto plano en `exception`. Los registros de
  `logging` (los de Celery, como `Task … raised unexpected`, httpx, SQLAlchemy) también salen en JSON,
  en todos los procesos que llaman a `configure_logging` (API y connector incluidos).
- Red de seguridad: un proceso que importa `core.shared.logger` sin configurar nada usa la consola
  sin variables locales en vez del default de structlog.
- El connector ya llamaba a `configure_logging` en su `main()`; no cambia salvo por los logs de
  `logging`, que ahora también van en JSON.

### Seguimientos del kernel del Coordinador: ids del debounce y turno en Celery (2026-10-11)

- HU-AOS-10, #438. El turno que dispara el debounce registra en `core_decisions.inbound_message_ids` todos los
  mensajes de la ráfaga. Los ids viajan en una clave Redis nueva, `debounce:inbound:{tenant_id}:{conversation_id}`
  (mismo TTL y pipeline que el texto), que la purga del negocio barre y que sobrevive al reencolado por cupo
  saturado.
- HU-AOS-10, #439. Las tareas Celery que lanza un turno escriben con su `turn_id` (`messages`, `usage_ledger`,
  `audit_log`): el turno viaja en headers del mensaje y el worker lo fija al empezar cada tarea, con la misma guarda
  de negocio del default de columna. El reintento lo conserva; las tareas nuevas que encola una tarea no lo heredan.
- Sin migración, sin bandera nueva y sin cambio de conducta (gate en replay sin regrabar). Adenda en ADR-0112.

### Canal WhatsApp por QR: el connector recicla los sockets zombie (2026-10-10)

- `WaWebSocket` expone `alive()`; `NeonizeSocket` lo implementa con `is_connected` / `is_logged_in`
  del cliente de Neonize (en `neonize.aioze` son corutinas: se esperan, bajo `_BRIDGE_LOCK`).
- El drain del manager consulta `alive()` cada 30 s mientras la sesión está conectada. Si lleva 60 s sin
  conexión, registra `waweb_socket_zombie`, manda una alerta `warning`, marca la sesión `disconnected` y
  recicla el socket con la sesión guardada. Antes, si el auto-reconnect interno de whatsmeow fallaba sin
  emitir `DisconnectedEv` (`Error sending close to websocket ... EOF`), la sesión seguía en `connected` y
  los mensajes entrantes se perdían en silencio (en test, 4 días desde el 2026-10-06 y otra vez el
  2026-10-10).
- Un negocio que se vinculó por QR dentro de la tarea actual ya no pierde la sesión al reconectar: antes,
  si la tarea había arrancado en `pending_qr`, cualquier reciclado (desconexión o zombie) borraba las
  credenciales y volvía a pedir QR.

### Reset de WhatsApp (QR) con la sesión vinculada (2026-10-10)

- Re-vincular desde el panel ya funciona aunque el connector tuviera la sesión conectada: antes el
  manager volvía a guardar las credenciales viejas al cerrar el socket (pisando el reset) y el
  siguiente ciclo no limpiaba porque el estado leído al arrancar la tarea seguía en `connected`. Ahora,
  con un reset pendiente, no se persiste el directorio de trabajo, y el ciclo siguiente borra el blob
  y el `session.db` local antes de reconectar, así se emite un QR nuevo.

### Test de puertos determinista (2026-10-10)

- `test_choose_port_ignora_un_previo_que_ya_no_es_nuestro` ya no depende de que el puerto 49600 esté libre
  en la máquina: fallaba al azar en el runner compartido (PRs #401 y #410).

### Enlaces de los términos, cotización perdida y línea de pago repetida (2026-10-10)

Incidente en test (tenant `solvershub`, 2026-10-10). Se apila sobre el arreglo de los términos del
alta (marcador fijo y adenda de ADR-0119).

- **Set etiquetado del verificador (ADR-0119).**
  - `evals/verificador.py` cuenta como falso positivo un link quitado de una respuesta correcta.
    Antes solo miraba si el borrador pasaba, y quitar un link no lo rechaza.
  - Una falsa por un link inventado (`urls_sin_respaldo`) cuenta como detectada si el link se quitó.
  - Casos nuevos en `respuestas_etiquetadas.jsonl`: el incidente (`terminos_pendientes` con los
    tres enlaces), un link inventado junto a los legales, una URL repetida en el `error` de una
    tool y un cierre falso con los términos pendientes.
  - `test_verificador_set.py` exige que ningún caso de incidente falle.
- **Cotización perdida entre turnos (`pack_commerce`).** Entre turnos el modelo solo ve texto y
  pierde el `quote_id`. En test inventó `confirmar_cotizacion("Q-0001")` después de que la clienta
  aceptó.
  - `aplicar_promocion`, `confirmar_cotizacion` y `crear_pedido` responden «Cotización no
    encontrada» con un `hint`: el `quote_id` es el de `generar_cotizacion`, no se inventa, y si no
    está se genera la cotización otra vez con los mismos productos. Una cotización ajena o
    inexistente sigue respondiendo igual.
  - El `hint` de `terminos_pendientes` pide repetir `crear_pedido` con el mismo `quote_id` y los
    mismos datos.
- **Línea de pago repetida (SafeRenderer).** Si el modelo imitó la línea de un `_safe_append`
  («💳 Podés pagar en línea aquí: …») y el verificador le quitó la URL, quedaba la línea sin link y,
  debajo, la completa. Ahora, antes de agregar el append, se quita del texto la línea sin URL que
  repite una línea con URL del append (aunque cambien la viñeta o la puntuación final).
- **Tests.**
  - `test_safe_renderer.py`: línea huérfana con y sin viñeta, texto que solo tenía esa línea, y que
    las otras líneas no se toquen.
  - `test_journeys_terminos_alta.py`: el incidente con un modelo guionado. La clienta responde
    «Si», el modelo inventa la cotización, la regenera con el `hint` y el alta queda aceptada. El
    token es de la conversación, no de la cotización.

### Suscripciones: las tools `sub_*` se ofrecen desde el primer arranque (2026-10-10)

- El predicado de oferta de `sub_consultar`, `sub_link_pago`, `sub_pausar`, `sub_cancelar` y
  `sub_cambiar_plan` lee los proveedores del registro de suscripciones en cada request. Antes los
  calculaba una sola vez, al registrar las skills del core, y ese conjunto quedaba fijo en el proceso.
- En la API (`core/main.py`) las skills del core se registran al importar el módulo y los packs se
  activan después, en el lifespan: el conjunto quedaba vacío y ningún negocio con
  `pack_subscriptions` recibía las `sub_*` en el consumer, el debounce ni el playground, en ningún
  arranque (reiniciar no lo arreglaba). El worker de Celery no corre turnos del asistente, así que no
  lo afectaba.
- `evals/journeys.py` sobre una base recién migrada ya ofrece las `sub_*` desde la primera corrida
  (antes solo desde la segunda, visto en el PR #409).
- Sin cambios en qué tools se ofrecen con todo cargado: siguen saliendo solo si el negocio tiene
  habilitado un proveedor de suscripciones registrado, y dejan de ofrecerse si el pack se desactiva.

### Aislamiento: el negocio del prompt sale de la sesión, no del `tenantId` del AgentSpec (2026-10-10)

- `PUT /api/v1/admin/settings/advanced/spec` y el editor crudo del panel SSR guardaban el `tenantId`
  que trajera el JSON. Como el prompt resolvía `TENANTS_ROOT/<slug>/` desde ese campo, un negocio podía
  cargar los playbooks y el `flow.json` de OTRO negocio (hallazgo del auditor multi-tenant durante
  HU-AOS-12).
- `save_spec_version` y `save_spec_if_changed` fuerzan `tenantId` al negocio que guarda
  (`con_tenant_de_la_fila`); un JSON que solo cambia el `tenantId` no crea versión. El endpoint devuelve el
  spec con el `tenantId` de la sesión.
- `build_system_blocks`, `load_playbook` y `resolve_flow` sacan el slug de `current_tenant_slug()` (el
  `tenant_scope`), nunca de `spec.tenantId`. Fuera de un scope no hay override por negocio. Las filas ya
  guardadas con un `tenantId` ajeno dejan de cargar artefactos de otro negocio (y el lector tolerante v3
  sigue avisando de la diferencia).
- `evals/runner.py` arma el flujo y el prompt dentro del `tenant_scope` del negocio evaluado, así que sus
  overrides se siguen cargando igual que antes.

### Se retira `generar_link_pago`: el modelo ya no elige referencia ni monto de un cobro (2026-10-10)

- Hallazgo de `auditor-multitenant` (severidad media, no cruza tenants): la tool recibía `reference` y
  `amount` del LLM y buscaba las transacciones solo por negocio. Un cliente que conociera el número de
  pedido de otro (`OC-0002`) sabía si estaba pagado y por cuánto, y podía crear un link con esa referencia
  y un monto inventado, que después bloqueaba el link del dueño del pedido.
- pack-payments ya no le ofrece tools al modelo. Sigue aportando la capacidad de pagos, el webhook y la
  pestaña de configuración. `pack_payments/skills.py` pasa a `pack_payments/proveedor.py`.
- El flujo de commerce ("Pedido y cierre") ofrece `confirmar_pago_pedido`, que toma el pedido del cliente
  de la conversación y el monto del servidor (ADR-0087). El playbook `pagos` ya no nombra tools ni le pide
  el monto al modelo.
- Test de integración `aislamiento`: por número de pedido, un cliente no ve el estado ni el monto del
  pedido de otro (`consultar_estado_pedido` responde igual que con un número inexistente).
- Cambia la lista de tools del modelo: hay que regrabar el gate de evals.
- Adenda en ADR-0087.

### Retención del stream de entrada y su DLQ en Redis (2026-10-10)

- El consumer borra (`XDEL`) cada mensaje de `agent_core:inbound` tras ackearlo, también al mandarlo a
  la DLQ. Antes el stream crecía sin límite y guardaba para siempre el texto y el teléfono de los
  clientes, incluidos los de negocios ya purgados (ADR-0053). Un `XDEL` fallido no cuenta como fallo
  del turno.
- Recorte periódico (cada 10 min) con `XTRIM MINID`: del stream, lo ackeado más viejo que
  `INBOUND_STREAM_RETENTION_HOURS` (24 h por defecto), con un corte que nunca pasa del pendiente más
  viejo del PEL ni del último entregado de cada grupo, así que lo pendiente o no leído no se pierde;
  de la DLQ, lo más viejo que `INBOUND_DLQ_RETENTION_HOURS` (168 h por defecto).
- La limpieza Redis de la purga de un negocio (`tenant purge` y `tenant purge-redis`) borra también
  sus entradas de la DLQ (por el campo `tenant_id`, sin tocar las de otros negocios).
- Documentado en `docs/MULTITENANT.md` (estado Redis en la purga) y las variables nuevas en
  `docs/despliegue-coolify.md`.

### Releases por conexión con rollback en un clic (2026-10-11)

- Agent OS (HU-AOS-14, ADR-0117): módulo `core/releases/` con build de una release inmutable por conexión
  (versiones exactas de agentes y habilidades, overrides de playbooks, secciones del negocio y campos de
  `AppSettings` del prompt, con `manifest_hash`), promoción total, rollback en un clic y caché en proceso.
- Release 1 por conexión conversacional desde el AgentSpec v2 activo: `agent-core db backfill-releases`.
- Sticky por episodio y revalidación de tareas al cambiar de release (`revalidated`, `cancelled`/`escalated`
  con `release_changed`), solo con `tenants.aos_runtime = 'v3'`. Sin cambio de conducta con `legacy`.
- API `/api/v1/admin/connections/{id}/releases`, `/releases/{rid}`, `/releases/{rid}/promote` y `/rollback`.
- Sync de `pack_versions` en el arranque del `api` (`agent-core db sync-packs`, función SECURITY DEFINER de la
  migración 0079) y lock de versiones de los packs en CI (`packages/pack-versions.lock.json`).

### El asistente ve el `quote_id` real en el turno siguiente (2026-10-10)

- **Incidente en test** (tenant solvershub):
  - El modelo no tenía el id de la cotización del turno anterior, porque el historial es solo texto.
  - Llamó `crear_pedido(quote_id="PLAN-PREMIUM")` y `confirmar_cotizacion` con `OC-0001` y con `Q-0001`.
  - Generó 6 cotizaciones en 9 minutos y nunca cerró el pedido.
- **Referencias vigentes** (ADR-0130):
  - Un pack aporta al bloque dinámico del prompt lo que el cliente está negociando, leído de sus tablas.
  - Pack-commerce aporta la cotización en curso (`quote_id`, estado, total) y el último pedido de la conversación.
  - El core sanea los datos y arma la línea: solo ids, números y textos cortos.
  - Si una fuente falla o tarda más de 0,5 s, el turno sigue sin ella.
- `aplicar_promocion`, `confirmar_cotizacion` y `crear_pedido`: si el `quote_id` no existe y la conversación tiene una cotización en curso, el error devuelve `quote_id_vigente` con su total. No la usan en silencio, y ya no mandan a generar otra.
- **Playbook `ventas.md`:**
  - el `quote_id` sale de `generar_cotizacion` o de las referencias, nunca de un SKU ni de un número de pedido;
  - no se regenera una cotización vigente;
  - el total de las referencias se puede citar.
- **Verificador:** "ya quedó confirmada la cotización" sin `confirmar_cotizacion` ya no pasa (antes daba `cierres: 0`).
  - Las afirmaciones `cotizacion_aceptada` y `cotizacion_generada` detectan el participio antes del sustantivo cuando lleva auxiliar ("quedó", "está", "fue").
  - Con condicional ("una vez confirmada la cotización…") no hay falso positivo.
  - El set etiquetado suma 5 casos: 208 correctas y 16 falsas, 0 falsos positivos y 0 falsos negativos.
- **Journeys:**
  - Test de integración con modelo guionado: compra del Premium en 4 turnos con el loop real y las tools de pack-commerce, tomando el `quote_id` del prompt. Cierra con una sola cotización.
  - Journey `suscripciones/12-comprar-premium-en-varios-turnos` para el LLM real.

### Purga de un negocio: Redis se limpia después del commit (2026-10-10)

- `purge_tenant` limpiaba el estado Redis del negocio ANTES de la función SQL: si la purga
  fallaba y hacía rollback, el negocio seguía existiendo sin su memoria de trabajo, debounce ni
  marcadores. Ahora Redis se limpia solo después del commit y de verificar que la fila `tenants`
  ya no existe; una purga SQL fallida deja Redis intacto.
- Si la limpieza de Redis falla tras el commit, la purga no se revierte: se registra
  `tenant_purge_redis_cleanup_failed` (error, con `tenant_id` y comando de reintento, sin slug),
  se manda una alerta `critical` y `purge_tenant` devuelve `False` (antes `None`). La CLI sale
  con código 2 y el panel SSR muestra un aviso; la API sigue respondiendo 204.
- Reintento idempotente: `agent-core tenant purge-redis <tenant_id>` (`purge_tenant_redis`).
  Solo acepta el UUID y se niega si el negocio todavía existe o si el UUID aparece en Redis como
  id de un hijo de otro negocio.
- La limpieza estaba desfasada de los formatos de clave reales: borraba `wm:{conv}` (hoy la
  memoria es `wm:{tenant}:{conv}`) e `inbound:meta:{id}` (hoy `inbound:{tenant}:...`), y no tocaba
  `debounce:messages`, `alta:terminos`, `waweb:*` ni el resto. Ahora borra las claves por
  conversación y hace `SCAN MATCH *<tenant_id>*` + `UNLINK` de lo que lleva el UUID en posición
  de tenant (primer UUID del nombre). La convención de claves queda en `docs/MULTITENANT.md`.

### Una respuesta larga ya no escala ni pausa el bot (2026-10-10)

- `persona.maxLength` pasa a ser un tope blando (adenda de ADR-0050). Antes, si el modelo se pasaba del largo
  dos veces seguidas, el turno escalaba y pausaba el bot: en los journeys de SolversHub (HU-SH-08) la consulta
  "¿qué planes tienen y cuánto cuestan?" escaló 1 de 4 veces. Ahora el largo se mira al final, cuando el
  verificador y los guardrails ya pasaron, con un reintento propio que no gasta `guardrail_max_retries` ni
  dispara `guardrail_fail_action`. Si el texto acortado falla una verificación, se entrega el borrador largo
  ya verificado. Si no queda vuelta del bucle ni presupuesto, se entrega sin reintento.
- Cada respuesta entregada por encima del tope deja `persona_max_length_excedido` en `audit_log` (largo,
  tope y qué se entregó, sin texto).
- `forbiddenPhrases` y la regla declarativa `max_length` (ADR-0051) no cambian.
- El envío ya no arma burbujas de más de 4096 caracteres con `pocos_cortos` o `varios_cortos`: la cola que
  junta el tope de burbujas y los párrafos enormes también se parten por largo.

### Un pedido pagado de otra compra ya no cubre la compra actual (2026-10-10)

- **Incidente en test** (tenant solvershub, 2026-10-10, 19:34–19:49 UTC):
  - La clienta había pagado el Basic (pedido `OC-0001`) y, en la misma conversación, quiso comprar el Premium. Había cotización y ningún pedido nuevo.
  - El bloque de referencias mostraba `OC-0001` como el pedido de la conversación.
  - El modelo le dijo que "el pedido OC-0001 ya figura como pagado, así que no hace falta generar otro". El Premium no estaba pagado.
- **Referencias vigentes** (adenda de ADR-0130):
  - Entre la cotización vigente y el último pedido, el más nuevo es la compra en curso.
  - La cotización sale con `pedido=ninguno`. Un pedido de otra compra, o uno cancelado, entregado o expirado, sale como `pedido_anterior`: sin total y con su `order_id`, para poder cobrarlo si está pendiente.
  - Una cotización más vieja que el último pedido ya no se muestra.
- **Playbook `ventas.md`:** `pedido_anterior` es otra compra y su pago no cubre la cotización en curso. Con `pedido=ninguno` toca seguir con `confirmar_cotizacion` y `crear_pedido`. Nunca decir "ya está pagado" ni "no hace falta pagar" por el pago de otro pedido.
- **Verificador:** la afirmación nueva `pago_registrado` cubre "figura como pagado", "el pago quedó registrado", "ya pagaste" y "recibimos tu pago". Pasa solo si en el turno se llamó `consultar_estado_pedido` o `sub_consultar`. Las negaciones y los condicionales ("si ya pagaste…", "apenas recibamos tu pago…") no cuentan como afirmación.
  - El set etiquetado suma 11 casos (2 del incidente): 218 correctas y 21 falsas.
  - 1 falso positivo (0,5 %), aceptado a propósito: "tu pago quedó registrado" sin consultar el estado en el turno.
  - 0 falsos negativos.
- **Journey** (test de integración con el loop real y un modelo guionado): la clienta compra y paga el Basic y después compra el Premium en la misma conversación.
  - Termina con dos pedidos, el del Premium pendiente.
  - Con el bloque como estaba antes, la frase del incidente la frena el verificador.
- Test nuevo: ningún playbook puede pasar el tope de 8 KB. Pasado el tope, `load_playbook` lo descarta sin avisar, y `ventas.md` ya pesa 7,5 KB.

### Motor de procedimientos del Agent OS (2026-10-10)

- HU-AOS-05: motor de procedimientos en el kernel (`core/kernel/procedures/`), activo solo con
  `tenants.aos_procedures`. Con una tarea en foco, solo se ofrecen y ejecutan las acciones de su
  etapa (más las del core). Una acción fuera de la etapa se deniega con un error de negocio.
- Los datos se validan en el servidor (tipo, opciones, formato y el *resolver* de la habilidad),
  todo o nada.
- Lo que compromete espera la confirmación explícita del cliente, atada a los argumentos (o, en la
  salida de una etapa, a los valores de la tarea). El botón firmado `confirmar:<tarea>:<seq>:<firma>`
  confirma sin LLM. "dale" y "sí" a secas no confirman, y un sí escrito solo vale para la petición
  del turno anterior.
- La tarea se cierra por un evento de dominio del outbox que correlaciona por la clave de identidad
  del topic (`CLAVES_DE_CORRELACION`, p. ej. `reference` en `payment.confirmed`) con un dato fijado
  por una acción. Corre en un savepoint del despachador.
- Datos sin LLM: un botón o una fila de lista (con candidatos) y la media del mensaje llenan el
  dato de la tarea en foco desde el consumer. El parser de WhatsApp pasa el id del botón
  (`interactive_id`) al stream.
- Modo sombra con `aos_runtime = 'v3'` y `aos_procedures` apagada: el motor solo observa y
  registra las diferencias en `audit_log`, sin cambiar el prompt, las acciones ni la respuesta.
- API de plugins: `MCPHost.register_procedures` y `register_resolver`. El linter reserva el
  prefijo `confirmar:` y exige que el dueño de un evento de cierre sea el core, la habilidad o una
  de `requires`. Los fixtures de `procedure/1` cierran con `core.payment.confirmed`.
- La tarjeta de estado la arma el motor con las definiciones de los procedimientos (se retira
  `tarjeta_del_turno`).
- Adendas de ADR-0112 y ADR-0113.

### Modelo por riesgo y confirmación con botón (2026-10-10)

- HU-AOS-09, todo detrás de `tenants.aos_procedures` (con la bandera apagada no cambia nada).
- Una etapa `risk: high` usa el modelo con thinking (`KernelSpec.modelPolicy.highRiskStage`),
  decidido una vez por turno. El razonamiento de DeepSeek vuelve en las vueltas siguientes. Si la
  llamada falla, el turno sigue con el modelo de siempre. Queda en `usage_ledger.result.politica_modelo`
  (ADR-0132).
- El reintento tras un rechazo del verificador usa el respaldo del agente si es de la misma familia
  de modelo; si no, se anota `respaldo_no_disponible`.
- Cuando el turno pide una confirmación, el mensaje lo arma el kernel con los argumentos que va a
  ejecutar (no con el texto del modelo). En WhatsApp, con la ventana abierta y si la etapa lo pide,
  va con el botón "Confirmar" firmado; en los demás canales, como texto.
- Migración `0082`: `messages.interactive` (payload del mensaje interactivo en la cola de salida).
- Clasificador de confirmación escrita, determinista y conservador, medido con un set etiquetado
  (precisión 1.0). El "sí" a secas sigue sin confirmar.

### Memoria por scope con procedencia y olvido (2026-10-10)

HU-AOS-36 (agent-core #398, ADR-0114 y su adenda, ADR-0053). Sin cambio de conducta con las banderas apagadas: el
perfil del cliente queda idéntico y el recall también.

- **Hechos con procedencia.** `memory_facts` es la fuente del perfil del cliente y `customers.attributes` su
  proyección (solo el namespace `perfil`), en la misma transacción. El único escritor es `core/memory/facts.py`;
  `entity.upsert_attributes` y el intake de leads de pack-commerce pasan por ahí. Cada hecho guarda fuente, turno,
  tarea, instancia, confianza y `data_class` (`pii`/`sensitive` por namespace y clave). Un valor nuevo cierra al anterior
  (`superseded_at`); el mismo valor no crea fila.
- **Lo que escribe el LLM** con `update_customer_profile`: claves de una línea, valores de hasta 4 KB, nada de pago,
  salud ni contraseñas, y no pisa claves del sistema (`crm_lead_id`, `optin`, `source`). Si no, error genérico.
- **Permisos.** `ToolContext.write_namespaces` (None en el runtime legacy): un agente v3 sin el namespace concedido no
  escribe. El lector tolerante concede `perfil` al General.
- **Consolidación nocturna** (`core.memory_consolidation`, 03:30 UTC): los datos confirmados de las tareas
  `completed` y de las cerradas por `episode_expired` pasan a hechos con su tarea y su turno. Nunca los de pago ni lo
  que el LLM extrajo sin confirmar.
- **Olvido.** `purge_memory(customer_id=…)` (CLI `tenant memoria-purgar --customer-id` o `--email`) borra (lógico) los hechos del
  cliente, su perfil proyectado y sus recuerdos, incluidos los intercambios del loop, que antes no tenían cliente y la
  purga no alcanzaba. También limpia la memoria de trabajo de Redis de todas sus conversaciones. Sus tareas y mensajes
  quedan. Lo olvidado no vuelve por la consolidación (`customers.memory_purged_at`).
- **Retención.** `MEMORY_RETENTION_DAYS` cubre también `memory_facts` y su proyección.
- **Recall.** Ignora lo purgado y los intercambios (como antes), y sin cliente no devuelve nada.
- **Migración `0080`** (sobre `0076`; se re-encadena al mergear). Agrega:
  - en `memory_vectors`: `scope`, `connection_id`, `agent_id`, `kind`, `importance`, `expires_at`, `embedding_model`
    y `source_turn_id`, con los CHECK `NOT VALID`;
  - `memory_facts.source_task_id`;
  - `customers.memory_purged_at`;
  - los índices concurrentes `ix_memory_vectors_scope` e `ix_conversation_tasks_closed`.

  Además, backfills en lotes: el cliente y la conexión de los intercambios viejos, y los hechos `perfil` desde
  `customers.attributes` (clientes vivos, con la hora de la migración). Los recuerdos viejos sin cliente posible quedan
  borrados (lógico). Exige el rol dueño (`BYPASSRLS`).
- **Después del deploy:** `agent-core tenant memoria-importar-perfil` (copia lo que el código anterior escribió en
  `attributes` mientras tanto; idempotente) y `VACUUM (ANALYZE) memory_vectors`.

### Pagos: el link vencido se regenera y cada rechazo dice por qué (2026-10-10)

- `confirmar_pago_pedido` volvía a mandar el link de pago de un pedido aunque ya hubiera vencido:
  la lógica de vencimiento se fue con `generar_link_pago` (#423). Ahora la capacidad de pagos
  (`pack-payments`) la tiene. Un link que vence en menos de 15 min se reemplaza por uno nuevo y la
  transacción vieja queda borrada (auditoría y webhook tardío). Nunca hay dos vivas por referencia,
  tampoco con dos pedidos de link a la vez. Una referencia con un link vencido por otro monto (el
  saldo de una cuota de cobranza después de un abono) ya no queda bloqueada.
- `confirmar_pago_pedido` contestaba "el total es menor al mínimo de la pasarela" a cualquier
  rechazo. Ahora distingue: pedido ya pagado en la pasarela (no ofrece otro medio de pago), pago en
  revisión o cobro previo por otro monto (escala a una persona), pasarela sin configurar o que no
  respondió, y el monto bajo el mínimo de verdad. Los códigos viven en `core/payments`, así que
  commerce sigue sin importar el pack de pagos.
- Un rechazo de Wompi que no es el mínimo (p. ej. credenciales vencidas) ahora se loguea como error.
- Adenda en ADR-0067 y nota en ADR-0087 §4. Cobranza y suscripciones quedan como seguimiento: siguen
  tratando todo rechazo como monto bajo el mínimo, y suscripciones puede reusar un link guardado ya
  vencido.

### `generar_link_pago` ya no reusa a ciegas la transacción de una referencia (2026-10-10)

- Visto en test (tenant `solvershub`, 2026-10-10): el modelo llamó `generar_link_pago` con $600.000 y
  `OC-0001`, un pedido del 2026-10-05 ya pagado por $200.000. La idempotencia por referencia devolvía la
  transacción existente tal cual (`aprobado`, $200.000 y el link vencido en `_safe_append`), y el asistente
  le dijo a la clienta que ya tenía su link de pago.
- Ahora, si la referencia ya tiene una transacción:
  - **pagada** (también si se pagó un link anterior ya reemplazado): error recuperable `ya_pagado`, sin link;
  - **con un pago en revisión** (`monto_distinto`): error `en_revision`, sin link;
  - **por otro monto**: error `monto_no_coincide` que dice el monto real, sin link;
  - **pendiente o rechazada con el link vencido** (o a menos de 15 minutos de vencer, calculado con
    `created_at` + `link_expira_horas`): se genera un link nuevo con la misma referencia. La transacción vieja
    queda con `deleted_at` (y `expirado` si estaba pendiente), así un webhook tardío del link viejo todavía
    se aplica por `provider_reference`.
- Migración `pack_payments_0003`: el índice único `(tenant_id, reference)` pasa a ser parcial
  (`where deleted_at is null`, invariante 8). Sigue habiendo una sola transacción viva por referencia; el
  borrado de la vieja exige que su estado no haya cambiado, así dos regeneraciones simultáneas o un webhook
  en el medio no dejan dos vivas ni pisan un `aprobado`.
- La capacidad de pagos (`WompiPaymentCapability`) busca la transacción viva de la referencia.
- La descripción de la tool le dice al modelo que un error significa que no hay link.

### Kernel del Coordinador: decisión y resultado de cada turno (2026-10-10)

- Cada turno deja una fila en `core_decisions` (al decidir) y otra en `turn_outcomes` (al cerrar) con el mismo
  `turn_id`, y su `trace_id`. Bloqueos por negocio pausado, cuota, presupuesto, conexión pausada (ADR-0122), horario o
  conversación escalada quedan `blocked` con su motivo, sin llamar al LLM (HU-AOS-10, adenda de ADR-0112).
- `messages`, `usage_ledger` y `audit_log` llevan el `turn_id` del turno que las causó (`usage_ledger` también el
  `trace_id`); nunca el de otro negocio.
- La traza del turno viaja a las tareas Celery en `traceparent`: la salida comparte `trace_id` con el turno.
- Nuevos `core/kernel/decisions.py`, `ingress.py` (cuota y presupuesto) y `egress.py` (un mensaje por turno). Sin cambio
  de conducta; se apaga con `KERNEL_LEDGER_ENABLED=false`.

### Políticas de entrada en el kernel del Coordinador (2026-10-10)

- Las políticas del mensaje que entra pasan del consumer a `core/kernel/ingress.py` (`politicas_de_entrada`): conexión
  pausada (ADR-0122), kill-switch o conversación escalada, palabras de escalamiento y horario de atención. El debounce
  usa la misma guarda de conexión pausada al disparar (`conexion_pausada`). HU-AOS-10, parte 2.
- Sin cambio de conducta: mismo orden, mismas respuestas y mismas decisiones en `core_decisions`. La velocidad de
  respuesta (inmediato o debounce) sigue en el consumer.

### Políticas de entrada: el escalamiento no se pierde si algo falla (2026-10-10)

- Palabra de escalamiento: la conversación se escala antes de cargar el texto de respuesta. Si el
  `AgentSpec` no carga (p. ej. BD caída), se escala igual y se responde el texto fijo de escalamiento.
  Antes quedaba la decisión `escalated` en `core_decisions` sin escalamiento real, y el reintento del
  consumer no lo rehacía (el inbound ya guardado se ve como duplicado).
- Si escalar falla, se reintenta una vez tras 0,5 s. Si vuelve a fallar, el turno queda `failed` en
  `turn_outcomes`, se registra `escalation_keyword_failed`, se avisa a los dueños por push y no se le
  responde nada al cliente (no se promete una persona que no viene).
- Si el aviso al cliente no sale después de escalar, el turno sigue `escalated` (antes quedaba `failed`).
- Fuera de horario: si el envío del mensaje configurado lanza una excepción, se escala como cuando
  devuelve `False` (antes la excepción cortaba el turno sin escalar).
- Los logs nuevos registran solo el tipo de error: el texto de un error de SQLAlchemy trae el mensaje
  del cliente entre sus parámetros.

### Agent OS: Coordinador y General por conexión (backfill B2) (2026-10-10)

- HU-AOS-13 (parte 1, #374). **Backfill de agentes** `agent-core db backfill-agentes [--dry-run]
  [--tenant] [--lote]` (paso B2 de SPEC-26 §4.4). Por negocio: un agente `coordinador` (core,
  KernelSpec derivado de la `persona` v2) y un `general` (specialist, el AgentSpec v2 activo
  convertido por el lector tolerante, con sus procedimientos abiertos), sus versiones y una
  instancia de cada uno en cada conexión conversacional (el General, por defecto). Las
  integraciones no reciben agentes.
  - Idempotente por `legacy_agent_spec_id`: re-correrlo no crea nada y cubre las conexiones
    nuevas; si el v2 activo cambió, publica la versión siguiente.
  - Exige el backfill de conexiones de HU-AOS-11 hecho antes. Si falta, el negocio queda
    bloqueado con el comando a correr y el CLI sale con código 1.
  - Los secretos literales de las `declarativeTools` v2 se copian a la instalación `core` antes
    de la primera versión. Ninguna versión ni borrador lleva literales. Un conflicto con un
    secreto existente es un error del negocio, sin escrituras. El backfill nunca pisa un secreto
    (lo comprueba con la fila bloqueada) ni le suma dominios por su cuenta: si la tool sumó
    dominios, pide reescribirlo en el editor.
  - No se corrió en ningún entorno. Sin cambio de conducta: el runtime sigue en
    `aos_runtime = legacy` y `agent_specs` no cambia, salvo la siembra del spec por defecto que
    haría el loop.
- **API de instancias** (lectura): `GET /api/v1/admin/connections/{id}/agents`, con el
  Coordinador primero y la última versión publicada de cada agente. Las escrituras quedan para la
  parte 2.
- `core/agents/instancias.py::asegurar_instancias_base`: Coordinador + General en una conexión,
  idempotente. Nunca pisa otro Coordinador ni otro agente por defecto. La usarán el alta
  posterior de una conexión, la promoción de ADR-0127 §3 y las releases (HU-AOS-14).
- Lector tolerante: el header sensible de una tool sin `allowed_domains` se descarta con aviso.
  Esa tool hoy no llama a nadie. Adenda de HU-AOS-13 en ADR-0113.
- **Credenciales fuera de la lista también por referencia** (decisión del dueño 2026-10-11, adenda en
  ADR-0113, ajustada tras la revisión de seguridad). `core/agents/credenciales.py` detecta como
  sensibles los nombres de headers, query, body y del schema de `parameters` con patrón de
  credencial, también en español (`clave`, `contraseña`, `llave`, `firma`…). El userinfo y el fragmento
  de la URL también cuentan.
  - El linter v3 rechaza literales (`credencial_literal`), mezclas de texto fijo y argumentos
    (`credencial_mixta`), `{secret:..}` fuera del valor completo de la query
    (`referencia_mal_ubicada`) y `extensions.tools` en v3.
  - El lector tolerante y el backfill mueven los literales a la instalación `core`. Una mezcla o un
    `default` sensible en `parameters` deja al negocio en error con la ruta.
  - El loop legacy y el editor v2 no cambian. Las referencias de URL y body las resolverá el
    runtime v3.

### Agent OS: escrituras de instancias por conexión y altas con agentes (2026-10-11)

- HU-AOS-13 (parte 2, #374). **API de instancias**: `POST /api/v1/admin/connections/{id}/agents`
  (agrega un especialista con versión publicada), `PATCH .../agents/{instance_id}` (habilitar o
  deshabilitar, prioridad, volverlo el agente por defecto) y `DELETE .../agents/{instance_id}`
  (borrado lógico). Sección `settings` con escritura; con `aos_runtime = legacy`, solo el master.
  - El Coordinador lo gestiona el sistema: no se agrega, no se deshabilita ni se quita por la
    API (409 `core_required` / `not_specialist`). Siempre hay un agente por defecto y está
    habilitado (409 `default_disabled` / `default_required`). 422 para una integración.
- **Altas posteriores:** un número dado de alta o reconectado (los cinco caminos, por
  `numero_para_conexion`) nace con Coordinador y General si el negocio ya tiene agentes, en la
  misma transacción. Un error ahí se registra y el alta sigue.
- **Promoción a producción:** la conexión del número real clona las instancias de la de prueba
  (`clonar_instancias`, que HU-AOS-14 reutiliza para clonar la release).
- El backfill y las altas ya no recrean el General en una conexión que tiene Coordinador: lo que
  el dueño quitó no vuelve.
- Sin cambio de conducta: con `aos_runtime = legacy` nada en runtime lee las instancias.

### Agent OS: contratos AgentSpec v3 y KernelSpec v1 (2026-10-10)

- HU-AOS-12 (parte 1, #373). `agentcore-contracts` pasa a **3.0.0** (ADR-0113 y su adenda del
  2026-10-10). Trae `agentspec/3` (`AgentSpecV3`: un agente especialista o de back-office, sin
  identidad ni `kind`) y `kernelspec/1` (`KernelSpec`: voz de la conexión, enrutamiento sin
  regex, límites A2A y presupuestos). `AgentSpec` sigue siendo el v2 y el loop legacy no cambia.
  Se retira el espejo TS de `packages/contracts/src/`, que nadie consumía.
- Tools declarativas sin secretos en el contrato: `Authorization`, `Cookie` y `X-Api-Key` solo
  admiten `{"secret": "<nombre>"}`; un valor literal no valida.
- Validador y linter únicos (`core/agents/contrato_v3.py`): acciones inexistentes,
  declarativas sombreadas por una acción del binario, dominios no públicos, duplicados y
  procedimientos expuestos sin declarar. CLI `agent-core agent lint` y paso nuevo de CI sobre
  `tests/fixtures/agents/`.
- Lector tolerante v2 → v3 (`core/agents/tolerante.py`). Aplica la tabla campo por campo,
  arma los procedimientos abiertos del flujo y consiente en `tools` las acciones que hoy
  ofrece el loop. Saca los secretos literales como `SecretStr` y deriva el KernelSpec de la
  `persona` v2. `cargar_como_v3` lista las conexiones conversacionales. Nadie lo llama en
  runtime: es la base del backfill (HU-AOS-13) y de las releases (HU-AOS-14).
- Las versiones viejas de `agent_specs` conservan los literales en claro: fuera de alcance.

### Agent OS: agentes versionados por API y tools declarativas sin secretos (2026-10-10)

- HU-AOS-12 (parte 2, #373). **API de agentes** en `/api/v1/admin/agents` (ADR-0113, ADR-0077):
  - crear un agente (`kind` core → `kernelspec/1`; specialist o backoffice → `agentspec/3`)
    con su borrador;
  - guardar el borrador con `lock_version`: 409 si otra persona lo cambió, 422 con los errores
    del contrato por campo (el mismo validador del CLI y de CI);
  - publicar una versión inmutable con `content_hash` (sha256 del JSON canónico), idempotente
    por hash. Publicar no activa nada: las releases llegan con HU-AOS-14.

  Con el runtime `legacy` solo escribe el master (o un negocio con `aos_runtime = 'v3'`). Las
  keys `general` y `coordinador` quedan reservadas para el backfill de HU-AOS-13.
- **Editor de tools declarativas** en `/api/v1/admin/settings/declarative-tools` (antes solo
  SSR). Los headers sensibles van por referencia `{"secret": ...}` y los valores son de solo
  escritura. El GET enmascara los literales viejos; guardarlos con la máscara los migra. El
  SSR pasa por el mismo validador y ya no muestra secretos.
- **Secretos en la instalación `core`** del negocio (`pack_installations.config_encrypted`,
  ADR-0118 §5). Van cifrados con AAD `(tenant_id, installation_id)` y cada secreto está atado
  a los dominios de las tools que lo usan. `core/mcp/declarative.py` los resuelve al ejecutar,
  solo hacia esos dominios; los literales v2 siguen funcionando igual. `migrar_secretos` es el
  paso del backfill antes de la primera `agent_version`.
- Migración `0076`: fila `core` en el catálogo `plugins` (FK de `pack_installations`).
- Los editores crudos del AgentSpec (`/advanced`, `/advanced/spec` y el SSR `raw_json`) ya no
  devuelven literales sensibles de las tools declarativas, y sus cambios a `extensions.tools`
  pasan por el mismo validador del editor de declarativas. Al ejecutar, un secreto nunca viaja
  por `http://`.

### Estado de la conversación: tareas event-sourced y tarjeta de estado (2026-10-10)

- HU-AOS-04 (ADR-0114). `core/kernel/state.py`: tareas como `task_events` (fuente de verdad) +
  proyección `conversation_tasks`, escritas en la misma transacción con un único reductor puro;
  la reconstrucción desde los eventos da lo mismo que lo guardado, también a través de un
  `revalidated` entre versiones del procedimiento (`data.schema_version` en cada evento).
- `core/kernel/episodio.py`: inicio de episodio por la hora de los mensajes (720 min sin
  mensajes, hasta que llegue el KernelSpec); al empezar uno nuevo, las tareas sin actividad en
  todo el anterior quedan `cancelled` con `episode_expired`. Fija `conversations.episode_started_at`.
- `core/kernel/tarjeta.py` + playbook del kernel `core/playbooks/kernel/estado-conversacion.md`:
  tarjeta de estado en el bloque dinámico, justo antes de la fecha y hora (ADR-0091), con los
  valores como cadenas JSON escapadas. Solo con `tenants.aos_procedures` encendida; apagada, el
  prompt no cambia.
- La confirmación de un procedimiento vive en la tarea (`pending_confirmation`), no en
  `conversations.state`. Migración `0074`: `conversation_tasks.pending_confirmation` y `status_reason`.
- API: `GET /api/v1/admin/conversations/{id}/tasks` y `…/tasks/{task_id}/events` (sección
  `conversations`, 404 entre negocios).

### Alta de planes: el `hint` de `terminos_pendientes` distingue si los enlaces ya salieron (2026-10-10)

- `TerminosPendientes` trae `enlaces_enviados`: si el cliente ya recibió los enlaces de este juego de
  versiones (el primer mensaje saliente, no fallido, posterior al marcador). Lo calcula
  `_hora_de_envio`, que ahora también usa `_mensaje_que_acepta`; la regla de aceptación no cambia.
- `crear_pedido` (pack-commerce) elige el `hint` según eso. Si los enlaces todavía no salieron, pide
  compartirlos y no repetir la llamada en el turno: un «sí» anterior no los acepta. Si ya salieron, el
  `hint` es el de antes («si el último mensaje acepta, repite ahora con `acepta_terminos`»).
- Antes el `hint` era siempre el segundo. Cuando el cliente decía «acepto» antes de ver los enlaces,
  el modelo repetía la llamada en el mismo turno y volvía `terminos_pendientes` (canary de los journeys
  de SolversHub, run 38065232533, comprar-basic, intento 4/8).

### Evals: grabar y reproducir el LLM, y gate en replay en las PRs (2026-10-10)

- Modo grabar/reproducir del LLM para las evals (`core/llm/cassette.py`, adenda de ADR-0119). `evals/runner.py` y
  `evals/journeys.py` aceptan `--grabacion off|record|replay` (o `LLM_CASSETTE`) y `--grabaciones DIR` (o
  `LLM_CASSETTE_DIR`).
  - `record` llama al LLM real y graba un JSON por escenario o journey. Gasta saldo: solo con OK del dueño.
  - `replay` responde desde la grabación sin red ni `LLM_API_KEY`. Si algo cambió, falla con el diff (código de
    salida 3) y nunca cae al LLM real.
- La clave normaliza la fecha y hora del prompt (ADR-0091), los UUIDs, los timestamps y los ids generados. La
  respuesta grabada se rehidrata con los ids de la corrida actual.
- El modo solo se enciende en development o test. Una corrida en replay no se guarda en `eval_runs`.
- CI (adenda de ADR-0120): nuevo job `evals-replay` en las PRs a `develop`. Corre el gate de evals y journeys en replay
  sin `LLM_API_KEY`, con `scripts/gate_evals.sh`. El push a develop y el nightly siguen con el LLM real.
- Guía y flujo para regrabar en `evals/README.md`.
- `scripts/grabar_gate_local.sh`: graba el gate con el LLM real en un stack efímero idéntico al CI, lo reproduce dos
  veces sin clave y revisa que no queden secretos. Tiene ensayos sin LLM (`--ensayo`, `--ensayo-demo`).
- Seguridad: los tracebacks ya no imprimen secretos. `Settings` oculta en `repr` los campos secretos y las contraseñas
  de las URLs. Los runners de evals usan una consola sin variables locales (`configure_console_logging`), y la CLI
  apaga `pretty_exceptions_show_locals`. El contenedor de secretos del modo grabación no se puede imprimir.

### Develop no gasta LLM (2026-10-10)

- CI: el gate de evals y journeys del push a `develop` corre en replay (sin `LLM_API_KEY`); el LLM real queda
  solo en la PR de release hacia `main`.
- `evals-nightly.yml` deja de correr cada noche: se lanza a mano con `k` = 2 por defecto.
- Adenda de ADR-0120 y ajustes en ADR-0119 y `evals/README.md`.

### Agent OS: conexión canónica por número y ruteo por conexión detrás de bandera (2026-10-10)

Primera parte de HU-AOS-11 (ADR-0115): *expand* y *switch* detrás de `AOS_CONNECTION_ROUTING`,
sin *contract*.

- **Pausa y credenciales por la conexión.** Las guardas de pausa de ADR-0122 (consumer y debounce,
  `sender._registrar_si_pausada`, `outbound_send`, `event_notices` y el 409 de la API de
  conversaciones) resuelven la cuenta de Meta con `cuenta_meta_de_conversacion`: si la conversación
  tiene una conexión ligada a Meta, la de `channel_connections.meta_connection_id`; si no, el puente
  `conversations.meta_connection_id`, como antes. `resolve_credentials` de una conexión canónica
  envía con la cuenta de la conexión y **por el número de la conexión**, sin caer a otro número, a
  la primaria ni al `.env`. Las plantillas se buscan en la WABA de esa misma cuenta.
- **Altas posteriores.** `meta_asset_claims.numero_para_conexion` (los cinco caminos de ADR-0122 §6
  y, por el alta BYOK, la promoción de ADR-0127 §3) crea o religa, en la misma transacción, la fila
  de `channel_connections` del número: sin credenciales, `is_primary = false`, tipo `whatsapp_meta`.
  El rollback de un alta BYOK fallida la devuelve a la cuenta anterior. Una conexión manual del mismo
  número gana y no se duplica.
- **Las filas canónicas no cambian lo de antes.** Migración `0073`: `resolve_tenant_for_phone` y
  `meta_asset_claimed_by_other_tenant` ignoran las filas con cuenta (el dueño del número sigue siendo
  `phone_numbers`). La firma del webhook, el listado y la edición de conexiones manuales (API y SSR),
  el gating de tools (ADR-0072), la primaria y los primeros pasos solo miran conexiones sin cuenta.
- **Backfill** `agent-core db backfill-conexiones [--dry-run] [--tenant] [--lote]`: idempotente, por
  lotes y por negocio. Pone `type_id`/`category` a las conexiones viejas sin tipo, crea la conexión de
  cada número vivo de WhatsApp, liga las conversaciones de cuentas con un solo número y reporta los
  choques con conexiones manuales y las conversaciones abiertas que quedan sin conexión. **No se ha
  corrido en ningún entorno.**
- **Ruteo por (conexión, cliente)** detrás de `AOS_CONNECTION_ROUTING` (global, apagada por defecto):
  encendida, una conversación abierta por conexión, sin reescribir `connection_id`, tolerando
  duplicados (la más reciente) y adoptando la abierta del cliente que quedó sin conexión en la misma
  cuenta. Apagada, el camino de siempre, que ahora limpia `release_id`, `release_lane`,
  `active_agent_instance_id` y `owner_since` al cambiar de conexión.
- **Cambio visible con la bandera apagada:** desde este despliegue, un número dado de alta o
  reconectado tiene su conexión canónica, sus conversaciones quedan con `connection_id` y la respuesta
  sale por el número al que escribió el cliente (antes, por el primer número vivo de la cuenta). Solo
  difiere en cuentas con varios números.
- Pendiente (otra parte de la HU): *contract* (dedupe, índice único, `connection_id` NOT NULL),
  eliminar `mover_conversaciones`, chat web como conexión `webchat`, clonar instancias y release en la
  promoción (HU-AOS-13/14) y la verificación con Meta real en test.

### El asistente ya no niega una cita de hoy que ya pasó (2026-10-10)

- En test, a las 23:49 de Bogotá una clienta preguntó por su cita de las 10:00 de ese día, que seguía
  en `scheduled`. `list_client_appointments` solo miraba las citas futuras: devolvió `total: 0` tres
  veces y el asistente dijo que no aparecía ninguna cita.
- Ahora la tool también devuelve las citas de las últimas 24 h que ya pasaron, con `ya_paso: true` y
  sin índice; las futuras llevan `ya_paso: false` y conservan la numeración 1..N que usa
  `cancel_calendar_event` ("cancelá la 2"). La respuesta suma `proximas` (cuántas faltan) y `total`
  cuenta todas.
- La descripción de la tool le pide al asistente que diga que la cita ya pasó, con su fecha y hora,
  que ofrezca agendar otra y que nunca niegue una cita marcada `ya_paso`.

### Alta automática: un negocio purgado y vuelto a crear no hereda su Redis (2026-10-10)

- El alta automática (ADR-0121) da al negocio un id determinista (UUIDv5 de vendedor + referencia). Si ese
  negocio se purgaba, la limpieza de Redis fallaba y luego se reintentaba o aprobaba el alta, el negocio
  nuevo nacía con el mismo UUID y heredaba las claves sin TTL del anterior (memoria, pausas, marcadores de
  términos). Hallazgo de la revisión del PR #424.
- Ahora, justo antes de crear el negocio y solo si todavía no existe, el alta borra el estado Redis de ese
  id con `limpiar_redis_antes_del_alta` (`core/tenants.py`), la misma limpieza que `purge_tenant_redis`.
  Las claves de otros negocios no se tocan. Si Redis no responde o tarda más de 30 s, el negocio no se crea:
  el alta se reintenta y al tercer intento queda en revisión. El id sigue siendo determinista, así que la
  idempotencia del alta no cambia. No cubre la DLQ de entrada, la sesión de WhatsApp Web en disco ni
  `TENANTS_ROOT/<slug>/`, que la purga tampoco borra (quedan para otro PR).
- Adenda en ADR-0121; `docs/MULTITENANT.md` y el runbook `altas-en-revision.md` lo explican. Tests de
  integración: reintento después de una purga con Redis caído, alta sin Redis y Redis colgado.

### Los términos del alta se aceptan por chat de verdad (2026-10-09)

- **Marcador fijo por juego de versiones (HU-SH-07, adenda de ADR-0125).** Con los documentos legales
  publicados, ningún cliente podía contratar un plan por chat. En el turno en que el cliente acepta,
  el modelo llama `crear_pedido` sin el token (o con `true`) antes de llamarla con el token. Esa
  llamada movía el marcador de «enlaces mostrados» al mensaje que aceptaba, y el reintento fallaba.
  - Ahora la clave lleva la huella del juego de versiones y se escribe con `SET NX` (24 h). Una
    versión nueva pide aceptar otra vez.
  - La aceptación tiene que llegar después de la respuesta que llevó los enlaces (el primer mensaje
    saliente no fallido posterior al marcador). Un mensaje que llegó durante ese turno no acepta.
  - Es por pedido: al guardar el pedido se borra el marcador.
  - El `hint` de `terminos_pendientes` dice que, si el cliente ya aceptó, repita con el token en ese
    mismo turno (el token se reemite igual).
  - Sigue fallando cerrado.
- **Verificador (adenda de ADR-0119).** Las URLs de una tool del turno actual cuentan como evidencia
  de links aunque la tool termine con error. Antes se quitaban los tres enlaces legales y el cliente
  aceptaba sin verlos. Una tool fallida sigue sin respaldar cierres ni links de pago.
- **Playground.** Guarda en `messages` el mensaje de la prueba y la respuesta, así la prueba desde
  el panel de un vendedor puede aceptar los términos. Los de la conversación `test` no cuentan para
  la cuota de `mensajes` ni para `messages_7d` del tablero.
- **Journeys (`evals/journeys.py`, HU-SH-08).**
  - `negocio.json` acepta `documentos_legales`, que se publican con `legal.publicar`.
  - Variables `{cliente.documento}` y `{cliente.celular}`, únicas por intento.
  - Cada turno guarda el mensaje del cliente y la respuesta, como el consumidor y el envío.
- **Tests.**
  - `test_journeys_terminos_alta.py` pasa por el runner, el loop real y el playground con un modelo
    guionado.
  - `test_antiabuso_alta.py` prueba la secuencia de dos turnos, la ráfaga, la aceptación por pedido
    y una versión nueva.
  - `test_verificador.py` prueba los links de una tool fallida.

### Runbook de producción en el VPS y verificaciones de la migración (2026-10-09)

- HU-ARCH-01 (preparación; la HU se cierra al ejecutar en el VPS). `docs/despliegue-coolify.md` describe la
  producción en el VPS de dos nodos con Coolify (D-19): topología, recursos, borde con Cloudflare Tunnel y
  Access (solo `/webhooks/*` y `/healthz` son públicos de la API), registros DNS, variables por recurso
  (nombres y origen, sin valores), rotación de secretos, separación de test y observabilidad. La producción
  de la máquina de desarrollo queda como "heredada" hasta el corte.
- `docs/runbooks/produccion-vps.md`: lo que hace el dueño (VPS con dimensionamiento a validar, bucket S3,
  DNS, token del túnel), la instalación de Coolify, los recursos, la base migrada desde cero con el rol
  dueño `agent_core_migrations` (BYPASSRLS, sin superusuario) y el runtime `app_runtime` con clave propia,
  el ensayo, el corte (congelar los webhooks, drenar las colas, `pg_dump` y restauración de solo filas,
  conteos y vuelta atrás), la prueba de apagado y lo que queda para HU-ARCH-02 y HU-ARCH-03.
- `scripts/comparar_conteos.py`: compara el conteo de filas por tabla y las versiones de `alembic_version*`
  entre dos bases (`ORIGEN_DATABASE_URL` y `DESTINO_DATABASE_URL`, solo del entorno). Sale con 1 si algo
  difiere y con 2 si el rol no puede saltarse RLS (contaría 0 en las dos bases y "coincidiría").
- `scripts/verificar_aislamiento.py`: reutiliza el check `rls_forced` del checklist de producción y suma RLS
  sin FORCE fuera de `public`, que el rol del runtime no sea superusuario, no tenga BYPASSRLS ni sea dueño
  de nada, que sea `app_runtime` y que no use la clave de desarrollo. Hoy marca `meta_private.meta_tokens`
  (RLS sin FORCE, ya anotado en `docs/BACKLOG.md`).
- `scripts/vaciar_base_nueva.sql`: deja sin filas la base nueva antes de restaurar; solo corre en la base
  marcada con `COMMENT ON DATABASE … IS 'solvershub-produccion-vps'`, así que no puede vaciar la heredada.

### El aviso de renovación sale por el SMTP del vendedor (2026-10-09)

- El aviso de renovación por el panel de un negocio del alta automática sale por la integración
  SMTP de su vendedor (canal de correo saliente, ADR-0071). Si el vendedor no tiene una, o el
  negocio no tiene vendedor, sale por el SMTP de la plataforma. `renovacion_avisada` registra el
  `remitente`. Enmienda de ADR-0123.
- El acceso del alta y la recuperación de contraseña siguen por el SMTP de la plataforma.
- BACKLOG: elegir el SMTP por propósito cuando un negocio tiene varias integraciones de correo.

### `purge_tenant` funciona con un dueño sin superusuario (2026-10-09)

- Corrección: el borrado de un negocio (`tenant purge`, panel y API) fallaba siempre en producción.
  `purge_tenant` (migración `0011`) apagaba los triggers con `ALTER TABLE … DISABLE TRIGGER ALL`, que
  en los triggers internos de las FKs exige superusuario ("… is a system trigger"), y el dueño de
  producción es `agent_core_migrations NOSUPERUSER BYPASSRLS`. Hacía rollback completo: sin fuga, pero
  sin borrado. En CI, local y test no se veía porque ahí se migra como superusuario.
- Migración `0072`: `purge_tenant` se declara con `SET session_replication_role = replica` (se
  restaura al salir de la función, también si falla) y `SET search_path = pg_catalog, public,
  pg_temp`, con nombres calificados y descubrimiento de tablas por `pg_catalog`. La fila `tenants` se
  borra con las FKs encendidas: el `ON DELETE SET NULL` de `provisioning_requests.negocio_tenant_id`
  (la solicitud del vendedor) se aplica y una FK inesperada aborta la purga. Siguen igual: SECURITY
  DEFINER, transaccional, master-only y frase `ELIMINAR <slug>`.
- La purga ahora también borra `meta_private.meta_tokens` (tokens cifrados de Meta del negocio), que
  quedaban huérfanos porque solo se miraba el esquema `public`.
- `EXECUTE` de `purge_tenant` solo para `app_runtime` (antes, PUBLIC).
- **Solo se purga un negocio suspendido** (`tenants.status = 'suspended'`, el de `tenant
  deactivate`; no la falta de pago). Lo exigen `core/tenants.py` (`TenantNotSuspendedError`, con un
  mensaje que dice cómo suspenderlo), la CLI (antes de pedir la frase), el panel (el botón
  "Eliminar" solo se habilita en los suspendidos), la API (409 `tenant_not_suspended`) y la función
  SQL (defensa en profundidad: falla sin borrar nada, con `FOR UPDATE` sobre la fila del negocio).
- **Paso manual antes de desplegar** donde el rol de migraciones no sea superusuario (producción):
  como superusuario, `GRANT SET ON PARAMETER session_replication_role TO agent_core_migrations;`.
  Sin eso la `0072` aborta a propósito. Agregado al paso 4.3 de `docs/runbooks/produccion-vps.md`
  y a los gotchas de `docs/despliegue-coolify.md`. Test, CI y el stack local migran como
  superusuario: no necesitan nada.
- Test de integración (`-m aislamiento`): purga con un dueño de la función sin superusuario
  (BYPASSRLS y el permiso sobre el parámetro); comprueba `meta_private`, que otro negocio queda
  intacto, el `SET NULL` de la solicitud del vendedor, el rollback completo sin el permiso y que
  `session_replication_role` no se filtra a la transacción del llamador.
- Tests de integración nuevos: purgar un negocio activo falla en Python y en SQL sin borrar nada
  (ni Redis); la API responde 409 con el negocio activo y 204 una vez suspendido. Los tests de purga
  existentes (frase incorrecta, borrado completo, panel) suspenden antes.

### Los journeys vuelven a cobrar con la pasarela simulada (2026-10-09)

- Desde #324, `pack_payments` llama a `build_provider(config, http=…)`. El reemplazo de los journeys
  (`PagoSimulado._proveedor`) no aceptaba `http`: la tool fallaba, el core apagaba `pack_payments` para el
  negocio y todas las `sub_*` desaparecían del flujo. El primer nightly con k = 4 dio pass@1 43 % por eso.
  Ahora el simulado tiene la misma firma y un test compara las dos. Medición local del set: 9/11.

### Journeys de un negocio y canary del asistente (HU-SH-08, 2026-10-09)

- `evals/journeys.py --tenant <slug>`: corre `TENANTS_ROOT/<slug>/evals/journeys/` contra una réplica efímera del
  asistente. Copia sus artefactos a un `TENANTS_ROOT` temporal, crea el negocio con su preset y su plan, sin
  ejemplos, y siembra catálogo, packs y voz de marca. Los escenarios del cliente siguen fuera del framework.
- `"vendedor": true` en `negocio.json`: el alta automática (HU-SH-03) corre de verdad dentro del proceso del
  runner, sin tocar la allowlist real.
- Paso nuevo `{"outbox": "despachar"}`: entrega los eventos del outbox del negocio del journey (solo ese negocio)
  para que la compra llegue hasta el alta. Los correos de sistema se guardan en vez de enviarse
  (`{correos.ultimo.para}`).
- `core.onboarding.sembrar_artefactos` (catálogo, `<pack>.json` y voz de marca), extraída de `agent-core tenant
  seed`, que ahora la usa. `core.tasks.domain_events_dispatch.entregar_evento`, extraída del despachador sin
  cambiar su comportamiento.
- ADR-0016: adenda con el canary de un asistente cuyos artefactos viven en `TENANTS_ROOT`: gate en el repo del
  negocio con k = 8 y rollback por revert más reaplicar.

### Antiabuso y cumplimiento del alta automática (2026-10-09)

- HU-SH-07 (ADR-0125). Los controles se aplican cuando un negocio vendedor (`PROVISIONING_SELLER_TENANTS`)
  vende un plan de la plataforma. Fuera de los vendedores no cambia nada.
- **Términos aceptados.** `crear_pedido` de un plan no crea el pedido hasta que el cliente acepta términos,
  privacidad y DPA en su versión vigente:
  - responde `terminos_pendientes` con los tres enlaces y un token (`acepta_terminos`): HMAC de la
    conversación y del juego de versiones;
  - con el token en `customer_data.acepta_terminos` **y un mensaje del cliente posterior a los enlaces**,
    crea el pedido y registra en `pedidos_de_alta` quién, cuándo, qué versiones y ese mensaje;
  - el core decide (resultados tipados) y `pack_commerce` arma la respuesta de su tool;
  - el negocio creado guarda la aceptación en `tenants.settings["alta"]["aceptacion"]` y en `audit_log`
    (`negocio_creado`).
- **Documentos legales con versión.** `documentos_legales` guarda las versiones y nunca se edita: una
  versión nueva es una fila nueva, publicada por el master desde Plataforma. **Los textos definitivos dependen
  del abogado (HU-GTM-02):** al llegar se publica una versión nueva, sin tocar código. Mientras falte alguno
  de los tres, toda alta va a revisión.
- **Un Free por persona.** Si el mismo cliente, número de WhatsApp, teléfono o documento pidió un Free en los
  últimos 12 meses, la tool responde `free_no_disponible` y pide ofrecer un plan pago. Free es el plan de la
  línea, no el total de la cotización. El consumidor lo vuelve a comprobar (revisión).
  - Teléfono y documento se guardan como HMAC-SHA256 con una clave derivada de `APP_SECRET_KEY`, nunca en
    claro.
  - El cron diario `core.alta_huellas_retencion` vacía las huellas de más de 12 meses.
- **Límite por origen.** Hasta `ALTA_PEDIDOS_POR_HORA` (3) pedidos de planes por hora desde el mismo
  cliente del chat. Pasado el cupo, `limite_de_altas`. Usa el limitador compartido `core/security/rate_limit.py`.
- **Revisión humana.** El alta queda en revisión, con todos los motivos y un push a los dueños del vendedor,
  en estos casos:
  - el interruptor está pausado;
  - falta la aceptación;
  - un Free repetido;
  - un correo de dominio desechable (`core/provisioning/dominios_desechables.txt`);
  - `ALTA_INTENTOS_REVISION` (3) pedidos de alta de la misma persona en 24 h;
  - datos inconsistentes en el checkout (documento o teléfono inválidos).
  El motivo junta todos los controles que fallaron, también los de ADR-0121 (Free repetido, recompra).
- **Interruptor del master.** Se guarda en `tenants.settings["aprovisionamiento"]` del vendedor y queda
  auditado. Con el alta pausada, las altas nuevas quedan en cola de revisión. El proveedor de suscripciones
  y el preset leen el negocio con `FOR UPDATE` antes de escribir `settings`, así no pisan una pausa.
- **Aprobar un alta en revisión.** Solo una en revisión por un control de riesgo (una por un dato se
  corrige y se reintenta). Descartar toma el mismo candado que el consumidor. Queda `en_curso` y el worker la corre por el outbox
  (`provisioning.alta_aprobada`), sin los controles de riesgo que la frenaron; las validaciones de datos
  siguen. La aprobación es de un solo uso: si vuelve a revisión, o se reintenta, se borra. Queda auditada.
- **La prueba no se edita.** Triggers impiden editar o borrar una versión publicada y la aceptación de un
  pedido de alta (`purge_tenant` no se frena). Los enlaces legales son `https` y solo ASCII.
- Contrato nuevo de import-linter `antiabuso-sin-alta`: lo que usan los packs y la API del aprovisionamiento
  no llega al alta, ni de forma indirecta.
- **API de Plataforma**, solo para el master y solo sobre vendedores:
  `/api/v1/admin/platform/altas-automaticas` (estado, interruptor, documentos, cola, aprobar y descartar).
- **CLI:** `agent-core tenant aprobar-alta` y `agent-core tenant interruptor-altas`.
- Migración `0065` (`documentos_legales` y `pedidos_de_alta`, RLS forzada, triggers de inmutabilidad).
- `test_despachador_entrega_con_el_tenant_del_evento` despacha por lotes hasta encontrar su evento: con la
  suite completa hay más de un lote pendiente.
- `docs/openapi.json`, runbook `docs/runbooks/altas-en-revision.md` y `docs/despliegue-coolify.md` al día.
- ADR-0123 § 10 suma un requisito para encender el alta en producción: los documentos publicados, el
  playbook de ventas al día y los criterios de HU-SH-07 verificados en test.

### Primeros pasos calculados y embudo de activación (2026-10-09)

- HU-SH-06 (ADR-0124). `GET /api/v1/admin/onboarding` (sección `dashboard`) devuelve los primeros pasos del
  negocio **calculados del estado real**: datos del negocio, WhatsApp conectado y verificado (salud de
  HU-CONN-04), catálogo o servicios, probar el asistente y equipo invitado; además, las habilidades que el
  preset dejó pendientes de configurar (ADR-0110) y el tipo de negocio que espera un plan más alto. Cada paso
  trae un motivo (clave estable) y `puede_actuar` según la escritura del usuario en su sección.
- Hook opcional `BaseModule.contenido_cargado(tenant_id)` (contrato `core/primeros_pasos/contrato.py`): cada
  habilidad dice cuánto contenido cargó el negocio. Lo implementan `pack_commerce` (productos a la venta),
  `pack_calendar` (tipos de cita) y `pack_cobranza` (clientes). El core no lee tablas de los packs.
- Embudo de activación: `onboarding_hitos` (migración `0062`, RLS forzada) guarda la primera vez que se cumplió
  cada paso, con los segundos desde el alta (`tenants.created_at`), más la invitación aceptada y el primer
  mensaje de un cliente real. Idempotente (`ON CONFLICT DO NOTHING`), auditado (`primer_paso_completado`) y en
  el log (`onboarding_hito`). `agent-core tenant embudo [--dias N] [--solo-automaticas]` barre y agrega:
  negocios por paso, porcentaje, mediana desde el alta y cuántos dentro de las 24 horas.
- El canje del enlace de la invitación (`POST /api/v1/auth/accept-invitation`) **abre la sesión**: responde
  200 con `token`, `admin_id`, `tenant_id` y `expires_in` (antes, 204). Enmienda ADR-0121 § 3.8.
- `GET /api/v1/admin/me` suma `invitaciones_pendientes`; `contar_pendientes` usa los mismos filtros que la
  lista (vigentes y de negocios activos).

### Embedded Signup real en el panel nuevo (2026-10-09)

- HU-SH-05 (ADR-0128, implementa ADR-0080). El alta "Producción: conectar con Facebook" funciona de punta a punta
  con Embedded Signup v4: el panel abre el popup con el `config_id` de la plataforma y el backend canjea el `code`
  y encadena suscribir la WABA, registrar el número con el PIN del negocio, sincronizar plantillas y verificar
  salud. La verificación contra Meta real queda pendiente del dueño (app de la plataforma aprobada y configurada).
- Estado y cancelación del wizard: `GET /api/v1/admin/meta/wizard/session` (en qué paso quedó, sin efectos),
  `POST /wizard/cancel` y `POST /wizard/resume`. El canje exige el intento de un solo uso que da
  `POST /wizard/session` (atado al admin, vence a los 15 min). `/wizard/exchange`, `/wizard/number` y `/wizard/pin`
  devuelven el estado completo (`WizardSessionOut`) en vez de `{connectionId, nextStep}`. Los IDs del popup solo
  aceptan dígitos.
- El token del canje se guarda solo después de verificar que ve la WABA y el número; reautorizar con otra WABA u
  otro número da 409. El wizard ya no toma una conexión BYOK (409 `byok_connection_exists`) ni reactiva una pausada
  (409 `connection_paused`).
- El PIN de verificación en dos pasos lo escribe el negocio (antes se generaba uno que nadie veía); sin PIN la
  conexión queda `pending` hasta registrarlo. En producción también es obligatorio desde "Números".
- Un alta vencida (24 h) reutiliza la conexión borrador; `restart` empieza de nuevo; dos pedidos simultáneos ya no
  dan 500; la cadena tiene un tope de 45 s por request. Sin `META_APP_ID`/`META_APP_SECRET`/`META_CONFIG_ID` el
  wizard dice qué variable falta.
- La app de la plataforma: el alta de un negocio ya no re-registra su suscripción de webhook (solo el master); el
  cron de 15 min la lee con las credenciales del entorno (solo lectura) y alerta una vez si está inactiva. Una
  conexión de Embedded Signup solo queda `connected` con el token y esa suscripción verificados.
- Mensaje de prueba desde el hub de la conexión: `GET|POST /api/v1/admin/meta/connections/{id}/test-message`
  (plantilla aprobada sin variables, uno cada 30 s y 10 al día, dueño o master en producción, en la auditoría).
- "Plataforma → Embedded Signup", requisitos de Meta de la app de la plataforma (solo master):
  `GET /api/v1/admin/platform/meta-app/embedded-signup` y `POST .../step`. Migración `0066`
  (`platform_meta_app_requirements`, tabla de sistema; cuelga de 0063 y hay que re-encadenarla tras 0064/0065).
- Seguridad: errores de transporte del canje como `GraphError` y más claves (`client_secret`, `code`, `params`…) en el
  scrubber de Sentry; los logs de `debug_token` solo llevan la forma.

### El plan de SolversHub se paga por el asistente o por el panel, sin cobrar dos veces (2026-10-09)

- HU-SH-04 (ADR-0123). `tenant_subscriptions` del negocio es la fuente única del cobro de la
  plataforma. Cada período tiene la referencia `plat-<slug>-<AAAAMM>`, la misma en los dos canales,
  y se aplica una sola vez:
  - `aplicar_pago_de_periodo` es la única puerta;
  - un índice único de `subscription_payments` admite un pago `aprobado` por período;
  - un segundo pago del mismo período queda `duplicado` y se alerta.
- Los períodos pagos se cortan **por calendario** (mismo día del mes siguiente, con día ancla), igual
  que `pack_subscriptions`: el primer período del alta y el del panel duran un mes, no 30 días. La
  regla vive en `core/subscriptions/periodos.py`.
- **Canal asistente:** al pagarse el cargo de un período, `pack_subscriptions` emite
  `subscription.charge_paid` y el core extiende el plan del negocio aprovisionado.
- **Canal panel:**
  - `/plan` cobra el período que toca, con su referencia;
  - al pagarse, `platform.period_paid` deja pagado el cargo del mismo período en el asistente
    (sincronización inversa): el asistente no lo vuelve a cobrar ni a recordar;
  - `GET /api/v1/admin/plan` trae `cobro` y `plan_por_el_asistente`, y los pagos traen `periodo`.
- **«Ya pagado» en los dos canales:**
  - `sub_link_pago` devuelve `ya_pagado`;
  - `POST /api/v1/admin/plan/subscribe` responde 409 `ya_pagado` y no adelanta períodos con el plan al
    día;
  - un negocio del asistente cambia de plan en el asistente (409 `cambio_en_el_asistente`).
- **Renovación** (`core.billing_renovacion`, diaria, 13:00 UTC): 5 días antes del fin del período
  (`BILLING_DIAS_RENOVACION`) genera el cobro del siguiente y avisa por el canal preferido:
  - por WhatsApp: el cargo y su aviso con la plantilla de `subscription.charge_created` fuera de
    24 h los manda el cron del vendedor (`dias_anticipacion` igual a `BILLING_DIAS_RENOVACION`), que
    ya no le manda link a quien pagó el último período por el panel;
  - o por correo a los dueños, con el enlace a `/plan`.
- **Conciliación** (`core.billing_conciliacion`, diaria): compara el asistente con el negocio y alerta
  cada diferencia con su detalle: auditoría, Slack y push. Qué hacer con cada una:
  `docs/runbooks/cobro-de-plataforma.md`.
- **Cambio de plan desde el asistente:** un Free del alta que compra un plan pago cambia de plan en el
  mismo negocio y recibe el preset que había quedado pendiente. Comprar después de que venció la prueba
  ya no crea otro negocio: la suscripción Free vencida se reusa.
- **Riesgo R-05 verificado:** Wompi admite una sola URL de eventos por cuenta y ambiente. Con una cuenta
  compartida, `/webhooks/billing/wompi` reenvía al negocio de `BILLING_WOMPI_EVENTOS_TENANT` los eventos
  que no son de la plataforma; su pack de pagos los verifica con su propio secreto.
- **Migraciones:**
  - `0061`: `subscription_payments.periodo` con su índice único, `tenant_subscriptions.dia_ancla` e
    índice del negocio en `provisioning_requests`;
  - `pack_subscriptions_0007`: `pack_subscriptions_charges.pago_externo`.
- **Cierres de dinero** (revisión de seguridad y arquitectura):
  - un cargo o un cambio de plan del asistente por menos que el precio del plan no se aplica
    (`monto_distinto` + alerta);
  - la alta manual de suscripciones del pack exige permiso de escritura;
  - el webhook del panel exige el monto firmado por Wompi;
  - al cambiar de plan se anulan los links pendientes del plan anterior;
  - una recompra después de cancelar va a revisión.
- `PROVISIONING_SELLER_TENANTS` va en la **app y en el worker**. Los requisitos para encenderla en
  producción están en ADR-0123 § 10.
- Un test exige un solo head de Alembic en el core y en cada pack.

### El negocio de quien compra un plan se crea solo (2026-10-09)

- HU-SH-03 (ADR-0121). Cuando un negocio vendedor (`PROVISIONING_SELLER_TENANTS`, UUID, en el worker;
  vacía = apagado; **no encenderla en producción antes de HU-SH-04**) activa la suscripción de un plan
  de la plataforma, el core crea el negocio del comprador:
  - con el plan comprado: Premium activo con el período pagado y su pago en `subscription_payments`;
    Free en prueba de 30 días;
  - con el preset de su tipo de negocio, sin datos de ejemplo;
  - con una invitación al owner.

  No hay tokens ni tools para el LLM: lo dispara el outbox (`subscription.activated`) y firma la
  auditoría la identidad de servicio `provisioning`.
- Cada alta queda en `provisioning_requests` (migración `0059`, RLS forzada en el vendedor), única por
  referencia del pedido y por suscripción: un evento repetido, un pedido con dos planes o un worker que
  se cae no duplican negocios.
- Queda en `revision`, con el motivo y un push a los dueños del vendedor, lo que no se puede resolver
  solo: sin correo, plan desconocido, slug ajeno, correo de un master, cliente distinto o un Free repetido
  del mismo cliente o correo. El master la reintenta o la descarta con
  `agent-core tenant reintentar-alta | descartar-alta` (`docs/runbooks/altas-en-revision.md`).
- El enlace de un solo uso (`tenant_invitations.token_nonce`) va **solo por correo** y solo a una cuenta
  que el alta creó sin contraseña. El owner la define con `POST /api/v1/auth/accept-invitation`, que tiene
  bloqueo progresivo y solo sirve para una cuenta limpia. Una cuenta existente entra y acepta en la app.
- Por WhatsApp sale siempre el mismo aviso, sin token y con el correo enmascarado
  (`provisioning.invitation`). Aceptar una invitación vencida en la app ya no da acceso.
- `onboard_tenant` pasa a `core/alta_negocio.py`, y el contrato `alta-fuera-del-llm` de import-linter se
  lo prohíbe a las tools y a los packs.
- `pack_commerce`:
  - el pedido de un plan guarda `datos_alta` (migración `crm_0011`): correo, teléfono, nombre y tipo de
    negocio;
  - `checkout_fields` admite `business_name` y `vertical`, que pide `missing_fields` (no van en el schema
    de la tool). `vertical` se valida contra la lista de presets, y un nombre con enlaces se rechaza;
  - un plan va en su propio pedido: `agregar_item_pedido` ya no le suma productos.
- `subscription.activated` suma `variant_id` y manda `referencia` nula cuando no hubo pedido.

### Checklist de producción real: por conexión y de la plataforma (HU-CONN-07, 2026-10-09)

- **Por conexión.** `GET /api/v1/admin/meta/connections/{id}/production` devuelve el último resultado
  guardado (con fecha) del checklist de producción de esa conexión: token, número registrado en la Cloud
  API, WABA suscrita a la app, webhook (suscripción de la app de esa conexión al objeto de su canal y
  override por número) y plantillas mínimas aprobadas (al menos una, y todas las asignadas a avisos en
  `message_templates_map`). Se evalúa con las credenciales de ESA conexión (resolver de HU-CONN-01).
  `POST .../production/run` (202) solo registra la corrida y encola el job.
- **Solo lo necesario.** `check_app_subscriptions_complete` exige solo `whatsapp_business_account`;
  `page` e `instagram` se informan como no obligatorios en vez de dejar el check siempre en rojo.
- **Plataforma aparte, solo master.** `GET /api/v1/admin/platform/meta-app` y `POST .../meta-app/run`:
  webhook de la plataforma, suscripciones de la app, App Secret distinto al de desarrollo, claves de
  cifrado, worker y DLQ, RLS forzada, tests de aislamiento (informa el commit, sin pytest) y secretos en
  plantillas. Un check que revienta queda "sin evaluar" sin tumbar la corrida.
- **Sin bloqueo.** Las dos corridas son jobs de Celery (`core.meta_connection_production_check` y
  `core.platform_readiness_check`); el encolado va en un hilo. El resultado queda en
  `meta_production_checks` (por negocio, RLS forzada) y `platform_readiness_runs` (tabla de sistema),
  migración `0064`. Una sola corrida activa por conexión y una para la plataforma; una activa de más de
  10 minutos se muestra fallida y deja pedir otra.
- **Obsoletas** (se mantienen para la app móvil): `GET /admin/meta/checklist/production` y
  `POST /admin/meta/checklist/production/run`. La segunda ya no corre los checks de la plataforma ni nada
  en el request: encola la revisión de cada conexión y devuelve lo guardado.
- **Tras la revisión de seguridad:** espera mínima de 60 s entre corridas terminadas (cada una llama a
  Meta); el worker solo toma corridas en cola y vigentes, con tope de 240 s; el servicio exige master
  para la plataforma y un test impide usar `platform_readiness_runs` fuera de su servicio; el GET al
  override de webhook de un número solo va a hosts públicos por HTTPS (anti-SSRF, `ScopedHttp`); los
  detalles guardados ya no llevan el texto de excepciones de red ni el cuerpo crudo de una respuesta no
  JSON de Meta; y "Requisitos de Meta" ya no muestra a un negocio los datos de la app de la plataforma
  (nombre, suscripciones, `config_id`): eso queda en "Plataforma → App de Meta".

### BYOK de producción con registro de número y promoción (2026-10-09)

- HU-CONN-06 (ADR-0127). El wizard BYOK de la API elige el entorno (`environment`: `sandbox` o `production`) y lo
  guarda en la conexión. Antes BYOK quedaba siempre en prueba. En producción se rechaza el número de prueba de
  Meta (`account_mode = SANDBOX`) y el alta pide ser propietario del negocio o master.
- Registro del número con PIN: si Meta no reporta el número registrado en la Cloud API (`platform_type`), la
  conexión queda `pending` ("Falta registrar el número en la Cloud API con su PIN") y pasa a `connected` al
  registrarlo, con el PIN en el mismo alta (`pin`) o desde "Números". Antes el alta lo marcaba registrado a ciegas.
  En una BYOK de producción el PIN es obligatorio (no se genera uno que nadie ve).
- "Pasar a producción" de una conexión BYOK de prueba: `GET/POST /api/v1/admin/meta/connections/{id}/promotion`
  (`/number`, `/verify`, `/finish`). El número real crea la conexión de producción de la misma app con el alta BYOK
  (la de prueba sigue funcionando); con registro, plantillas aprobadas y los chequeos de la app del negocio en
  verde, la de prueba se da de baja. Sus conversaciones nunca se responden desde el número real.
- Los chequeos de producción de una conexión BYOK usan su app (token, App ID y App Secret de la conexión), nunca
  la de la plataforma, y exigen que la WABA esté suscrita a esa app. `/promotion/verify` también sirve para una
  BYOK creada directo en producción.
- Cada alta BYOK guarda un `verify_token` propio (el que escribe el admin o uno aleatorio) en vez de registrar el
  global, y el wizard rechaza el App ID de la app de la plataforma (seguimientos de ADR-0126).
- `origin` en `GET /api/v1/admin/meta/health`. "Diagnosticar" no mejora una conexión que espera el registro, pero
  sí la pasa a `error` si el token murió. Registrar con PIN ignora los números dados de baja.

### Webhook y secreto por conexión (2026-10-09)

- HU-CONN-05 (ADR-0126). Un negocio con varias conexiones de WhatsApp (una BYOK con su app y otra de
  Embedded Signup, o dos apps propias) recibe de todas: cada mensaje se verifica con el App Secret de la
  conexión dueña de su número o WABA (`core/gateway/webhook_secrets.py`). Antes había un solo secreto por
  negocio y la otra conexión daba 401.
- El secreto lo elige la base, no el cuerpo: el de una conexión no autoriza los números de otra, ni del mismo
  negocio (`signature_connection_mismatch`). Por el slug, un número que no es de ninguna conexión del negocio
  ya no se le atribuye: 401 o `unresolved_tenant`.
- El handshake `GET /webhooks/whatsapp/<slug>` acepta el `verify_token` de cada conexión del negocio.
- "Registrar webhook" por conexión: `POST /api/v1/admin/meta/connections/{id}/webhook/register`. En una BYOK
  registra la app de esa conexión con la URL del negocio y su token, sin caer nunca a la app de la plataforma;
  en una de Embedded Signup (app de la plataforma) solo lo puede hacer el master. `POST /webhooks/register`
  queda obsoleta y `GET /webhooks` trae `connection_id`, `origin`, `uses_platform_app` y `can_register`.

### Verificador de afirmaciones con modelo de evidencia (HU-ARCH-29, 2026-10-09)

- **Nuevo:** `core/orchestrator/verificador/`. Antes de enviar cada respuesta, el loop compara el borrador con lo que
  el sistema hizo (ADR-0119, adenda de implementación):
  - **Montos:** toda cifra en `$`/`COP` tiene que haber salido de una tool, de este turno o de uno anterior. Si no,
    reintento con los montos verificados del turno.
  - **Afirmaciones de cierre** que declaran los packs ("tu pedido quedó confirmado", "te envío el link de pago"):
    exigen una tool del turno que las respalde. Las preguntas, las negaciones ("todavía no generé el link") y los
    condicionales ("para generarte el link necesito…", "con tu sí te lo envío") ya no cuentan como afirmación.
  - **Links:** una URL que no devolvió una tool del turno ni está en el prompt o en los mensajes del cliente se quita
    del texto, sin reintento. El link exacto lo sigue entregando `_safe_append`.
  - **Referencias y fechas:** se cuentan en el veredicto, sin bloquear (modo sombra).
  - Mismo reintento (`guardrail_max_retries`) y misma acción al reincidir (`guardrail_fail_action`, ADR-0092). Si el
    verificador falla, el turno sigue (fail-open para montos y cierres) y los links se quitan todos.
- **Retirados** `crm_amount_check` y `crm_completion_check` de pack-commerce. Sus montos y sus afirmaciones pasan al
  verificador, y pack-commerce las declara con `MCPHost.register_afirmaciones`. El link de pago lo respalda cualquier
  tool del turno que haya devuelto un link (antes, `sub_cambiar_plan` escalaba compras válidas). Nuevas
  afirmaciones `cotizacion_generada` y `cotizacion_aceptada` ("Ya dejé tu cotización aceptada" sin
  `confirmar_cotizacion`, visto en los journeys). `calendar_completion_check` sigue igual.
- **Interruptores por negocio:** `verificador_montos`, `verificador_cierre` y `verificador_links` en "Guardrails
  activos". Los nombres viejos se traducen solos: un negocio que había apagado `crm_amount_check` lo mantiene apagado.
- **Veredicto por turno** en `audit_log` (`core:verificador` / `verificacion`), solo códigos y conteos.
- **Medición:** `python evals/verificador.py` corre el verificador sobre el set etiquetado
  `evals/verificador/respuestas_etiquetadas.jsonl` (borradores reales de los journeys con sus resultados de tools, más
  los incidentes reales). `tests/test_verificador_set.py` exige falsos positivos ≤ 2 % y ningún falso negativo.
  Hoy: 205 correctas con 0 falsos positivos y 14 falsas con 0 falsos negativos. En los mismos 90 borradores, los
  guardrails viejos tenían 15 falsos positivos (16,7 %). Journeys locales con k = 4: pass@1 95 %, pass^4 91 %.
- **Seguridad** (revisión del diff): las URLs se comparan normalizadas e incluyen las que empiezan por `www.`; se
  quitan por posición hasta que no quede ninguna sin respaldo; se verifica el texto ya sin markdown; la memoria y el
  perfil que escribe el asistente no cuentan como evidencia; si el verificador falla se quitan todos los links;
  `parse_money` descarta cifras no finitas y el patrón `cerr… pedido` deja de ser cuadrático ante espacios.
- `core/shared/dinero.py`: `parse_money`, `MONEY_KEYS` y `MONEY_IN_TEXT` pasan de pack-commerce al core
  (`docs/PLUGIN-CONTRACT.md` §11.1 y §13).
- Instrucciones de reintento en el playbook `respuestas` (`verificador_monto_sin_evidencia`,
  `verificador_status_sin_evidencia`).

### Contrato procedure/1, linter y conversión de flujos (2026-10-09)

- HU-AOS-03 (SPEC-26, ADR-0113). `agentcore-contracts` 2.1.0 suma el contrato `procedure/1`
  (`agentcore_contracts.procedure`): disparadores, datos, etapas con sus acciones permitidas, confirmación,
  riesgo, cierre, recuperación, afirmaciones y escenarios. Entra desde el inicio con las tres extensiones
  de la HU: dato llenado desde un payload interactivo (`setBy: "interactive:plan:"`), dato llenado por el
  tipo de contenido que envía el cliente (`setBy: "content:location"` o `"content:image"`) y dato con
  candidatos para elegir (`candidates`). Suma también `requiredWhen` (la dirección solo con domicilio),
  que exige el pedido con entrega; `requires`, las habilidades de las que usa acciones por nombre (como
  `generar_link_pago` de pack-payments), y `confirmActions`, la confirmación por acción que hereda un
  procedimiento abierto del `requiresConfirmation` de su binding v2 (adendas de ADR-0113). Es aditivo: ningún contrato existente cambia de forma; el 3.0.0 llega
  con AgentSpec v3 (HU-AOS-12). Los tests del paquete pasan a `tests/test_contracts_congelados.py`, que sí
  corre.
- Validador y linter en `core/kernel/procedures/`, con un mensaje por problema: acciones inexistentes,
  etapas inalcanzables, acciones `irreversible` sin confirmación, afirmaciones de dinero sin fuente del
  servidor, acciones de una habilidad que no está en `requires` y referencias rotas (datos, etapas,
  prefijos repetidos, resolvers de otra habilidad). Devuelve a lo sumo 50 problemas y uno que cuenta el
  resto. Las
  acciones se comprueban contra el binario (core y habilidades), nunca contra datos de un negocio. El
  efecto de cada acción llega con el manifiesto de acciones v2 (HU-AOS-34): hasta entonces la regla de
  irreversibles no marca acciones reales.
- `agent-core procedures lint <archivos>` y paso en el CI sobre los procedimientos críticos de ejemplo
  (`tests/fixtures/procedures/`: consultar planes, contratar plan, pedido con entrega, agendar cita y cobro
  de suscripción).
- `POST /api/v1/admin/procedures/validate` (sección Habilidades con escritura): devuelve
  `{valid, errors}` con todos los problemas; tope de 64 KiB y 400 ante un JSON inválido, anidado en exceso
  o con enteros enormes. En `docs/openapi.json`.
- Las afirmaciones con frases se compilan a la `Afirmacion` de `register_afirmaciones` (ADR-0119), con las
  frases escapadas: un solo verificador. Montos y links ya los cubre el verificador genérico.
- `flow_to_procedures` (`core/agents/flow.py`) convierte el flujo compuesto en procedimientos abiertos: uno
  por sección, con una etapa que permite exactamente sus acciones, `requires` con las habilidades ajenas que
  usa y la confirmación heredada del binding v2. Pasan el linter completo.
- Sin cambio de conducta: nada en runtime lee procedimientos todavía.

### Esquema aditivo del Agent OS (expand) (2026-10-09)

- HU-AOS-02 (#363, SPEC-26, ADR-0112..0118). Cinco migraciones aditivas, sin cambio de conducta:
  nada en runtime lee ni escribe lo nuevo.
  - `0067`: `UNIQUE (tenant_id, id)` en `channel_connections`, `conversations`, `customers` y
    `meta_connections` (y en las chicas `agent_specs` y `eval_runs`), con `CREATE UNIQUE INDEX
    CONCURRENTLY` + `ADD CONSTRAINT … USING INDEX`, una tabla por transacción; y la función genérica
    `prevent_mutation()` (columnas exentas por `TG_ARGV`; rechaza también TRUNCATE).
  - `0068`: catálogo de sistema `pack_versions` (inmutable salvo `yanked`, que no se revierte; solo
    `SELECT` para `app_runtime`), vista `agent_templates` (`security_invoker`, sin `tenant_id`) y
    `plugins.owner_tenant_id` (NULL = plataforma).
  - `0069`: 21 tablas de negocio vacías (agentes, versiones e instancias; grafo A2A; instalaciones
    con scope; releases por conexión; tareas *event-sourced*; ledger del Coordinador; A2A;
    `memory_facts`; `trigger_embeddings`; feedback y propuestas), con RLS forzada, FKs compuestas
    `(tenant_id, …)`, `prevent_mutation()` en las append-only y sin `DELETE` para `app_runtime`.
    Integridad entre entidades por FK: el rol de la instancia es el `kind` del agente, un
    componente de release es de la conexión de la release y su versión es del agente, y los dueños
    e iniciadores son instancias de esa conexión. Las referencias al turno son diferidas al COMMIT.
    `pack_installations` solo guarda texto cifrado y rechaza el plugin privado de otro negocio.
    Funciones de trigger con `search_path` fijo.
  - `0070`: columnas nullables o con default en `channel_connections`, `conversations`, `messages`,
    `usage_ledger` (`kind`) y `audit_log`; CHECKs y FKs compuestas `NOT VALID` (turno, conexión,
    instancia, versión y release); triggers diferidos "release
    activa ⇒ Coordinador" en conexiones e instancias (con `FOR SHARE` y fallo cerrado); banderas
    `tenants.aos_runtime` y `tenants.aos_procedures` con el CHECK de orden. Cada tabla en su propia
    transacción, re-ejecutable.
  - `0071`: índices concurrentes (`uq_cc_webchat_key` global, también sobre las borradas; atribución por turno, conexión y
    agente, parciales) y `VALIDATE` de `ck_usage_kind`.
- `alembic/env.py`: `transaction_per_migration=True`, para que los locks de una revisión no se
  retengan hasta el final de la tanda.
- Sin modelos SQLAlchemy en el expand: llegan con la HU que primero lea o escriba cada tabla.
  Quedan fuera la memoria por scope (`memory_vectors`, HU-AOS-36), las columnas del gate en
  `eval_*` (HU-AOS-21), el contract (HU-AOS-11/15), el resolvedor de `webchat`, el sync de
  `pack_versions` (HU-AOS-14) y la federación (HU-AOS-39).
- `docs/MULTITENANT.md`: tablas de sistema y FKs compuestas. Tests de integración
  `tests/integration/test_agent_os_esquema.py` (`aislamiento`): RLS forzada y permisos por tabla,
  0 filas desde otro negocio, FKs compuestas, triggers y `purge_tenant` con datos.

### ADRs del Agent OS aceptados (2026-10-09)

- ADR-0112 a ADR-0118 (HU-AOS-01) corregidos tras dos pasadas del arquitecto contra lo que entró después de
  escribirlos (ADR-0119 a 0128) y **aceptados** por el dueño.

### `must_not_call_tools` respeta la lista de tools del escenario (2026-10-09)

- `evals/runner.py`: la aserción `must_not_call_tools` con una lista de nombres solo falla si el asistente
  llama alguna de esas tools; antes cualquier valor truthy se evaluaba como «no debe llamar ninguna tool»
  (el escenario 17 `limite_de_altas` del tenant solvershub fallaba por un `set_conversation_note` que no
  estaba en la lista). `true` sigue prohibiendo cualquier tool; `false`, la lista vacía o la clave ausente
  no restringen nada, y otro tipo (un string suelto, un número) hace fallar el escenario como inválido.
  Tests en `tests/test_evals_runner.py`.

### Datos del alta en el schema de `crear_pedido` (2026-10-09)

- `crear_pedido` (pack-commerce) declara en `customer_data` los campos opcionales `business_name`,
  `vertical` (alta automática, ADR-0121) y `acepta_terminos` (el token de `terminos_pendientes`,
  ADR-0125). Antes el modelo los mandaba solo por el `hint` y el playbook de ventas. Van sin enum: las
  opciones de `vertical` siguen en la respuesta y el schema no cambia con los presets (ADR-0129).
- La tool sigue validando: token de términos atado a la conversación y a las versiones vigentes,
  `vertical` como id de un preset y `business_name` sin enlaces.
- Tests: el schema los declara opcionales y sin enum (reemplaza el test que fijaba lo contrario); el token de términos de un vendedor no vale en
  otro (`aislamiento`).

### Un solo checklist para "Pasar a producción" y para la App de Meta (2026-10-09)

- HU-CONN-06 sobre HU-CONN-07. "Verificar" y "Pasar a producción" de una conexión BYOK ya no tienen una lista
  propia de chequeos: usan el checklist por conexión de HU-CONN-07 (token, número registrado, WABA suscrita,
  webhook y plantillas mínimas, también las de los avisos) y le suman lo que solo pide la promoción: que la app sea
  propia, que el número sea real (no el de prueba de Meta), que esté registrado en la Cloud API y que la WABA esté
  suscrita a la app del negocio. La respuesta trae cada ítem con su etiqueta y su acción de reparación, como en
  "Producción" de la conexión (`PromotionCheck` se reemplaza por `ReadinessItem`). Como corren en el request,
  hay una sola revisión a la vez por negocio, 10 s de espera después y un tope de 60 s (504 si Meta no responde).
- HU-SH-05 sobre HU-CONN-07. Los requisitos de Embedded Signup ya no revisan por su cuenta las variables de la app,
  el HTTPS ni el webhook: la revisión de "Plataforma → App de Meta" suma el ítem `meta_app_configured`
  (`META_APP_ID`, `META_APP_SECRET`, `META_CONFIG_ID`) y el webhook falla si `PUBLIC_BASE_URL` no es HTTPS. Los
  requisitos solo quedan "listos" con los pasos manuales confirmados y la última revisión de la plataforma sin ítems
  en rojo y de hace menos de 30 días (`platform_check` reemplaza a `automatic`; `expired` y `expires_at` dicen si
  venció). Pasado el plazo vuelven a pendiente y la página pide revisar de nuevo.

### Dos reservas a la vez ya no dejan una cita "agendada" sin fila (2026-10-09)

- HU-SH-02. Cuando dos clientes reservan el mismo horario a la vez, Postgres puede cortar uno de los
  inserts con `deadlock detected` en vez de con la restricción sin solape. El espejo lo tomaba como un
  error cualquiera y la tool respondía `created` sin `event_id`, y el cliente leía que su cita había
  quedado agendada. Ahora el insert se reintenta (hasta 3 veces) y, al reintentar, la base dice quién
  ganó. Si en modo interno igual no queda la fila, la tool devuelve un error en vez de confirmar.
  Lo encontró el CI (`test_dos_reservas_a_la_vez_una_gana_y_la_otra_recibe_alternativas`, intermitente).

### El link del cargo de una suscripción le llega al cliente (2026-10-08)

- `sub_link_pago` devolvía la URL solo en el resultado de la tool: si el modelo no la copiaba, el cliente leía "te
  envío el link de pago" sin link. Ahora la tool trae `_safe_append` con el link exacto, el plan, el período y el
  monto, y el core lo adjunta (como el link de un pedido). Encontrado por los journeys de HU-SUB-12 (3 de 3 rondas).

### Las tools declarativas ya no llegan a la red interna (2026-10-08)

- Seguridad (SSRF, severidad alta, hallazgo del `auditor-multitenant`): un admin del tenant podía apuntar una
  tool declarativa (ADR-0052) a hosts internos que el chequeo de `ScopedHttp` dejaba pasar, y la respuesta volvía
  al chat: nombres de Docker de una etiqueta (`redis`, `postgres`, `api`), formas numéricas que glibc resuelve
  (`2130706433`, `127.1`, `0x7f.1`) y dominios que resuelven a IPs privadas o de metadata
  (`169.254.169.254.nip.io`).
- `ScopedHttp.request` ahora rechaza esos hosts sin resolver, resuelve el DNS y rechaza si alguna IP es privada,
  loopback, link-local, reservada, multicast, sin especificar, CGNAT (`100.64/10`) o IPv4 embebida en IPv6, y
  conecta a la IP validada con `Host` y SNI del dominio (el certificado se sigue verificando contra el dominio;
  sin DNS rebinding). Sin redirects ni proxies del entorno. Un dominio que no resuelve da el mismo error que uno
  interno (no deja enumerar nombres de Docker/Coolify), y si una IP no conecta se prueba la siguiente (IPv4 primero,
  connect de 5 s por intento).
- Las tools declarativas solo guardan `allowed_domains` que sean FQDN públicos; el admin avisa cuáles rechazó.
  Las ya guardadas con dominios internos quedan sin allowlist (fail-closed). Los packs (`plugin_http`, p. ej.
  pack-calendar hacia `www.googleapis.com`) siguen igual.

### El primer turno tras sumarse un playbook de pack ya no se cae (2026-10-08)

- `_ensure_default_playbooks` agregaba los playbooks faltantes con `model_copy(update=...)`, que no valida: el
  AgentSpec devuelto traía dicts y el turno se caía en `load_playbook` (`'dict' object has no attribute 'name'`).
  Pasaba en el primer mensaje de un negocio después de habilitar un pack (o de que un pack sumara un playbook);
  la fila sí quedaba bien, así que el siguiente turno funcionaba. Ahora el spec se valida. Visto con los journeys
  de HU-SUB-12.

### El rate limit del chat web es por widget y vive en Redis (2026-10-08)

- `POST /api/v1/public/web-chat/{key}/messages` limitaba por IP con un dict del proceso compartido por todos los
  tenants: una IP que escribía a un widget gastaba el cupo de los demás, y las IPs nunca se borraban (el dict
  crecía sin límite). Ahora el cupo (30 por minuto) es por `(key del widget, IP)`, en un sorted set de Redis con
  ventana deslizante y TTL de la ventana: las IPs inactivas se borran solas y el límite vale para todas las
  instancias. El set guarda como mucho el cupo + 1, así que una inundación no infla Redis, y una `key` con forma
  inválida cae en un cupo común en vez de crear claves arbitrarias. La IP sale de `client_ip()` (la real detrás
  de proxies de confianza, `TRUSTED_PROXY_CIDRS`), no la del proxy, que juntaba a todos los visitantes de todos
  los tenants en un solo cupo; esa misma IP es la que se manda a Turnstile. La respuesta sigue siendo 429
  `rate_limited`. Si Redis no responde, deja pasar (el `Origin` y Turnstile siguen siendo barreras).

### El rate limit del intake de leads es por tenant y vive en Redis (2026-10-08)

- `POST /api/pack/pack_commerce/leads/{slug}` limitaba por IP con un dict del proceso compartido por todos los
  tenants: una IP que mandaba leads a un tenant gastaba el cupo de los demás, y las IPs nunca se borraban (el dict
  crecía sin límite). Ahora el cupo (30 por minuto) es por `(slug, IP)`, en un sorted set de Redis con ventana
  deslizante y TTL de la ventana: las IPs inactivas se borran solas y el límite vale para todas las instancias.
  El set guarda como mucho el cupo + 1, así que una inundación no infla Redis. La IP sale de `client_ip()` (la
  real detrás de proxies de confianza, `TRUSTED_PROXY_CIDRS`), no la del proxy, que juntaba a todos los clientes
  en un solo cupo. La respuesta sigue siendo 429 `rate_limited`. Si Redis no responde, deja pasar (el secreto
  del intake sigue siendo la barrera). Señalado por el auditor multitenant.

### Un solo rate limit en el core, con cupo de claves inexistentes por IP (2026-10-08)

- El widget del chat web y el intake de leads de pack-commerce tenían el mismo limitador copiado. Ahora los dos
  usan `core/security/rate_limit.py`: ventana deslizante en un sorted set de Redis, tope de tamaño, TTL de la
  ventana y fail-open si Redis no responde. Cada ruta solo arma su clave; el cupo por (widget o slug, IP) no
  cambia (30 por minuto).
- Una `key` del widget o un `slug` al azar en cada request abría un cupo nuevo cada vez, así que esos 404 no
  tenían límite y cada uno consultaba la base. Ahora cada 404 por clave inexistente suma en un cupo de fallos por
  IP (20 por minuto): con ese cupo lleno, la IP recibe 429 `rate_limited` antes de resolver el tenant. Solo
  cuentan los fallos, y no frenan a las claves que se sabe que existen: cada resolución exitosa anota la `key` o
  el `slug` como conocido en Redis por 15 minutos. Así, tenants que comparten IP (hosting compartido, CGNAT de los
  operadores móviles) no se cortan entre sí, ni con tráfico legítimo ni porque uno mande claves inexistentes.
  Aplica a `POST /api/v1/public/web-chat/{key}/messages` y a
  `POST /api/pack/pack_commerce/leads/{slug}`.
- El log de Redis caído pasa a ser `rate_limit_unavailable` con `name` (`web_chat` o `lead_intake`), en vez de
  `web_chat_rate_limit_unavailable` y `lead_intake_rate_limit_unavailable`.

### El asistente puede vender planes y dar el link de una suscripción (2026-10-08)

- Un plan recurrente (`billing_period`) ya no se controla por inventario: con stock 0 (como el catálogo de
  `solvershub`), `consultar_inventario` lo marcaba no disponible y `generar_cotizacion` lo rechazaba ("No hay stock
  suficiente"), así que el asistente escalaba en vez de vender. Ahora figura disponible (`stock: null`), se cotiza y
  el pedido no descuenta inventario.
- El guardrail `crm_completion_check` acepta el link de `sub_link_pago` (período de una suscripción) y de
  `generar_link_pago` como evidencia de "link de pago"; antes solo el del pedido, y el aviso del cargo terminaba en
  escalamiento.
- Ambos los encontraron los journeys de HU-SUB-12.

### Un plan se pide sin método de entrega (2026-10-08)

- `crear_pedido` exigía `delivery_method` también para planes (un servicio): el asistente insistía en "¿domicilio o
  recoger en tienda?" con el Plan Free y nunca creaba el pedido, y la respuesta decía "entregado en None, None,
  None". Ahora es opcional para planes (no se pregunta ni se muestra), los productos lo siguen pidiendo, y un plan
  nunca pide dirección aunque la config del negocio sea anterior a la clave `plan` de `checkout_fields`. Encontrado
  por los journeys de HU-SUB-12.

### Comercio y pagos salen a red solo por el cliente acotado del core (2026-10-08)

- `pack-payments` dejó de usar `httpx` directo (ADR-0052). `WompiProvider` recibe un `ScopedHttp`. La tool
  `generar_link_pago` le pasa su `context.http`. "Probar y guardar" del panel y la capacidad de pagos que usa cobranza
  no tienen `ToolContext`, así que usan `plugin_http` con la allowlist del pack: `production.wompi.co` y
  `sandbox.wompi.co`. `checkout.wompi.co` sigue en el manifest, pero solo lo abre el navegador del pagador.
- `pack-commerce` valida Turnstile del intake de leads por `plugin_http` (`challenges.cloudflare.com`, ahora también
  en `network` del manifest). Mismo timeout de 8 s, y sigue fail-closed: un fallo de red o una respuesta
  inesperada rechaza el lead.
- Sin cambios de comportamiento en Wompi: timeout de 15 s; un 4xx/5xx sigue siendo `PaymentRequestRejected` (recuperable,
  sin `auto_disable`); un fallo de red es `PluginError`, igual que antes la excepción de `httpx` envuelta por el host. El
  mensaje de rechazo ahora muestra el cuerpo como JSON re-serializado (antes era el texto crudo). Un 2xx sin JSON ahora es
  `PaymentRequestRejected("sin id")`, antes era una excepción de parseo. Si la red falla cuando comercio pide un link
  (capacidad de pagos), el error ahora es de `pack_payments` y el auto-disable apaga los pagos del tenant, no todo
  `pack_commerce`.
- El test AST que impedía importar `httpx` en la agenda ahora cubre todos los `packages/pack-*/**/*.py` y también
  `requests`, `aiohttp`, `urllib3`, `urllib.request` y `http.client`. Además verifica en cada pack que la
  `network_allowlist` esté declarada en `network` del manifest, y que cada `plugin_http(X)` reciba una constante igual
  a esa allowlist (`tests/test_packs_salida_a_red.py`).
- `pack-payments` ya no declara `httpx` como dependencia.

### El pago en línea se confirma con el monto que firmó Wompi (2026-10-08)

- `payment.confirmed` llevaba el monto que pedimos al crear el link, no el que Wompi dice que se pagó: la
  validación de "monto exacto" de los consumidores (suscripciones, pedidos, cobranza) comparaba dos valores
  del servidor. Ahora `WebhookEvent` trae `amount` (`data.transaction.amount_in_cents` / 100, solo si la firma
  cubre ese campo) y `pack_payments` lo compara con el del link: si difiere, o no viene firmado, la transacción
  queda en `monto_distinto`, no se emite `payment.confirmed`, se registra `payments_pago_monto_distinto` en la
  auditoría y se avisa a los dueños por push (una vez, aunque Wompi reintente). El evento lleva el monto real.
- El mínimo de 150.000 COP del link de pago es del sandbox de Wompi; en producción Wompi publica 1.500 COP
  (modelo Agregador). `WompiProvider` aplica el mínimo según `sandbox` y la prueba de conexión de la pestaña
  Pagos crea el link por el mínimo del ambiente de la llave. El mínimo propio de `pack_commerce`
  (`pago_en_linea_monto_minimo`, 150.000 por defecto) no cambia: en producción hay que bajarlo por tenant.

### La agenda sale a Google solo por el cliente acotado del core (2026-10-08)

- `pack-calendar` dejó de usar `httpx` directo (lo señaló el auditor multi-tenant en HU-SH-02). Las tools
  (`create_event`, `update_event`, `cancel_event`, `check_availability`/`find_next_available`) llaman a
  Google Calendar y refrescan el token por `context.http`. El panel (`admin_api`, `admin`) y el OAuth, que no tienen
  `ToolContext`, usan `plugin_http` con la misma allowlist. Todo vive en `pack_calendar/google_api.py`.
- Core: `BaseModule.network_allowlist` declara los dominios del pack (agenda: `www.googleapis.com`,
  `oauth2.googleapis.com`), y `plugin_manager.scoped_http(plugin_id)` arma con ellos el `context.http` de sus tools.
  Antes las tools de un pack recibían una allowlist vacía. `ScopedHttp` acepta `methods`, `data` (form) y `timeout`
  por llamada. Para packs: `GET/POST/PUT/PATCH/DELETE`. Las tools declarativas del tenant siguen limitadas a GET/POST.
- Una tool registrada ya no toma la allowlist de una tool declarativa con el mismo nombre.
- Sin cambios de comportamiento: timeout de 30 s y errores como `PluginError("Calendar API <status>")`. El único
  cambio es que la cancelación desde el panel ahora deja un warning en el log también cuando Google responde 4xx.

### Pedir el link de un cargo ya avisado no rompe la suscripción (2026-10-08)

- El cron genera el cargo con su link (HU-SUB-05). Cuando el cliente pedía el link (`sub_link_pago`), se creaba otro
  en la pasarela con la misma referencia `sub-<cargo>` y el insert chocaba con el índice único: link huérfano en
  Wompi y una excepción que el loop trataba como falla del pack ("La herramienta no está disponible"). Ahora
  `charge_link` reusa el link del cargo y `create_link` de pagos es idempotente por referencia (mismo monto → el link
  existente; otro monto → rechazo de negocio). Encontrado por los journeys de HU-SUB-12.

### Journeys: conversaciones completas con pagos y reloj (2026-10-08)

- HU-SUB-12. `evals/journeys.py` corre conversaciones completas contra el loop real (`process_conversation`),
  con la pasarela simulada a nivel de proveedor (sin red, con el mínimo real), eventos (`payment.confirmed`), saltos
  de reloj y jobs de los packs, y verifica herramientas (desde `audit_log`), estados (consultas por negocio con RLS)
  y mensajes. Formato en `evals/journeys/README.md`; reutilizable por otros sets (`--set`).
- Set `suscripciones` (8 journeys): informar planes, comprar Free, comprar Basic y pagar, cargo del período
  siguiente, mora con recordatorio y reactivación, pausa, cancelación y cambio de plan.
- CI: gate con `01,05` (umbral 100 %). Nightly: set completo con umbral 95 % y reporte JSON como artefacto. La
  corrida se guarda en `eval_runs` (`journeys:<set>@<versión del spec>`).

### Cambio de plan con precio del servidor, prorrateo y downgrade programado (2026-10-08)

- HU-SUB-07 (ADR-0111). `sub_cambiar_plan` solo recibe el SKU del plan nuevo: precio, moneda y periodicidad salen
  del catálogo del negocio (`plan_catalog_registry`, que commerce registra). Antes el LLM pasaba el precio y un
  journey vio un plan quedar en $0.
- Upgrade: cargo `cambio_plan` por la diferencia prorrateada por días (desde Free, el precio completo y un período
  nuevo), con link de pago en línea o pendiente para transferencia; el plan cambia al confirmarse el pago.
  Downgrade: programado para el próximo período, sin reembolso (`generar_cargos` lo aplica). Migración
  `pack_subscriptions_0006` (`tipo`/`cambio_plan` en cargos, `referencia_origen`/`cambio_programado` en suscripciones).
- La suscripción guarda la referencia del pedido (índice único por negocio): el mismo pedido dos veces no crea dos
  suscripciones. `SubscriptionConfig.varias_por_cliente` (apagado por defecto): si está apagado, commerce no crea un
  pedido de otro plan pago para un cliente con suscripción paga y le indica al asistente que ofrezca el cambio.
- Los jobs de período y `charge_link` ignoran los cargos de cambio.
- `crm_amount_check` reconoce `monto`, `valor_periodo` y `amount` como montos verificados (antes bloqueaba y
  escalaba al mencionar el valor de una suscripción o la diferencia de un cambio de plan).

### Demo agendada de punta a punta (2026-10-08)

- HU-SH-02. Sin doble reserva: restricción de exclusión (`btree_gist`) sobre las citas internas programadas de un
  negocio (migración `pack_calendar` `0004`); si dos prospectos eligen el mismo horario a la vez, uno queda y el otro
  recibe alternativas (también al reagendar y desde el panel, `horario_ocupado`).
- Confirmación con fecha, hora, zona horaria y enlace de la reunión (`AppointmentType.enlace_reunion` o el general
  `CalendarConfig.enlace_reunion`; texto `confirmacion_texto`).
- Recordatorios al cliente a 24 h y 1 h (`recordatorios_horas`, cron `recordatorios_de_citas` cada 15 min, una vez cada
  uno) por el outbox: plantilla si la ventana está cerrada (`appointment.reminder`).
- Aviso al equipo de cada cita nueva, reagendada o cancelada (`appointment.changed`): push al responsable del tipo de
  cita (`responsable_admin_id`, que la cita hereda en `asignado_admin_id`) o a los dueños.
- Asistencia: `POST /pack/calendar/appointments/{id}/asistencia` marca `asistio`/`no_asistio` una cita pasada; al
  `no_asistio` se le ofrece reagendar al cliente (`appointment.no_show`).
- Panel en modo interno: crear una cita con `customer_id` le avisa al cliente (`appointment.confirmed`) y al equipo.
  `PUT /pack/calendar/types/{id}` edita responsable y enlace.

### Diagnóstico de Meta que dice la verdad (2026-10-08)

- HU-CONN-04. `GET /api/v1/admin/meta/health` devuelve `checks` (solo los checks, en orden y con su
  `repair_action`) y `billing_manual_status`. `health` guarda también datos del alta (`coexistence`,
  `token_scope_check`, `webhook_subscription_active`) y el panel los listaba como chequeos.
- Marcar la facturación a mano guardaba solo `manual_status`: el check `billing` desaparecía de la lista hasta
  el próximo diagnóstico y su detalle quedaba viejo. Ahora se rearma con el estado nuevo.
- La vista de salud ya no lista conexiones eliminadas.
- Test de integración del "sin verde falso": una conexión `connected` con el token rechazado por Meta pasa a
  `error` con el motivo y el check queda primero con `reauthorize`.

### Ciclo de vida seguro de las conexiones de Meta (2026-10-08)

- HU-CONN-03, ADR-0122. **Desactivar pausa de verdad.** Lo que entra se guarda sin IA y se avisa a los dueños con un
  push al día. No sale nada por la conexión, y lo que estaba en cola falla sin escalar. Una BYOK pausada ya no da 401:
  su entrada se verifica y se guarda.
- **Sin respaldo silencioso.** Si la conexión de la conversación no puede enviar, la respuesta no sale desde la
  conexión primaria ni desde el número de la plataforma.
- **Activar** solo reactiva una conexión pausada, y solo con el diagnóstico verificado en el acto.
- **Eliminar sin perder historia.** Migración 0063: números y WABAs con `deleted_at`, único parcial del número más un
  único por negocio, y plantillas únicas por negocio. Plantillas y `pricing_daily` ya no se borran en cascada. Los
  tokens se revocan, y en Embedded Signup se desuscribe la app de la plataforma de la WABA. Reconectar el mismo
  número o la misma WABA revive las filas.
- Ninguna acción del panel opera sobre una conexión eliminada.
- Los webhooks de plantillas buscan por negocio en vez de usar el resolvedor global con `LIMIT 1`.

### Evals de consistencia: pass^k, cliente simulado y aserciones de estado (2026-10-08)

- HU-ARCH-28 (ADR-0119, ahora aceptado). `evals/runner.py` y `evals/journeys.py` aceptan `--k`: cada escenario
  corre k veces y se reportan pass@1, pass^k y los escenarios inestables, con lo que cambió entre los intentos
  que pasaron y los que fallaron (herramientas que faltaron o aparecieron y el motivo). El gate sigue midiendo
  pass@1, así que con k = 1 todo queda como antes; el nightly corre k = 4.
- Paso `simulado` en los journeys: un LLM hace de cliente con objetivo, datos, estilo y semilla (prompt en
  `evals/simulador/cliente.md`) y conversa con el loop real hasta cumplir el objetivo, pedir relevo o llegar al tope
  de turnos. Dos journeys nuevos: comprar el Free y comprar el Basic con aserciones sobre el estado final.
- Migración 0058: `eval_runs` y `eval_results` guardan `k`, `pass_1`, `pass_k`, `inestable`, la traza del
  simulador y cada aserción de estado con esperado y real (`verifier` queda listo para HU-ARCH-29).

### Un cliente no puede tocar la cotización ni el pedido de otro (2026-10-08)

- Seguridad (IDOR dentro del tenant, auditor-multitenant en HU-SUB-07). `confirmar_cotizacion`, `crear_pedido` y
  `aplicar_promocion` buscaban la cotización solo por `quote_id` + `tenant_id`, y `agregar_item_pedido` y
  `confirmar_pago_pedido` el pedido solo por `order_id` + `tenant_id`: un cliente que conociera el UUID de otro
  cliente del mismo negocio podía confirmarla, aplicarle una promoción, convertirla en pedido o sacar el link de pago.
  Ahora también filtran por el `customer_id` de la conversación; la ajena responde "no encontrada", igual que una
  inexistente. Tests en `tests/integration/test_pack_commerce_idor_cliente.py`.

### CI por niveles: las PRs a develop esperan minutos, no media hora (2026-10-08)

- ADR-0120. Las PRs a develop corren un nivel rápido:
  - lint, tipos, capas, unitarios y gitleaks sobre los commits de la PR;
  - si la PR toca código que cubre la integración, solo el subconjunto crítico de integración (`-m aislamiento`: RLS, aislamiento entre tenants, webhooks, IDOR).
- La integración completa, el gate de evals y los journeys corren en el push a develop y en todo lo que va hacia main. Hacia main también se exige que `docs/openapi.json` no tenga drift.
- El Postgres del CI ahora es la misma imagen de producción (pg17 + pgvector + PostGIS, publicada en GHCR). Ya no se instala PostGIS en cada corrida.
- Los services usan puertos dinámicos, así que se puede sumar un 2º runner en el mismo host (`docs/WORKFLOW.md`).
- La instalación usa `uv` con caché persistente en el runner. `scripts/install_plugins.sh` respeta `$PIP`.
- No se despliega a test hasta que el CI del push a develop esté en verde (skill `desplegar-test-agent-core`).

### El CHANGELOG se arma con fragmentos (2026-10-08)

- Cada PR agrega su entrada en un archivo propio `changelog.d/AAAA-MM-DD-<tema>.md` en vez de editar la cabeza de
  `CHANGELOG.md`, que hacía chocar a casi todos los PRs abiertos en paralelo. La release los consolida con
  `python scripts/changelog.py --version vX.Y.Z --titulo "…"` (también toma las secciones "Sin versión" que quedan).

### ADRs del Agent OS de procedimientos (2026-10-08)

- ADR-0112 a ADR-0119 en estado **propuesto** (SPEC-26 en `solvershub-docs`, D-27 y D-28):
  Agent OS de procedimientos (responde P-10), contratos `procedure/1`, AgentSpec v3 y KernelSpec,
  estado de la conversación por tareas, conexión canónica, colaboración entre agentes y back-office,
  releases por conexión, habilidades con procedimientos y scope, y fiabilidad medida (pass^k y verificador).
  Solo documentación: no cambia código ni esquema.

### Un preset no mezcla sus ejemplos con un catálogo real (2026-10-08)

- Al aplicar el preset `suscripciones` al negocio `solvershub` de test (HU-SH-01), `aplicar-preset` sembró los
  productos de ejemplo del fragmento: creó «Plan mensual» y le pisó los atributos a `PLAN-FREE`. Ahora
  `apply_preset(..., sembrar_ejemplos=False)` no los carga; `tenant aplicar-preset` no los carga por defecto
  (`--con-ejemplos` para pedirlos) y `tenant create --sin-ejemplos` permite crear un negocio que trae su catálogo.

### El sync de precios guarda lo que devuelve Meta (2026-10-08)

- `pricing_daily` nunca se llenaba: Meta agrupa los puntos de `pricing_analytics` en `data[].data_points[]`, y
  `fetch_pricing_analytics` validaba cada grupo como si fuera un punto, así que los descartaba todos como
  `pricing_analytics_data_point_invalido`. Ahora aplana `data_points` (un elemento sin `data_points` se sigue
  tomando como punto). Visto en test al verificar HU-CONN-01 (2 SERVICE y 3 UTILITY en CO, descartados).

### La salud del número usa su estado de conexión (2026-10-08)

- `number_registered` fallaba con `code_verification_status = NOT_VERIFIED`, así que un número de prueba de Meta (o
  uno registrado sin código SMS) dejaba la conexión en `degraded` aunque enviara bien (verificado en test con
  `+1 555-191-6775`). Ahora decide el `status` del número en la Cloud API: `CONNECTED` pasa; cualquier otro
  (`DISCONNECTED`, `BANNED`, `RESTRICTED`, `FLAGGED`…) falla. Sin `status` se mantiene la regla anterior.

### Asistente de SolversHub reproducible (2026-10-08)

- HU-SH-01. Preset genérico `suscripciones` (commerce + calendar con agenda interna + payments + subscriptions,
  plan mínimo premium) para negocios que venden planes recurrentes. El manifiesto acepta `proveedor_suscripciones`
  y `apply_preset` lo guarda (HU-SUB-09).
- Una sola fuente de precios: una variante con `attributes.precio_de_plan` toma su precio de `core/billing/plans.py`
  al sembrar, y el cron `sincronizar_precios_de_plan` (cada hora) lo alinea tras un deploy con precios nuevos.
- CLI: `agent-core tenant aplicar-preset <slug> <preset>` para negocios existentes y
  `agent-core tenant seed <slug> --voz-de-marca` para reemplazar la voz de marca por la del archivo.

### Los logs ya no muestran URLs con secretos (2026-10-08)

- **Seguridad.** `httpx` registra cada petición a nivel INFO con la URL completa, y el intercambio `client_credentials`
  de Graph manda el App Secret en la query (`/oauth/access_token?client_id=…&client_secret=…`). El worker de Celery
  no pasaba por `configure_logging`, así que esa línea salía en claro en los logs del worker de test. Ahora `httpx` y
  `httpcore` quedan en WARNING en la API, el worker y el beat (señales `after_setup_logger`/`after_setup_task_logger`).
  Afecta también a `fb_exchange_token` y al intercambio de código de Embedded Signup, que llevan secretos en la query.

### El beat agenda los crons de los packs (2026-10-08)

- Ningún cron de pack se despachaba: Celery crea el planificador del beat (copiando `beat_schedule`) antes de emitir
  `beat_init`, que es donde se cargan los packs y corren sus `@register_cron`. El handler ahora vuelve a pasarle el
  `beat_schedule` al planificador. Afectaba a suscripciones (`generar_cargos`, `marcar_vencidas`, `avisos_de_cobro`),
  cobranza (`marcar_mora`, `cierre_diario`, `generar_cargos_suscripcion`) y commerce (follow-ups,
  `reintentar_activaciones`). Visto al verificar HU-SUB-09 en test.

### La respuesta sale por donde escribió el cliente (2026-10-08)

- Un cliente que había escrito por WhatsApp Web (QR) le escribió al número de Meta del mismo negocio: el mensaje
  entró a la conversación abierta, que quedó con las dos conexiones (`connection_id` de WhatsApp Web y
  `meta_connection_id`), y `resolve_credentials` prioriza `connection_id`. La respuesta salía por WhatsApp Web
  (sesión caída: `client is nil`) y el cliente nunca la recibía. Ahora la conversación queda con la ruta de la
  última entrada y nada más; si la entrada no identifica ninguna (número de la plataforma), se conserva la anterior.

### Comercio agnóstico al proveedor de suscripciones (2026-10-08)

- HU-SUB-09. Commerce ya no nombra `pack_subscriptions`: el core resuelve el proveedor del negocio
  (`capability_del_negocio`) con la elección guardada en `tenants.settings["subscriptions"]["provider"]`; sin elección
  y con un solo proveedor habilitado se usa ese. Las tools `sub_*` usan la misma resolución.
- Defecto: `capability_for_tenant` contaba como habilitado cualquier pack activo en el proceso (todos en el worker);
  ahora usa los packs del AgentSpec del negocio (`enabled_for_tenant`).
- `GET/PUT /api/v1/admin/settings/subscription-provider`: estado (`ok`, `sin_proveedor`, `ambiguo`, `invalido`), el
  motivo y la elección entre los habilitados.
- Si al pagarse un plan no hay un proveedor claro, el pedido queda pagado y la activación `pendiente` con el motivo
  (migración `crm_0010`: `plan_activation_status`/`plan_activation_error`, visibles en `GET /orders`), aviso a los
  dueños (push + `audit_log`, una vez) y el cron `reintentar_activaciones` (cada 10 min) la retoma.
- Productos: `billing_period` y `trial_days` en el alta/edición; crear un plan recurrente sin ningún proveedor responde
  400 con la habilidad a activar, y una prueba en un plan pago también (D-16).

### Free con prueba y planes pagos sin prueba (2026-10-08)

- HU-SUB-06 (D-16). Solo un plan de $0 tiene prueba: commerce arma el snapshot con `trial_dias = 0` si el plan es
  pago y registra en `audit_log` (`trial_ignorado_en_plan_pago`) la variante que traía prueba; el pack lo vuelve a
  aplicar al crear la suscripción y la API manual responde 422 si se pide prueba en un plan pago.
- Un pedido de Free nace pagado (sin link ni cobro) y activa la suscripción en el acto (`trialing`, 30 días). Un
  plan por contra entrega ya no nace pagado: espera el pago real.
- Comprar un plan pago estando en Free cambia la suscripción a ese plan (`activa`, período pagado); antes se
  devolvía la Free sin tocar. Toda activación emite `subscription.activated`.
- Fin de la prueba: `avisos_de_cobro` ofrece un plan pago `dias_aviso_fin_prueba` antes (3 por defecto, una vez,
  `subscription.trial_ending`, texto `aviso_fin_prueba_texto` o plantilla mapeada) y `marcar_vencidas` pasa a
  `vencida` la Free que terminó la prueba (`subscription.past_due`, `motivo = "fin_de_prueba"`), sin generar cargos.
- El seed del catálogo respeta `trial_dias: 0` (antes un 0 no actualizaba la variante existente).

### Cobro y mora con avisos por plantilla (2026-10-08)

- HU-SUB-05. El cargo se genera `dias_anticipacion` antes del vencimiento (3 por defecto) y guarda su link
  (`link_url`, migración `pack_subscriptions_0005`); el aviso al cliente lleva el texto del pack
  (`aviso_cargo_texto`, editable) o la plantilla mapeada si la ventana está cerrada.
- Cron `avisos_de_cobro` (09:00 Bogotá): recordatorios en `recordatorios_dias` (−3, 0, +3, +7), una sola vez cada
  uno (`subscription.charge_reminder`, dedupe por cargo y día), nunca si el cargo está pagado; y pausa por mora si
  `pausar_tras_dias_mora` > 0 (`subscription.paused`, `motivo_pausa = "mora"`, push a los dueños + `audit_log`).
  Pagar reactiva una pausada por mora, no una que pausó el cliente.
- El core avisa por los tres eventos (`charge_created`, `charge_reminder`, `paused`) con su clave de idempotencia y
  el texto de respaldo del pack; el catálogo de plantillas por evento los incluye.
- `GET /api/v1/pack/pack_subscriptions/cargos/{id}/avisos`: qué se envió por cada cargo, cuándo y con qué resultado.
- `docs/plantillas-suscripciones.md`: plantillas de utilidad sugeridas por evento.

### Los push de la app usan el canal de Android de la app (2026-10-08)

- Los mensajes a Expo no llevaban `channelId`, así que Android los mostraba por el canal de respaldo de FCM
  (`fcm_fallback_notification_channel`), sin la importancia ni el sonido que configura la app. Visto al verificar
  HU-APP-05 en el OPPO. Ahora van con `channelId: "solvershub-general"` (`CANAL_DEFAULT` de la app).

### Ciclo de vida correcto de la suscripción (2026-10-08)

- HU-SUB-04. El período sale del calendario (`periodicidad` mensual/trimestral/anual + `dia_ancla`, migración
  `pack_subscriptions_0004`), no `+30 días`: un anual del 2026-10-15 vence el 2027-10-15 y un mensual del 31 de enero
  el último día de febrero.
- `generar_cargos` ya no pone `activa` sin pago, no cobra durante la prueba ni planes en $0 y no acumula cargos de una
  `vencida`. Nuevo cron `marcar_vencidas`: tras `SubscriptionConfig.dias_gracia` (5 por defecto) pasa a `vencida` y
  emite `subscription.past_due` por el outbox.
- `payment.confirmed` valida el monto contra el cargo (si difiere: no se marca pagado, push a los dueños y
  `audit_log`) y reactiva una `vencida` calculando el período siguiente desde el pagado.
- `sub_link_pago` no cobra un período futuro ni durante una prueba (`en_prueba`, `ya_pagado`).
- **Todos los crons de packs se salteaban en el worker** (`_active` vacío: solo la API activaba packs). Ahora
  `register_cron` llama `ensure_process_activation()` antes de chequear.

### Presets de negocio (2026-10-08)

- HU-FWK-03 / ADR-0110. `core/presets/` con los manifiestos `comercio`, `restaurante` y `cobranza`, y
  `apply_preset(tenant_id, preset)`: valida plan y cuota, habilita los packs completando su config sin pisar lo del
  negocio, deja **pendientes** los que exigen claves (`pack_payments`), siembra con `seed_data` y registra
  `tenants.settings["preset"]` + `audit_log`. Idempotente.
- Fragmentos por pack en `packages/pack-x/presets/<vertical>.json` (config sin secretos + `seed`); commerce trae
  catálogo genérico y menú de restaurante.
- `pack_commerce.seed_data` siembra el catálogo (antes vivía en el CLI). `cli/commands/tenant.py` ya no importa packs.
- `agent-core tenant create --preset <id> --plan <plan>` y `agent-core tenant presets`. El alta con `--packs` ya no
  falla con un pack que exige claves: lo marca pendiente.

### `flow validate` y los evals ven las tools del core como el runtime (2026-10-08)

- `agent-core flow validate` daba falso negativo con las `sub_*` del core (HU-SUB-01): ahora las cuenta si el negocio
  tiene habilitado un pack proveedor de suscripciones, igual que su predicado en runtime.
- El runner de evals no registraba las skills del core (solo lo hace `core/main.py` al importar), así que los golden
  sets corrían sin `escalate_to_human`, memoria ni `sub_*`. Ahora las registra y, como el loop (ADR-0109), suma las
  `always_available` cuyo predicado pasa. Gate del CI 9/9 y golden de `solvershub` 12/12 en local.

### `crm_0009` no trunca el número de pedido al migrar (2026-10-08)

- El backfill de `public_order_number` repetía `'OC-' || lpad(order_seq::text, 4, '0')`, y `lpad` **trunca** en
  Postgres: los pedidos 83790..83799 quedaban todos `OC-8379` y el índice único `(tenant_id, public_order_number)`
  abortaba la migración (visto en la base local: seq 837966..837971). Ahora rellena hasta 4 dígitos sin cortar
  (`OC-0042`, `OC-837966`). Donde `crm_0009` ya corrió sin pedidos ≥ 10.000 (test) el resultado es el mismo.

### Composición de flujos multi-pack (2026-10-08)

- HU-FWK-01 / ADR-0109 (enmienda ADR-0054). El flujo efectivo se compone en cada turno: `core/flow.json`
  (`core:saludo`, `core:escalamiento`) + las secciones de todos los packs habilitados (`pack_x:id`) + las secciones
  propias del negocio. El `flow.json` del negocio deja de ser autoritativo y el alta ya no copia secciones de packs.
- Las tools del core (`CoreSkill` y las `sub_*`) son `always_available`: el loop las ofrece aunque el flujo no las
  nombre. Antes, con flujo declarado, quedaban ocultas memoria, perfil, correo y suscripciones.
- `saludo` de commerce y cobranza ya no chocan (pasan a `descubrimiento` y `verificacion`); antes un negocio con
  ambos perdía `crear_lead`, `consultar_inventario`…
- `agent-core flow migrate <slug> [--apply]`: quita copias viejas, convierte en propias las secciones que su pack no
  declara y avisa de tools que ningún flujo declara.

### La sincronización de plantillas acepta `quality_score` de Graph v26 (2026-10-08)

- Graph v26 devuelve `quality_score` como objeto (`{"score", "date"}`) y `sync_templates` lo insertaba en una columna de
  texto: el INSERT fallaba y ninguna plantilla se sincronizaba (visto en test al preparar la verificación de
  HU-ARCH-13). Ahora se guarda el `score`.

### Plantillas de WhatsApp por evento fuera de la ventana de 24 h (2026-10-08)

- HU-ARCH-13 / ADR-0108. Cada negocio mapea evento → plantilla aprobada (`app_settings.message_templates_map`,
  migración `0057`). Con la ventana abierta sale el texto de respaldo; cerrada, la plantilla aprobada de la WABA de la
  conexión (`send_template`); sin plantilla, el mensaje queda `failed` con `sin_plantilla` y el panel lo ve
  (`/admin/settings/message-templates/missing`). Variables solo de rutas permitidas del evento, nunca del LLM.
- `subscription.charge_created` sale por el outbox desde `generar_cargos` y lo envía un handler del core.
- El 409 `window_expired` del operador vuelve a salir (el sender se tragaba el error y el router respondía 204), con
  `GET .../templates` y `POST .../template-messages` para responder con una plantilla.
- Una plantilla nunca usa el número de la plataforma (`.env`) y sus parámetros se limpian (`clean_param`).

### El bloqueo de login usa la IP real del cliente (2026-10-08)

- `client_ip` confiaba en cualquier `X-Forwarded-For`: un cliente que hablara directo con la API podía mandar una IP
  distinta por intento (esquivar el bloqueo por IP) o la de otro (bloquearlo). Ahora las cabeceras de proxy solo cuentan
  si la conexión llega desde `TRUSTED_PROXY_CIDRS` (por defecto, redes privadas y loopback) y la IP es la primera no
  confiable de `X-Forwarded-For` leída de derecha a izquierda, con `CF-Connecting-IP` como respaldo.
- El BFF del panel llamaba a la API sin la IP del usuario, así que 5 fallos de cualquiera bloqueaban el login del panel
  para todos durante 15 minutos (visto en test el 2026-10-08). El panel ahora reenvía la cadena (solvershub-panel).

### El worker registra los suscriptores de los packs (2026-10-08)

- El despachador del outbox (ADR-0107) corre en el worker, que solo hacía `plugin_manager.scan()`: `emit_strict` corría
  sin handlers y el evento quedaba `entregado` sin efecto. Ahora `ensure_process_activation()` corre `on_activate`
  (`activate(register_only=True)`: sin migraciones ni escrituras de estado) una vez por proceso hijo, y el despachador
  avisa `domain_event_sin_handlers`. Verificado en test: un `payment.confirmed` dejó el pedido `pagado` (#275).

### `rls_tests_pass` deja de correr tests en el servidor (2026-10-08)

- El check de producción de Meta `rls_tests_pass` corría `pytest tests/integration` dentro del proceso. La imagen
  desplegada no trae pytest ni `tests/`, así que en test y producción siempre daba `exit=1`; y si los trajera, esos tests
  crean negocios en la base configurada (ensuciarían datos reales). Ahora es informativo (`ok=None`): los tests de
  aislamiento corren en CI (`build-and-test`) y el detalle muestra el commit desplegado (`SOURCE_COMMIT`/`COOLIFY_BRANCH`
  de Coolify). El aislamiento en runtime lo sigue cubriendo `rls_forced`.

### Google Calendar se conecta solo al negocio que inició el flujo (2026-10-08)

- El `state` del OAuth de Google Calendar ahora lleva el negocio que lo inició (`<tenant_id>.<aleatorio>`). El callback
  guardaba la cuenta en el negocio de la sesión al volver, que puede ser otro: el usuario cambió de negocio en otra
  pestaña mientras estaba en Google, o la app abrió el panel con la sesión del navegador en otro negocio (smoke de la
  APK, 2026-10-07). Si no coincide, no se intercambia el código y se vuelve con `?google=otro_negocio`.

### Una tool que pide relevo humano escala (2026-10-08)

- `pack_commerce` devolvía `{"error": ..., "escalate": True}` con un pedido por encima de `monto_alto_threshold`, pero
  ningún punto del core leía la clave: el pedido no pasaba a un humano y el LLM veía un error genérico. Ahora el loop,
  ante un resultado de tool con `"escalate": True` (o un `BusinessError(escalate=True)`), escala con el mecanismo
  nativo (`plugin:<tool>`, con el error como resumen: pausa el bot, push a los dueños), no ejecuta el resto del lote y
  cierra el turno con la respuesta de escalamiento. Solo cuenta `True` literal, y solo de tools registradas (pack/core):
  una tool declarativa devuelve datos de una API externa y su `escalate` se descarta (invariante 7).

### Un reintento BYOK fallido no se queda con el número (2026-10-08)

- `save_byok_connection` reasignaba el número a la conexión del intento antes de guardar tokens y suscribir; si eso
  fallaba, solo la marcaba `error`: se quedaba con el número y, con un `connected_at` más nuevo, podía ganarle a la sana
  en `_resolve_byok_token` (inbound en 401). Ahora `_rollback_failed_alta` le devuelve el número a su conexión anterior
  (si sigue viva y es del negocio) y, si la fila se creó en ese intento, la da de baja (borrado lógico) y libera sus
  números y WABAs: deja de prestar su secreto y de contar como suscrita. Una conexión reusada (misma app) no se borra.
- `_token_expiry` ya no aborta el alta por un error de red (`httpx.HTTPError`): es best-effort.
- Una petición cancelada también deshace el alta (`asyncio.CancelledError`, rollback con `shield`), y una conexión
  reusada recupera su `origin` anterior. `resolve_app_credentials` (preflight) ya no elige una conexión borrada.

### Números del portafolio de Meta (2026-10-07)

- **Nuevo:** `GET /api/v1/admin/meta/connections/{id}/business-numbers` lista, solo lectura, los números de todas las
  WABAs propias del portafolio de Meta dueño de la WABA de la conexión: el límite de números de Meta cuenta el portafolio
  entero. Visto en test: "Se superó el límite de números de teléfono por negocio" al agregar un número BYOK.
  - Solo BYOK recorre el portafolio (token del propio negocio). Con Embedded Signup y `manual` el token es de la
    plataforma y se lista solo la WABA de la conexión.
  - Nunca las WABAs de clientes (`client_whatsapp_business_accounts`); tope de 50 WABAs; no guarda nada en
    `phone_numbers`.
  - Los números que ya tiene otro negocio de la plataforma cuentan (`other_tenants_count`) pero no se muestran.
  - Lo que falla por partes (permiso `business_management`, una WABA) va en `warnings`.

### El motivo de Meta en los errores de números (2026-10-07)

- Los errores de agregar, pedir y verificar código, registrar y sincronizar números muestran el motivo que da Meta
  (`error_user_title`/`error_user_msg`) en vez de solo "Error de Meta: Invalid parameter". Visto en test al agregar un
  número BYOK: el código 100 no decía si el número ya estaba en otra cuenta o si el nombre no se permitía.
- `graph_api_calls.error_message` guarda también ese motivo (sin cambio de esquema).

### Registrar un número de una conexión BYOK o manual (2026-10-07)

- **Nuevo:** `POST /api/v1/admin/meta/connections/{id}/numbers/{phone_number_id}/register` registra el número en la
  Cloud API con un PIN de verificación en dos pasos (`POST /{phone_number_id}/register` de Meta). Antes solo lo hacía el
  wizard de Embedded Signup; un número agregado a una conexión BYOK o manual había que registrarlo a mano en Meta.
  - El PIN es opcional: si el número ya tiene verificación en dos pasos, se manda ese; si no, se usa el guardado (por
    ejemplo, el que fijó el wizard) y, si no hay, se genera uno. Se guarda cifrado antes de llamar a Meta y nunca se
    devuelve (en el cuerpo es `SecretStr`). Si Meta lo rechaza (4xx) vuelve el PIN que había: un PIN errado no
    borra el bueno; sin respuesta o con un 5xx se queda el nuevo, por si el registro se aplicó.
  - Solo números de esa conexión y de ese negocio; un intento por minuto por número (cada PIN errado suma hacia el
    bloqueo de Meta). En coexistencia con la app de WhatsApp Business no se registra, igual que en el wizard.
- `GET .../numbers` devuelve `registered` por número, para que el panel muestre el botón "Registrar".
- **Seguridad:** pedir y verificar el código de un número ahora también exigen que el número sea de esa conexión y de
  ese negocio (antes el `phone_number_id` iba directo a Meta con el token de la conexión). Sentry tapa `pin`,
  `new_pin`, `previous_pin`, `app_secret`, `access_token` y `verify_token` en el cuerpo del request y en las variables
  de cada frame.

### Eventos de dominio durables con outbox (ADR-0107, 2026-10-07)

- **HU-SUB-03:** los eventos de dominio (`payment.confirmed`/`payment.rejected`) se escriben en la tabla
  `domain_events` **en la misma transacción** que el cambio de estado del pago (webhook de Wompi), con
  `dedupe_key = payment:{provider_reference}:{status}` idempotente. Si la transacción se revierte, el
  evento no existe.
- Un despachador de Celery (beat 30 s) entrega los eventos pendientes bajo el `tenant_scope` de su tenant
  con backoff exponencial (30 s→10 m) y, tras 5 intentos, los deja `fallido` con su error — visibles en la
  "Bandeja de fallos" y reintentables a mano (`/api/v1/admin/domain-events`).
- El bus in-process queda para eventos del core; el despachador usa `EventBus.emit_strict` (levanta si un
  handler falla). `pack_cobranza` migra a `host.subscribe` (des-suscripción al desactivar el pack).

### Numeración única y checkout por tipo de producto (HU-COM-03, 2026-10-07)

- **Número de pedido (D4):** `public_order_number` deja de ser columna computada con prefijo fijo `OC-`. Lo arma
  `crear_pedido` con el prefijo del negocio (`order_prefix`, 1-8 alfanuméricos, fallback `OC`), sin truncar a
  partir del pedido 10.000, y un índice único `(tenant_id, public_order_number)` lo respalda (migración
  `crm_0009`, con backfill que conserva los números existentes). `consultar_estado_pedido` acepta el prefijo del
  negocio.
- **Checkout por tipo (D17):** `checkout_fields` configurables por tipo de entrega y de producto: los **planes
  recurrentes** y el **retiro en tienda** no piden dirección. `crear_pedido` responde
  `status: faltan_datos_cliente` con `missing_fields` y `existing_data` (captura progresiva, uno a uno) y solo
  guarda los datos en el perfil del cliente si el cliente aceptó (`guardar_datos_cliente`, Ley 1581).

### Suscripciones v1: contrato único y tools del core (ADR-0106, 2026-10-07)

- **HU-SUB-01:** `core/subscriptions` fija el contrato de 8 operaciones (`activate`, `get`, `list_for_customer`,
  `pause`, `resume`, `cancel`, `change_plan`, `charge_link`) con `PlanSnapshot`/`SubscriptionView`/`ChargeLink` y
  `capability_for_tenant` (proveedor del negocio; dos habilitados sin elección → `proveedor_ambiguo`).
- Las tools que ve el LLM pasan al core: `sub_consultar`, `sub_link_pago`, `sub_pausar`, `sub_cancelar` y
  `sub_cambiar_plan` (ofrecidas solo con proveedor habilitado); las `sub_*_suscripcion` del pack se retiran.
- `pack_subscriptions` implementa el contrato como proveedor de referencia; `charge_link` nunca crea períodos
  futuros (`periodo_futuro`) y los rechazos de negocio salen como `SubscriptionError` (`monto_bajo_minimo`,
  `sin_pasarela`).

### Bloqueo progresivo de inicio de sesión (ADR-0105, 2026-10-07)

- **HU-ARCH-04:** 5 intentos fallidos del mismo identificador (o IP) en 10 minutos → rechazo de 15 minutos con
  mensaje neutro (`429` `auth_bloqueado`), en `/api/v1/auth/login` y en `/auth/gestor/login` (scope `gestor:<slug>`,
  con aislamiento por negocio y `audit_log`). Estado en Redis con TTL y fail-open si Redis no responde.

### Una política anti-baneo de `waweb` no escala ni pausa el bot (ADR-0103, 2026-10-07)

- **Bug (alto, preexistente):** el tope diario o la pausa de WhatsApp Web le llegaban al worker como un fallo
  transitorio de envío: tras 3 reintentos el mensaje se marcaba `failed` y la conversación quedaba escalada con el
  bot pausado (`send_failed`), incluso después de reiniciarse el tope (issue #231, incidente del 2026-10-05).
- Ahora el connector distingue el rechazo anti-baneo (`reason: policy`), el worker lo clasifica como `POLICY` y
  **reprograma** el mensaje —sin marcarlo `failed` ni escalar— para después del reinicio del tope (00:02 UTC) o
  `+15 min` si la pausa es manual. Tras 24 h de bloqueo deja constancia en la auditoría del negocio.

### El autoescalado por turnos sin resolver no cuenta cortesías ni preguntas (ADR-0104, 2026-10-07)

- **Bug (medio, preexistente):** `resolved=bool(all_tool_results)` contaba como "sin resolver" cualquier turno sin
  tools: tres "Hola" seguidos —o un ciclo de preguntas pidiendo un dato— escalaban la conversación y pausaban el
  bot (issue #234, incidente del 2026-10-05).
- Ahora un turno sin tools no suma al contador si el mensaje del cliente es una cortesía suelta o si el asistente
  pregunta para avanzar; el contador queda como estaba (no reinicia). El fallo neutral de guardrail sigue contando
  (ADR-0092).

### Una conexión de Meta en `error` o `degraded` se recupera sola (ADR-0102, 2026-10-06)

- **Bug (medio, preexistente):** una conexión en `error` no volvía nunca a `connected`:
  - el cron de salud no la revisaba;
  - el de suscripciones dejaba de mirar la app de una conexión degradada;
  - el sync de plantillas se saltaba las `degraded`.

  Ahora el cron de salud y el de suscripciones revisan `connected`, `degraded` y `error`, tengan o no números. El sync
  de plantillas suma `degraded`.
- **Bug (alto, preexistente):** sin credenciales, el diagnóstico daba `connected`. "Diagnosticar" un alta BYOK
  fallida la pasaba a `connected` y, si era más nueva, le quitaba el secreto a la conexión sana: todo el inbound del
  negocio daba 401.
  - Ahora una conexión solo mejora de estado con el token verificado y, en BYOK, con su app suscrita: la suscripción
    de la app confirmada y la WABA suscrita a esa app.
  - Las filas que ya estaban promovidas bajan a `error` en la siguiente corrida: una BYOK sin credenciales propias
    falla `token_valid`, y una con la WABA sin suscribir a su app falla `own_app_subscribed`.
  - El alta BYOK marca la WABA como no suscrita hasta confirmar la suscripción, también al reconectar.
- El cron de suscripciones prueba con otra conexión de la app si la representativa falla, así un negocio que cargó el
  `app_id` de otro no le traba la recuperación al dueño. Ya no revisa la app de la plataforma, que una BYOK con su
  `app_id` dejaba "sin dato" para todos los negocios de Embedded Signup.
- El checklist de producción ignora las conexiones desactivadas o borradas.
- Las alertas de token (inválido o vencido, por vencer) y de calidad RED salen una vez al día por conexión, no en cada
  corrida ni una por cron. El wizard manual ya no manda una alerta crítica por un token mal pegado.
- El diagnóstico ya no borra `coexistence`, los datos del alta ni el estado de facturación que marca un admin, y el
  panel no los dibuja como checks. Se descarta si el wizard reconectó la conexión mientras corría.

### Una conexión BYOK degradada sigue recibiendo mensajes (ADR-0101, 2026-10-06)

- **Bug (alto, preexistente):** cuando el cron de salud pasaba una conexión BYOK a `degraded` o `error`,
  `_resolve_byok_token` dejaba de leer su App Secret y su `verify_token`. Todo el inbound del negocio daba 401
  (por slug y por el genérico).
  Ahora esos estados siguen prestando la credencial, solo como respaldo: primero `connected`/`pending`, después
  `degraded`, al final `error`, y primero las conexiones `origin = 'byok'`. `revoked` (desactivada a mano o borrada) y
  `draft` no prestan nada. El caso más común era un token a menos de 7 días de vencer, que ya pasa a `error`.
- "Diagnosticar" y el cron de salud ya no reactivan una conexión desactivada.
- El cron de vencimiento de tokens también avisa por las conexiones en `error` que todavía tienen números.

### Una ráfaga de mensajes no parte la conversación en dos (2026-10-06)

- **Bug (preexistente):** el consumidor procesa cada mensaje del batch en su propia tarea, y
  `_resolve_conversation` buscaba la conversación abierta del cliente y, si no había, la creaba sin candado.
  Varios mensajes seguidos de un cliente sin conversación abierta podían crear una conversación cada uno, y el
  agente los atendía en turnos separados, sin el contexto de los otros. Ahora el get-or-create se serializa por
  cliente con `pg_advisory_xact_lock` hasta el commit, como `meta_asset_claims` (ADR-0095). Sin migración.
- Test: `tests/integration/test_consumer_conversacion_concurrente.py` (5 tareas a la vez sobre un cliente sin
  conversación abierta: sin el candado salen 5 conversaciones).

### Un evento del webhook solo actúa sobre un negocio autorizado por su firma (ADR-0100, 2026-10-05)

- **Seguridad (alto, preexistente):** el webhook genérico aceptaba la firma con el secreto de cualquier negocio del
  payload y procesaba todas las entries: un negocio BYOK podía inyectarle mensajes a otro. Ahora cada grupo
  (entry, número) se procesa solo si lo firmó la plataforma (vale para todos) o su propio negocio; si no, queda en
  `webhook_events` como `signature_tenant_mismatch` (`signature_ok=False`) y se alerta. El POST se rutea una sola
  vez (`_rutear_grupos`), con la misma resolución que el despacho.
- Los eventos de plantillas y de estado de mensajes reciben el negocio de su grupo (`expected_tenant_id`) y se
  descartan si la plantilla o el mensaje es de otro: con su propia firma, un negocio ya no puede cambiar una
  plantilla ajena ni el estado o los costos de los mensajes de otro (genérico y por slug). El estado se busca
  dentro del negocio de la entry; `resolve_tenant_for_message` (`LIMIT 1`) queda solo para el log.
- `connections.platform_app_secret` y `resolve_own_app_secret`; `resolve_effective_app_secret` no cambia.
- El cron de salud ya no cuenta los eventos rechazados como "worker caído" (alertaba cada hora para siempre).
- La alerta "Un negocio firmó eventos de otro" sale una vez cada 24 h por par; marcar un evento como procesado
  filtra por su negocio; un payload que no es un objeto JSON se ignora con 200.

### Renumeración de migraciones y ADRs de la app (2026-10-05)

- **CI de `develop` en rojo:** los PRs de la app (HU-APP-02/05/14/15) se mergearon con las migraciones `0051` y
  `0052` que ya usaban HU-CONN-01 y ADR-0095, y `alembic upgrade head` fallaba por varias cabezas. Las de la app
  pasan a ir después de las de Meta: `0053_password_reset_tokens`, `0054_push_tokens` y
  `0055_mobile_audit_events`. Solo cambian los números y el orden: el esquema final es el mismo.
- Los ADRs de la app que repetían número pasan al final: recuperación de contraseña y token de gestor a ADR-0098
  (era 0094) y notificaciones push a ADR-0099 (era 0095). Búsqueda global (0096) y sync (0097) no cambian; los
  cuatro entran al índice de `docs/adr/`.

### Un número o una WABA de Meta pertenecen a un solo negocio (ADR-0095, 2026-10-05)

- **Seguridad (alto, preexistente):** un negocio podía registrar la WABA o el número de otro y recibir sus
  mensajes. El webhook ahora rutea **primero por el número** y después por la WABA. Si el número es de un negocio
  y la WABA de otro, o el número (o la WABA que decide) tiene dos dueños, el evento no va a ninguno: queda
  `tenant_conflict` y nunca cae al negocio del endpoint por slug ni al de desarrollo. Cada grupo
  `(entry, número)` se rutea por separado.
- `resolve_tenant_for_phone` y `resolve_tenant_for_waba` (migración `0052`) solo cuentan conexiones vivas (no
  borradas y fuera de `draft`; `resolve_tenant_for_phone` también mira `phone_numbers`, y en `channel_connections`
  solo WhatsApp por Meta) y devuelven NULL si el activo lo reclaman dos negocios. Dejan de ser ejecutables por
  PUBLIC y fijan `search_path = public, pg_temp`. La migración aborta si el rol no puede saltar RLS, lista en el
  log los activos con dos dueños y los de altas manuales a revisar, y retira (borrado lógico, reversible por la
  marca en `status_reason`) las conexiones en `draft` con filas del Embedded Signup anterior, cerrando su wizard.
  "Activar" ya no saca una conexión de `draft`.
- Función nueva `meta_asset_claimed_by_other_tenant` (`SECURITY DEFINER`, solo `app_runtime`): responde si otro
  negocio ya tiene la WABA o el número, sin decir cuál. Sincronizar o agregar números no guarda un número de otro
  negocio (`meta_numero_de_otro_negocio_no_se_guarda`); sincronizar sigue con los demás.
- **Altas verificadas** (`core/gateway/meta_asset_claims.py`): la manual, BYOK y Embedded Signup comprueban en
  Graph que el token ve la WABA y que el número es de ella, y rechazan (`asset_claimed`) un activo de otro
  negocio, con un candado por activo en la misma transacción del insert. También `onboard_tenant` y
  `save_connection` (tipo `whatsapp_meta`).
- **Embedded Signup:** `POST /api/v1/admin/meta/wizard/exchange` ya no guarda la WABA ni el número antes del canje
  (un `code` inventado los dejaba escritos); se guardan después, verificados con el token nuevo. Un `code`
  inválido responde 400 `meta_error` en vez de 500.

### Credenciales de Meta por conexión (HU-CONN-01, 2026-10-05)

- **`meta_connections.origin`** (`byok` | `embedded_signup` | `manual`, `NOT NULL` sin default, migración
  `0051`): el modo de credenciales vigente de la conexión. Lo fija cada alta (y cambia si el wizard de ES toma
  una conexión BYOK); deja de inferirse por los tokens que existen. Backfill por tenant (fija `app.tenant_id`)
  con la evidencia de cada alta; con tokens de los dos orígenes queda `byok` (el que usaba el sender) y se
  listan en el log; sin evidencia → `manual`. ADR-0094.
- **Una sola regla de qué token usa cada origen**, `core/gateway/meta_credentials.py`: `access_token_refs`,
  `access_token_for`, `access_token_expires_at` y `credentials_for` (`MetaCredentials`). BYOK usa el token y el
  App Secret de la conexión; Embedded Signup y manual, el token de la conexión y la app de la plataforma. Sin
  credenciales → `CredentialsUnavailable(reason)`; sin `tenant_scope` activo → `no_tenant_scope`.
- Números, Plantillas (panel SSR y API), salud, costos/créditos, endpoints de token BYOK y los crons de
  plantillas y de precios usan las credenciales de **esa** conexión. Antes casi todo asumía Embedded Signup:
  fallaba con BYOK y los crons exigían `TOKEN_KIND` **y** `app_access`, así que no procesaban ninguna conexión.
- **El sender y los avisos de vencimiento** (cron diario, resumen de salud, salud del panel SSR e `inspect`)
  usan el token que dice `origin` en vez de "BYOK primero": una conexión BYOK tomada por ES ya no envía con el
  token viejo ni dispara alertas críticas falsas por él (supersede parcial de ADR-0040 §3 y ADR-0041 §5).
- La columna "Origen" de la lista de conexiones sale de `origin` ("BYOK (app propia)", "Embedded Signup",
  "Carga manual"), no de comparar `meta_app_id` con la app de la plataforma.
- `POST /api/v1/admin/meta/manual` valida el token con la app de la plataforma, la misma con la que se resuelve
  (enmienda ADR-0034); sin ella configurada, 400 `platform_app_not_configured`.
- Crons: el error se registra por conexión (`connection_id`, `tenant_id`, `reason`) y no corta a las demás; el de
  plantillas pide las credenciales antes del lock horario, y el lock lleva el tenant en la llave. La
  sincronización manual de precios ahora informa las conexiones sin credenciales (antes las omitía en silencio).
- `POST /admin/meta/byok/token/extend` responde 400 `not_byok` para conexiones que no son BYOK (antes de
  resolver credenciales).
- Se elimina `resolve_waba_and_token` (y su copia en el panel SSR) a favor de `resolve_waba_and_credentials`.

### Conectar Google Calendar desde el panel (HU-CAL-07, 2026-10-05)

- El retorno OAuth de Google entra por el panel: `redirect_uri` = `<PANEL>/api/calendar/oauth/callback`
  (antes una ruta inexistente de la API → `redirect_uri_mismatch`/404). ADR-0093.
- `/oauth/start` genera un `state` anti-CSRF (cookie httponly `oauth_state`); `/oauth/callback` exige
  sesión (`pack_calendar:conexion`), valida el `state`, guarda los tokens en la conexión del negocio
  del principal y borra las cookies del flujo.

### Pack de agendamiento definitivo (HU-CAL-05, 2026-10-05)

- `check_availability`: fecha pasada → `status: in_past` (con `today`); para hoy no
  ofrece horas que ya empezaron; agrega `status`/`today` a la respuesta.
- Tool nueva **`find_next_available`** (`days` 1-14): próximos días con horarios
  libres, sin días cerrados ni sin cupo, máximo 5.
- **Link "agregar a mi calendario"** (plantilla de Google Calendar) en create/update
  del modo interno y en el correo (`{link}`, con línea de respaldo si la plantilla no
  lo trae). Plantillas de correo/WhatsApp con formato seguro.
- **Guardrail con evidencia real** (`status` del turno, D-SPEC-24-8): `rejected`/
  `not_found` ya no respaldan confirmaciones; "moví tu demo" cubierto; las preguntas
  no disparan el guardrail; el correo se respalda con `email_sent` y el reintento
  nunca pide `send_email`.
- **Panel**: mover o cancelar con `notify` avisa por correo y WhatsApp y devuelve
  `notification`; cancelar registra `cancelada`, es idempotente y responde 200.
- `citas.md` y propósitos de `flow.json` definitivos.

### Fecha actual en el prompt y anti-escalamiento (HU-CAL-04, 2026-10-04)

- **Zona horaria del negocio** `app_settings.timezone` (migración `0050`, default
  `America/Bogota`), editable en Configuración → Identidad; zona inválida → 422 y un
  PUT sin el campo no la pisa. El horario de atención la usa si no trae zona propia
  (antes caía a `UTC`). ADR-0091.
- **Fecha y hora actual en el prompt**: última parte del bloque dinámico, solo el dato
  ("Hoy es martes, 6 de octubre de 2026 — 10:00 (America/Bogota)."); la instrucción de
  cómo usarla y la regla de no filtrar los `[SISTEMA]` viven en `reglas-operacion.md`.
- **Respuesta neutral al agotar el guardrail** (`behavior_config.guardrail_fail_action`
  = `escalate` | `neutral`, default `escalate`): con `neutral` responde la sección
  `verifying` de `respuestas.md`, no escala, registra `audit_log` `guardrail_neutral` y
  cuenta el turno como no resuelto (el autoescalado por turnos sigue de red). ADR-0092.
- `guardrail_max_retries` se valida 0-5 en el API, igual que en el loop.

### Playground "Probar asistente" + F0 del canal waweb (2026-09-27)

### Playground (ADR-0075)
- Nueva vista `/admin/asistente/probar`: conversa con el asistente del tenant
  **sin pasar por ningún canal** (mismo loop del runtime, `process_conversation`).
- Botón destacado **"Probar asistente"** (acento indigo propio) en `/admin/conexiones`
  y en el menú lateral; vista tipo celular con aviso de modo prueba.
- Conversación de prueba `channel == "test"`: **no** persiste filas en `messages`,
  queda excluida de las métricas de billing (`clientes`/`conversaciones`/
  `costo_llm_usd`) y de `escalate()` (no crea `Escalation`, no pausa, no alerta).
- Archivos: `core/admin/playground.py`, `core/shared/test_mode.py`,
  `core/admin/templates/asistente_probar.html`, rutas en `core/admin/views.py`,
  filtros en `core/billing/metrics.py` y `core/orchestrator/escalation.py`.

### F0 del canal WhatsApp Web no oficial (ADR-0074, propuesto)
- Spike `scripts/spike_neonize.py` + verificación de instalación.
- **Validado:** `neonize 0.5.2` instala e importa (con `_ctypes` disponible) y
  `NewAClient` instancia. **Hallazgo:** el Python fuente del venv
  (`/usr/local/Python-3.12.0`) se compiló **sin libffi** → `No module named
  '_ctypes'`; el connector necesita un Python con libffi (el del Docker sí lo tiene).
- **Pendiente manual** (requiere número de repuesto): QR, persistencia tras
  reinicio, supervivencia a recrear contenedor, media y reconexión tras caída de red.

### F1 (inicio) — sesión persistente del canal `waweb`
- Migración core **`0039_waweb_sessions`**: tabla `waweb_sessions` (`tenant_id` +
  RLS + FORCE + índice compuesto + único parcial por conexión + `deleted_at`) y
  modelo `WaWebSession` en `core/db/models.py`. Aplicar/revertir validado contra
  el stack local.
- **Tipo de conexión `whatsapp_waweb`** en el catálogo (`connection_types.py`,
  wizard `/admin/conexiones/waweb`): aparece en "Agregar conexión" con su aviso.
- **Política anti-baneo por tenant** (`core/waweb/policy.py`): modo solo-respuesta
  (default), delays y tope diario configurables en `behavior_config`, con clamps.
- **Paquete `packages/channel-waweb`** (entry_point `agent_core.channels` →
  provider `waweb`) con `WaWebInbound`/`WaWebOutbound`; el envío cruza por Redis
  request/reply (`core/waweb/handoff.py`) hacia el futuro proceso `connector`.
- **Vista `/admin/conexiones/waweb`**: aviso de riesgo con aceptación registrada
  (`risk_ack_at/by/version`) y QR por polling (`/qr.json`), a la espera del
  connector.
- **Runtime del connector (handlers reusables)**: `core/waweb/inbound.py`
  (publica el entrante al stream `agent_core:inbound` con dedupe por wamid) y
  `core/waweb/outbound.py` (drena `waweb:outbox` con delay anti-baneo y responde
  el wamid/error). Backend abstracto: el socket Neonize se enchufa después del
  spike.
- **Connector completo (endurecido)**: `store.py` (sesión efímera ↔ blob cifrado
  en `waweb_sessions.creds_encrypted`), `backend.py` + `backend_neonize.py`,
  `manager.py` (FSM, reconexión con backoff + *attempt guard*, persistencia,
  alertas), `registry.py`, `limits.py` (tope diario real + kill-switch) y
  `connector.py` (entrypoint). Rol `connector`: `scripts/entrypoint-connector.sh`
  + servicio `waweb-connector` (perfil `waweb`, tmpfs, **apagado por defecto**).
- **Config**: `WAWEB_ENABLED` (default **false**), `WAWEB_SESSION_DIR`,
  `WAWEB_SWEEP_INTERVAL_S`, `WAWEB_RECONNECT_MAX_S`; extra `waweb` (neonize) en
  `pyproject`. Imagen con `libmagic1`/`ffmpeg` (Neonize).
- **Coexistence (Stream B)**: badge "coexistencia" en `/admin/conexiones` y
  lectura de `is_on_biz_app` en el wizard (sin migración).
- Tests: `tests/test_waweb_{policy,handoff,connector,store,limits,manager}.py`.
- **Verificado en vivo (sin SIM):** el connector arranca, el barrido encuentra la
  sesión con riesgo aceptado, Neonize conecta y **genera un QR real** que se
  publica a Redis y el panel lo muestra (`/admin/conexiones/waweb`). Mapeo de
  eventos de Neonize confirmado en código (`event.qr`, `ConnectedEv`,
  `DisconnectedEv`, `LoggedOutEv`, `TemporaryBanEv`, `ConnectFailureEv`,
  `MessageEv`).
- **Fix de aislamiento (importante):** el connector corre como `app_runtime`
  (RLS); `active_sessions`/`set_status` y el store de sesión ahora iteran
  `tenants` y abren `tenant_scope` por tenant (antes RLS los dejaba en cero).
- **Arreglos tras la primera prueba en vivo:**
  - Remitente: `MessageSource.Sender` es un **proto JID** (no texto); se resuelve
    con `.User` y fallback LID→`SenderAlt`. Antes producía `whatsapp:+User` y las
    respuestas eran inentregables.
  - UI: se elimina el **reload en loop** al conectar; el polling se detiene y se
    muestra "Número vinculado" en el lugar. `logged_out` muestra un aviso claro.
  - Al `logged_out` se **vacía el outbox** del tenant (no más envíos fantasma).
  - `request_send` **falla rápido** si la sesión no está conectada (marca
    `waweb:ready:<tenant>` la fija el connector al conectar).
  - Detección de **número duplicado** entre tenants (alerta + se detiene el de
    mayor id).

### conexiones clasificadas + pack-calendar v2 (2026-09-26)

Conexiones del panel clasificadas por categoría (comunicación, correo, calendario)
y **pack-calendar v2**, con gating por conexión. Ver ADR-0069/0070/0072. Épica #97.

### Core
- **Catálogo de tipos de conexión** `connection_types` (tabla de sistema, patrón
  `plans`) + `sync_connection_types()` en el arranque (ADR-0069).
- `channel_connections` gana `type_id`/`category`; `save_connection` se generaliza
  (valida credenciales contra el `credentials_schema` del tipo), corrige el filtro
  de tenant del lookup y acota la primacía a la categoría de mensajería.
- `/admin/conexiones`: el modal "Agregar conexión" se genera desde el catálogo,
  agrupado por categoría; los tipos `manual` (correo SMTP) abren un formulario
  genérico de credenciales (FormEngine + SecretBox).
- **Gating de tools por conexión activa** (`MCPTool.requires_connection_types`,
  ADR-0072): en pre-prompt y en `_tool_denial`.

### pack-calendar v2
- Los tokens OAuth de Google pasan a la **conexión del tenant** (vault), con
  dual-read transitorio de `plugin_configs` (ADR-0070).
- **Submenú** `/calendar/*` (ADR-0061/0063): Conexión · Horarios y calendario ·
  Tipos de cita · Citas.
- Tablas `pack_calendar_appointment_types` y `pack_calendar_appointments`
  (`tenant_id` + RLS + FORCE RLS).
- `create_calendar_event` espeja la cita en la tabla; `check_availability` usa el
  horario de atención configurado.
- Sin cuenta conectada, el asistente **no** ofrece agendar (gating).

### Correo saliente (ADR-0071)
- Adaptador de canal `smtp` (built-in, `smtplib`) reutilizando la cola de salida
  (`outbound_send`): un envío es un `Message` del canal `email`.
- Tool **`send_email`** (gated por conexión `email_smtp`): crea el `Customer` +
  `Conversation` del destinatario y encola el correo. Sin inbound.

### Migraciones
- Core `0038` (catálogo de conexiones). Pack-calendar `0001` (tipos de cita y citas).

### pack de cobranza (2026-09-24)

Nuevo vertical **`pack-cobranza`**: carteras, préstamos y negociación de cobranza
por WhatsApp. Ver ADR-0056/0057/0058/0059/0060.

> **Alineado a `app-cobranza` develop** (ADR-0060): vocabulario **Propietario →
> Cartera → Cliente → Préstamo** (no socios/rutas), con mapeo de compatibilidad
> 1:1 documentado. 20 tablas en v1.

### Pack (`packages/pack-cobranza/`)
- **21 tablas** `pack_cobranza_*` con `tenant_id` + RLS + FORCE RLS; PostGIS para
  la geografía de clientes (extensión + `geography(Point,4326)` + trigger + GIST).
- **Bloque A (operación)**: crear préstamo (genera cuotas), registrar pago y abono
  (mueven caja y saldo), detalle de cliente con cuotas/abonos, y **auditoría
  imborrable** (`pack_cobranza_auditoria`) de las operaciones. Tablas de cambios de
  cliente y evidencias de cliente.
- **Bloque B (operación de cartera)**: pestañas **Notas** (CRUD), **Estadísticas**
  (KPIs por cartera) y **Trayecto diario** con mapa (Leaflet/OSM), apertura del día
  y registro de posiciones. Tablas `cartera_notas`, `carteras_aperturas`,
  `posicion_gestor`, `reportes_diarios`, `cartera_estadisticas`, `cartera_optimizada`.
- **Bloque C (monitoreo)**: sección **Monitoreo** con **Reglas de negociación**
  (edita la fila por tenant que usa el motor determinista) y **Monitoreo IA**
  (conversaciones/escalaciones del core + promesas del asistente).
- **Bloque E (cobros al propietario, Épica 9)**: pantalla **Cobros** (generar el
  cobro del periodo, registrar pago, crear link de pago). Tablas
  `cobros_propietario` y `links_pago`.
- **Bloque F (RBAC por sub-sección, ADR-0063)**: los items de `admin_menu` declaran
  `section_key`; el RBAC resuelve por sub-sección (`pack_cobranza:carteras`,
  `:clientes`, `:cobros`, …), así un colaborador puede tener write en unas y read
  en otras vía grants.
- **Bloque D (API de campo, ADR-0057)**: `public_routes()` en `BaseModule` + API
  pública `/api/pack/pack_cobranza` con **JWT propio del pack** (HS256, sin deps).
  Endpoints: login de dispositivo, mis-carteras, clientes-del-día y **sync offline
  idempotente** (visita/pago/abono). Tablas `devices`, `intentos_acceso`,
  `sync_eventos` + `gestores.password_hash`. Pantalla **Dispositivos** (genera
  código + API key, ver intentos de acceso).
- **Reportes globales**: consolidado multi-cartera (clientes, préstamos, saldo,
  cobrado, caja, gastos, cuotas vencidas) + liquidaciones recientes.
- **Alertas de acceso** (sección Seguridad): intentos de acceso de la app de campo
  con los denegados resaltados.
- **Editar cuota**: formulario completo (valor, vencimiento, estatus) con auditoría.
- **Conversación del cliente**: acceso directo desde el detalle del cliente a la
  conversación del core (si el cliente está enlazado a un `Customer`).
- **Auditoría**: pantalla de `AuditoriaCartera` (imborrable) con filtros por entidad,
  operación y rango de fechas.
- **Promesas**: listado filtrable por estado + marcar cumplida/incumplida (con auditoría).
- **Caja** (pestaña de la cartera): saldo inicial/actual + libro de movimientos
  (pagos, abonos, inyecciones, gastos aprobados, préstamos y ajustes) + **ajuste manual**
  con motivo. Ahora **inyecciones y gastos aprobados mueven el saldo de caja**, para que
  el libro cuadre.
- **Eventos de sincronización** de la app de campo en la pantalla **Dispositivos**
  (`SyncEvento`, con los `error` resaltados).
- **Evidencias de cliente** (Bloque H): endpoint de la API de campo
  `POST /api/pack/pack_cobranza/clientes/{id}/evidencia` (multipart, foto facial o
  documento; jpeg/png/webp/pdf, máx. 5 MB) que guarda en
  `TENANTS_ROOT/<slug>/evidencias/` (fuera de Postgres) + galería en el detalle del
  cliente con servido tenant-scoped.
- **Cierre diario** (Bloque H): cron `cierre_diario` que congela `ReporteDiario` y
  `CarteraEstadisticasSnapshot` por cartera (idempotente por fecha), con botón
  "Generar cierre del día" e histórico en la pestaña **Estadísticas**.
- **Optimización de trayecto** (Bloque H): orden por vecino más cercano (haversine)
  desde la apertura del día → `CarteraOptimizadaLog` + plan y ruta en el mapa.
- **Comunicación con el propietario** (Épica 9, HU-63; ADR-0064): hilo por tenant
  (`conversaciones_propietario` + `mensajes_propietario`) con notificaciones de cobro
  auto-registradas, mensajes manuales saliente/entrante y enlace **wa.me** al
  WhatsApp del propietario (`CobranzaConfig.propietario_whatsapp`).
- **API de campo del gestor — contrato de la APK** (ADR-0065): router
  `api_gestor.py` en la API pública, con el contrato exacto de `solvershub-apk`
  (camelCase, IDs string). **F1**: `POST /auth/gestor/login` y
  `/auth/propietario/login` (`tenant`+`usuario`+`password` → access+refresh),
  `POST /auth/refresh`, `PATCH /perfil`.
- **Autovinculación de dispositivo (HU-39/43, ADR-0068)**: `POST/GET /gestor/dispositivo`
  (IMEI + WhatsApp, estado `pendiente`); el login exige coincidir con un device
  `activo` con IMEI (403 + `IntentoAcceso` si no) y no bloquea el primer vínculo;
  el panel **Dispositivos** aprueba/bloquea. Migración `0012` (IMEI único). X25519/HU-40 diferido.
- **Identidad unificada: el gestor es un colaborador del core** (ADR-0066). El
  propietario es `Admin` + `TenantUser(owner)`; el gestor es `Admin` +
  `TenantUser(colaborador)` **con perfil `Gestor`** (`admin_id`, sin credenciales
  propias). El core gana el punto de extensión `BaseModule.collaborator_roles` +
  `on_collaborator_role` (sin tabla nueva): `/admin/colaboradores` ofrece "Gestor
  de cobranza" y, al crearlo, el pack crea el perfil; crear un gestor desde
  Gestores crea la cuenta (aparece en Colaboradores). Migración `0009`.
- **API de campo del gestor — F2 (lecturas)**: mis-carteras (con `config` y
  `permisos`), notificaciones, trayecto-diario, estadísticas (con snapshot para
  `anterior`/`deltas`), clientes, tarjeta, préstamos, estado-cuenta, detalle de
  cuota, notas, liquidaciones y servido de evidencias por Bearer. Todo
  tenant-scoped y filtrado por cartera visible (gestor → las suyas; propietario →
  todas); `pack_cobranza/estadisticas.py` comparte los conteos con el cron diario.
- **API de campo del gestor — F3 (escrituras)**: visitas (pago/no-pago con pago o
  abono y promesa), gastos (multipart con evidencias), alta/edición de cliente
  (multipart con evidencias), préstamos, edición/eliminación de cuotas y bajas de
  pago/abono (con contraseña del admin + auditoría), notas, apertura/posición,
  trayectoria real, generar trayecto, editar cartera/configuración, liquidaciones y
  export imprimible (HTML en v1).
- **Gestor B (REQ-APK-002/003)**: `trayecto-diario` incluye `latitud`/`longitud` por
  cliente (cliente cercano en la app); la liquidación calcula KPIs reales
  (`estimado_acobrar`, `total_inyeccion`, `suma_cartera`, `comision_porcentaje`)
  y los conteos del día (`pagaron`/`faltan`/`totalDia`/`sinCuentas`/horas) —
  migración `0010`.
- **Capacidad de pagos del core + cobro digital (ADR-0067)**: registry
  `core/payments/` (molde `LeadRegistry`), topics `ON_PAYMENT_*` movidos al core y
  `qr_png_data_uri`. `pack_payments` registra su capacidad (Wompi) y `pack_cobranza`
  crea el link/QR de una cuota (`POST …/cuotas/{id}/link-pago`), registra el `Pago`
  al confirmarse por EventBus (idempotente), o devuelve la **cuenta informativa**
  de `CobranzaConfig` si no hay pasarela. Migración `0011` (LinkPago→cuota).
- **API de campo del gestor — F4 (offline)**: `POST /sync-offline/eventos`
  (idempotente por `eventoIdCliente`, **ack por evento**, savepoint por evento;
  tipos `visita`/`pago`/`abono`/`gasto` con evidencias base64/`trayectoria`/
  `cambio_cliente`) y `GET /sync-offline/trayecto-diario`.
- **5 tools del asistente** (orientadas al cliente): consultar saldo, estado de
  cuenta, **evaluación determinista de negociación**, registrar promesa y abono.
  Las operaciones de administración (gasto, inyección, visita, liquidación) viven
  en el panel, no en el asistente del deudor (auditoría M3). Guardrail
  `cob_promesa_check` y cron `marcar_mora`.
- **Panel propio (`admin_router`, `/cobranza/…`)**: Resumen con KPIs, CRUD de
  Gestores/Carteras/Clientes, matriz de **permisos del gestor** (14) y **detalle de
  cartera con pestañas** (Resumen · Configuración · Clientes · Gastos · Inyecciones ·
  Liquidaciones). `cartera-config` con los 32 flags.
- **El propietario ES el tenant** (ADR-0062): sin entidad `propietarios` ni
  `propietario_id`; su config vive en `CobranzaConfig` y sus permisos en el RBAC
  del core (owner=write, colaborador=read).
- `flow.json` con las secciones de atención.

### Core
- **ADR-0056**: leads globales con `LeadProvider` (`core/admin/lead_registry.py`),
  sección `leads` y vista `/admin/leads` que combina `Customer` + columnas por pack.
- **ADR-0058**: imagen de Postgres con **PostGIS + pgvector** (`docker/postgres/Dockerfile`).
- **ADR-0057/0059**: diseño de rutas públicas de pack e identidad de pack, y
  aislamiento multi-tenant del pack (1 tenant = 1 propietario).
- **ADR-0060**: alineación del pack al modelo de dominio de `app-cobranza`
  develop (mapeo de compatibilidad Propietario/Gestor/Cartera).
- **ADR-0061**: `admin_menu` de packs por **secciones** (`section`) y visibilidad
  por rol (`master_only`); el menú del pack replica la IA del panel de referencia.
- **ADR-0062**: el propietario es el tenant; el pack no duplica identidad.
- **ADR-0063**: RBAC por sub-sección de pack (`section_key` en `admin_menu`).
- Menú de packs **desplegable** en el panel (`core/admin/templates/base.html`).

## v0.7.0 — Operación y gobierno del agente (2026-09-23)

Capacidades empresariales de operación sobre lo construido en ADR-0049/0050. Ver ADR-0051.

### Rollback de AgentSpec en el panel
- Botón "Activar" por versión en el tab Avanzado; helper único
  `activate_spec_version` (CLI y panel). Sin canary (ADR-0016).

### Horarios y autoescalado
- `AppSettings.escalation_config` gana `business_hours` (tz + franjas), mensaje fuera
  de horario y `auto_escalate_after_turns`. Evaluación con `zoneinfo`, fail-safe ante
  config corrupta. UI en el tab Identidad.

### Guardrails declarativos por tenant
- Motor de reglas (DSL seguro, sin regex crudo) en `AgentSpec.extensions["guardrails"]`:
  `forbidden_phrase`, `required_tool`, `max_length`, con acción `retry`/`escalate`.
  UI en el tab Habilidades.

### Evals por versión de spec
- Tablas `eval_runs` / `eval_results` (migración 0036, RLS); el runner persiste cada
  corrida atada a `spec_version` y el tab Avanzado muestra el historial de calidad.

## v0.6.0 — Configuración por pestañas, pagos BYOK y AgentSpec v2 (2026-09-23)

Panel configurable por secciones, cobro BYOK del tenant y un contrato de agente
que deja de prometer en vano. Ver ADR-0047/0048/0049/0050.

### RBAC y colaboradores (ADR-0047/0048)
- Permisos de sección configurables (`admin_permissions`, global o por tenant).
- Admin por tenant (`TenantUser.owner`) con login propio; página "Colaboradores".
- CLI `agent-core admin` y alta de tenant con credenciales del administrador.

### Configuración por pestañas y pagos BYOK (ADR-0049)
- `/admin/configuracion` en pestañas: **Identidad** y **Pagos** (esta última si el
  pack `pack-payments` está habilitado).
- Pantalla de pagos: credenciales Wompi por tenant cifradas, "Probar y guardar"
  (link real por el mínimo), URL del webhook.
- `BaseModule.extra_admin_routers()`: packs aportan pestañas admin sin tocar el core.

### AgentSpec v2.0.0 (ADR-0050)
- El contrato pasa a v2.0.0: campos antes muertos pasan a tener efecto real o se
  eliminan. `temperature`, `model.fallback`/`router`, `memory.shortTermTurns`,
  `rag.topK`/`minScore`, `persona.tone`/`warmth`/`emojis`/`forbiddenPhrases`/`maxLength`
  y el motor real de `ToolBinding.scopes`/`maxCallsPerConversation`/`requiresConfirmation`.
- Nuevo bloque `extensions` (personalización por tenant sin fork).
- `load_active_spec` tolerante: los specs v1 guardados siguen cargando (los campos
  eliminados se descartan, los nuevos toman default).

## v0.5.0 — Planes y facturación de tenants (2026-09-22)

Cada tenant opera 1 mes gratis y después se vincula a un plan (basic, premium,
empresarial) con cuotas y cobro por Wompi. Ver `docs/BILLING.md`, ADR-0045/0046,
HU-011.

### Planes y cuotas (ADR-0045)
- Catálogo de planes en código (`core/billing/plans.py`) espejado a la tabla de
  sistema `plans` (`sync_plans()` al arrancar). 4 planes con límites de clientes,
  conversaciones, audio, OCR, plugins, mensajes y costo LLM del período.
- `tenant_subscriptions` (RLS): todo tenant nuevo arranca en `free` con 30 días de
  trial; backfill de la migración `0034` para los existentes (30 días desde la migración).
- Medición derivada de las tablas fuente (`core/billing/metrics.py`): sin contador
  paralelo, consistente con `usage_ledger` (ADR-0008).
- Enforcement de bloqueo duro (`core/billing/enforcement.py`): tenant no operacional
  si el trial/período venció (webhook + consumer); cuota alcanzada bloquea la nueva
  unidad de la métrica (cliente, conversación, audio/OCR, pack, turno).
- Cron `core.billing_lifecycle` (diario) normaliza `trialing→expired`/`active→past_due`.

### Panel
- `/admin/plan`: plan actual, estado, días restantes, barras de progreso por métrica,
  planes disponibles e historial de pagos.
- Banners globales de cuota (80%/100%) y servicio detenido; resumen en el dashboard.
- `/admin/tenants` (master): plan de cada tenant + cambio de plan/ extensión de
  período con registro en `audit_log`.

### Cobro con Wompi (ADR-0046)
- `core/billing/providers/wompi.py` (cliente propio de la plataforma, replica el
  algoritmo probado de `pack-payments`): link de pago por período, firma de webhook
  verificada, correlación por `payment_link_id`, mínimo real de 150.000 COP.
- `POST /admin/plan/subscribe` + `POST /webhooks/billing/wompi` (firma + idempotencia).
- Débito recurrente automático: **diferido y documentado** (la interfaz queda lista).

### Infra / fixes de tests
- `.env.example`: bloque de billing (`BILLING_PROVIDER`, `WOMPI_BILLING_*`).
- Tests de integración: helper compartido `_meta_request` firma los webhooks con el
  App Secret de `.env` (antes fallaban 401 con el secret configurado); fixture
  `_reset_test_tenants_state` cierra conversaciones de los tenants de prueba para
  que una BD local reusada entre corridas no contamine los tests.

## v0.4.0 — BYOK de WhatsApp productivo (2026-09-22)

Deja el flujo BYOK (app propia de Meta) funcionando de punta a punta: mensaje entra →
LLM responde → llega al celular.

### Webhook y credenciales por tenant (ADR-0040)
- El webhook resuelve `verify_token` y `app_secret` por tenant
  (`ChannelConnection` manual → BYOK → `.env`); antes caían al `.env` y daban 403/401.
- El sender usa el `access_token` BYOK (`cloud_api_access_token`); ya no cae al `.env`.

### Asistente de token y expiración (ADR-0041)
- Wizard BYOK: "Extender a 60 días" (`fb_exchange_token`) y "Verificar" (`debug_token`);
  se persiste `expires_at`, badge en `/conexiones/salud` y cron diario de vencimiento.
- Mensaje claro para token vencido/inválido (`190/463`).

### Reconexión y crons de Meta
- Reconectar una app tras "eliminar" crea una conexión nueva y reasigna el
  `phone_number_id` (unique global) en vez de duplicar.
- Crons de Meta (`subscriptions_health`, `template_sync`, `pricing_sync`,
  `health_check`) enumeran cross-tenant vía funciones `SECURITY DEFINER`
  (migración `0033`, ADR-0039); antes eran no-op silencioso por RLS.

### Worker / Celery (ADR-0042, ADR-0044)
- Se registra `core.outbound_send` (`include`) y se reemplaza `asyncio.run()` por un
  puente async con **loop persistente** (`core/shared/async_bridge.py`).
- El beat escanea plugins (`beat_init`) y los crons de los packs se registran al importar
  el módulo del pack → `pack_commerce.follow_up_leads/quotes` quedan programados.

### Alta de tenant (ADR-0043)
- `/admin/tenants/nuevo` conecta WhatsApp por el flujo BYOK (MetaConnection +
  suscripción app/WABA); si falla, conserva el tenant y avisa.

### Infra
- La Funnel de Tailscale funciona tras reinstalar/configurar; **dominio estable pendiente**
  (ver `docs/BACKLOG.md`).

## v0.3.0 — Sprint 3 (2026-08-26)

### CULTO migrado (tenant #1)
- `crm_*` multi-tenant: migración `crm_0002_tenant` (tenant_id + RLS + policy en las 13
  tablas), UNIQUEs compuestos por tenant, **numeración de pedidos por tenant** (ADR-0018,
  `OC-0001` independiente por cliente), GRANT a `app_runtime`.
- Modelos del pack con `tenant_id` fail-fast (error claro sin scope); crons iteran tenants.
- Middleware por allowlist de rutas públicas: `/crm/*` y `/calendar/*` quedan con scope.
- Playbooks de CULTO a `SKILL.md` (ventas, seguimiento) + copy al cliente a `CRMConfig`
  editable (ADR-0017). Fix: `sync_database_url` usa el dueño para migraciones.

### Evals y CI
- Golden set de 35 escenarios del dominio CRM (camino feliz, objeciones, fuera de alcance,
  datos no disponibles, manipulación, escalamiento).
- Harness parametrizado por el spec de CULTO (`evals/specs/culto.json`) con reporte
  `(escenario, spec_version, git_ref)`; soporta subconjunto crítico.
- CI GitHub Actions (bloquea: ruff + pytest + gate de evals) + nightly (set completo).

### Canary y rollback (ADR-0016)
- Canary redefinido como gate + tenant staging + rollback 1 paso.
- `agent-core agent list|activate|rollback`: flip de `is_active` en un paso.

### Para quien usa esto
- `crm_*` ahora exige `tenant_scope` para insertar (error claro si no).
- Los mensajes de follow-up/confirmación/estados se editan en la configuración del pack.
- `agent-core db migrate` usa `MIGRATIONS_DATABASE_URL` (dueño) para migraciones.

**Rollback:** `git revert` + `alembic downgrade` de las migraciones 0005/0004/0003 y
`crm_0002`; `agent-core agent rollback` revierte el AgentSpec activo.

## v0.2.0 — Sprint 2 (2026-08-26)

### Runtime
- `AgentSpec` desde base de datos: tabla `agent_specs` (UNIQUE tenant+version, `is_active`,
  RLS), spec validada contra el contrato congelado, seed en BD. Cero prompts en código
  (ADR-0014): identidad/tono/reglas/respuestas viven en playbooks `.md` con allowlist.
- 8 tools nativas del core (skill `core`): `escalate_to_human`, `get_customer_profile`,
  `update_customer_profile`, `remember_fact`, `search_memories`, `load_playbook`,
  `get_conversation_usage`, `set_conversation_note`.
- Tool loop con límites (ADR-0015): `maxToolCallsPerTurn` y `budgetUsdPerConversation`
  desde el spec; presupuesto medido contra `usage_ledger` en la cabecera del turno; flag
  `budget_exhausted`; el core ya no se auto-desactiva.
- `usage_ledger` append-only (ADR-0008): una fila por turno con `agent_spec_version`,
  tools y resultado. Fuente única de costo.
- OpenTelemetry: span por turno con hijos `llm_call`/`tool_execute`, export OTLP por env.

### Panel
- `/admin/costos` sobre `usage_ledger` por tenant (filtro explícito + RLS) y por
  conversación, en vivo. Dashboard con filtros de tenant explícitos.

### Entidad de datos
- Tabla `agent_specs`, columna `usage_ledger.agent_spec_version`. Migración 0004 (con
  downgrade probado).

### Para quien usa esto
- Si tenías un tenant existente, el spec default se siembra/actualiza solo.
- `llm_usage` queda como fuente legacy (se retirará en Sprint 3).
- Config nueva: `MAX_CONCURRENT_TURNS`, `MAX_TURNS_PER_TENANT`, `OTEL_ENABLED`,
  `OTEL_EXPORTER_OTLP_ENDPOINT`.

**Rollback:** `git revert` + `alembic downgrade 0003` deja el esquema previo; las filas de
`usage_ledger`/`agent_specs` se descartan. Sin canary todavía (producción llega en Sprint 3).

## v0.1.0 — Sprint 1 (2026-08-26)

- Esquema multi-tenant: `tenant_id` + RLS real (rol `app_runtime`) en todas las tablas de
  negocio, `processed_events` (ADR-0007/0013), `usage_ledger`, `tenant`, `tenant_user`.
- Auth propia (ADR-0012): `tenant_scope` + `set_config` en transacción; resolución de
  tenant por phone_number_id.
- Gateway: dedupe, cuota de concurrencia por tenant, DLQ con reintento manual.
- 4 tests de salida de integración (dedupe, debounce, fairness, RLS).