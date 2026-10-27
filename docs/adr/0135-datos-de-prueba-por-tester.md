# ADR-0135 — Datos de prueba propios de cada tester

- **Estado:** aceptado (dueño, 2026-10-13)
- **Fecha:** 2026-10-13
- **Contexto:** SPEC-30 de solvershub-docs (D-SPEC-30-6; HU-QA-07 y HU-QA-08). **Usa:** ADR-0083 (`seed_data`),
  ADR-0110 (fragmentos de preset por pack), ADR-0133 (tablas del centro de pruebas), ADR-0134 (rol tester).

## Contexto

- El dueño pidió que el centro de pruebas «facilite los datos necesarios para probar, o por lo menos cómo
  gestionarlos o crearlos». Unas 45 pruebas del catálogo piden la cuenta del dueño de Cobranza Demo, y esas
  contraseñas no se reparten a testers externos.
- El dueño eligió las dos opciones: un botón que genera datos propios por tester (solo en test) y una guía para
  los datos externos (Wompi, Meta, correo). Las cuentas demo compartidas quedan para el equipo interno.

## Decisión

1. **«Preparar mis datos»** (`POST /api/v1/admin/qa/datos`, sección `qa`, solo con `QA_TESTERS_ENABLED`) crea
   un negocio de prueba por tester (`qa-<8 hex del id>`) con `onboard_tenant`: Comercio, Agenda y Cobranza, plan
   premium, y tres cuentas en el dominio reservado `pruebas.solvershub.test`:
   - **dueño** (owner): panel y APK como propietario;
   - **gestor** (colaborador con perfil de gestor): app de campo, con la cartera asignada y todos sus permisos;
   - **colaborador**: Resumen, Conversaciones, Leads y Escalaciones.
2. **Los datos de ejemplo salen de fragmentos `packages/pack-*/presets/qa.json`** y entran por el `seed_data`
   de cada pack (ADR-0083, ADR-0110): el core no importa packs. Se suman `seed_data` a Agenda (tipos, citas y
   bloqueos) y a Cobranza (cartera, caja, gestor y clientes al día, en mora y sin préstamo). Ambos son
   idempotentes. Los clientes no llevan teléfono, para que ningún recordatorio llegue a un número real.
3. **Las contraseñas se generan al azar y se devuelven una sola vez** (al preparar o al pedir contraseñas nuevas,
   `POST /datos/contrasenas`, que además cierra las sesiones del panel de esas cuentas). No se guardan.
4. **`qa_tester_datasets`**, tabla de sistema sin RLS como el resto de `qa_*`: una fila por tester reclama la
   corrida con un INSERT condicional. Si una corrida falla a medias, la siguiente reutiliza el negocio y las
   cuentas que esa misma fila creó, y nunca toma una cuenta ajena con el mismo correo. `tenant_id` es FK lógica:
   si el master purga el negocio, el tester lo vuelve a preparar.
5. **Las cuentas de prueba siguen al tester.** Viven en el dominio reservado `pruebas.solvershub.test`:
   `cuenta_habilitada` las rechaza sin `QA_TESTERS_ENABLED`, igual que al tester, y revocar al tester las
   desactiva y cierra sus sesiones. El flujo solo toca una cuenta de su fila si sigue siendo exclusiva del negocio
   de prueba (colaborador, su correo, sin permisos globales ni vínculos con otros negocios) y nunca reactiva una
   cuenta que el master desactivó.
6. **Sin carreras con otras altas:** la fila del negocio se crea con el id y el slug de la corrida antes de
   `onboard_tenant` (el único de `tenants.slug` hace de candado), y un token de corrida en la fila impide que una
   corrida vencida cierre la de otra.
7. **Catálogo v6 (HU-QA-08):** cada prueba lleva `entorno` (panel, apk, whatsapp, web o api), `area` (la sección
   del panel o la pantalla de la APK) y `requiere` (slugs de la sección nueva `datos`, la guía de datos externos).

## Consecuencias

- Cada tester prueba con su propio negocio sin pisar a los demás. En test quedan negocios `qa-*`, que el master
  purga desde Negocios cuando un tester se va.
- Los testers se ven los negocios de prueba entre sí: el rol tester ve todos los negocios de test (ADR-0134).
- Un cambio de esquema en Comercio, Agenda o Cobranza que rompa el seed lo detecta
  `tests/integration/test_qa_datos.py`.
- La app de campo emite tokens propios de 12 horas: pedir contraseñas nuevas no los corta, pero `_auth`
  revalida la cuenta en cada pedido (ADR-0134).

## Adenda (2026-10-18): el negocio de prueba listo para el Agent OS (HU-QA-09)

«Preparar mis datos» deja además el negocio `qa-<hex>` del tester listo para probar el Agent OS
(`core/qa/datos_aos.py`), con las mismas funciones de la operación y sin duplicar su lógica:

1. **Chat web activo** (`chat_web_conexion.guardar_widget`, extraída de `PUT /settings/web-chat`): reutiliza la
   clave si existe, lo habilita y agrega a `allowed_origins` el origen de `LANDING_BASE_URL`, que sirve `/w/<clave>`.
   Las demás conexiones no se tocan.
2. **Backfills** de conexiones, agentes y releases y **`migrar_a_v3(construir=True)`**, como la CLI.
3. **Agentes del preset v2** (`aplicar_agentes`) con un manifiesto en memoria: las plantillas `ventas` y `agenda`
   (constante `AGENTES`, con las keys de sus plantillas). Cobranza no: el catálogo de pruebas la crea desde el
   editor de agentes, y con la key ocupada el editor respondería 409. Ningún preset de archivo encaja con el negocio
   de prueba, y uno en `core/presets/` aparecería en el alta de clientes reales. No escribe `settings.preset.id`:
   `presets-v2 --todos` lo omite. Nunca toca agentes que ya existan.
4. **Release activa** en cada conexión conversacional (`construir` + `promover`, que pasa el gate).
5. **`aos_procedures = true` y `aos_routing = 'active'`** en un solo UPDATE con el rol dueño
   (`MIGRATIONS_DATABASE_URL`, que tiene el contenedor de la API), con la defensa en la misma consulta: id, slug
   `qa-` del tester, vivo y en `v3`.
6. **Volver a pulsar** con los datos listos solo pone al día el Agent OS (y vuelve a sembrar el catálogo de
   Comercio, idempotente por SKU, para que los negocios anteriores reciban el plan de prueba): 200, sin
   contraseñas, sin tocar la corrida ni las cuentas. Es idempotente, también para los negocios `qa-` creados antes en legacy.
7. **Si un paso falla**, el motivo queda en `qa_tester_datasets.error` (con estado `listo`: las cuentas y los
   datos sirven igual) y la respuesta lo muestra en `agent_os.motivo`. El negocio queda como esté: antes de
   `migrar_a_v3` sigue en legacy; después ya es un v3 funcional con el General y el reintento completa. **No se
   vuelve a legacy**: reconstruir en legacy refrescaría el Coordinador desde el v2 y perdería las reglas `preset_*`,
   que la lápida de ADR-0139 ya no repone.

El fragmento `pack-commerce/presets/qa.json` suma el plan `QA-PLAN-MENSUAL` (mensual, 150.000 COP, el mínimo de un
link de Wompi sandbox) para `contratar_plan`. El cobro en línea NO se configura: el negocio de prueba no tiene
`pack_payments` y copiar las claves de otro negocio o de la plataforma sería mover secretos entre negocios.

**Excepciones acotadas** al negocio `qa-` del propio tester y a `QA_TESTERS_ENABLED` (más
`AOS_CONNECTION_ROUTING`):

- El tester escribe banderas `aos_*` con el rol dueño, en lugar del master (ADR-0112).
- La release se promueve sola (ADR-0117 y ADR-0139 §5): el tester es el dueño de su negocio de prueba y pulsar el
  botón es su decisión.
- Se saltan el pass^4 y la semana en sombra (runbooks de ADR-0117 y ADR-0138).

**Deuda:** un setter SECURITY DEFINER de banderas (previsto en la adenda HU-AOS-15 de ADR-0117) reemplazaría el
uso del rol dueño desde la API. La respuesta (`agent_os`: estado, motivo, chat web con clave y enlace, agentes)
también va como una entrada «Agent OS» en `contenido`, que la pestaña Datos del panel ya muestra; el botón
«Preparar» en estado `listo` es un cambio pendiente del panel.

## Adenda (2026-10-26): escenarios de datos de prueba (HU-QA-13)

«Preparar mis datos» deja el negocio `qa-<hex>` listo pero sin actividad: sin conversaciones, turnos,
escalaciones, versiones, pedidos, suscripciones, avisos ni mensajes fallidos. El dueño decidió dar esos datos de dos
formas: **escenarios con un botón**, deterministas y sin LLM, para lo difícil de reproducir, y la conversación real
por el chat web para los flujos de punta a punta. Esta adenda fija los escenarios (`core/qa/escenarios.py`).

1. **Registro de escenarios.** Cada escenario declara `slug` (fijo: el catálogo de pruebas lo cita como
   `datos-escenario-<slug>`), título, qué crea, qué requiere y la ruta del panel donde se ve. Son diez:
   `conversacion-con-turnos`, `escalacion-en-curso`, `calidad-con-muestra`, `version-candidata`, `canary-en-curso`,
   `clientes-y-pedidos`, `suscripciones`, `notificaciones`, `mensajes-fallidos` y `cobranza-completa`.
2. **API** bajo `/api/v1/admin/qa`, con `_solo_plataforma` y la sección `qa`: `GET /datos/escenarios` (la lista y lo
   que creó la última vez cada escenario para este tester) y `POST /datos/escenarios/{slug}` (lo que creó y la ruta
   para «Abrir»). **404 sin `QA_TESTERS_ENABLED`**: fuera de test los escenarios no existen.
3. **Dónde escriben.** Solo en el negocio de la fila `qa_tester_datasets` del tester: estado `listo`, negocio vivo,
   `slug = qa-<hex del tester>`, revalidado con `slug LIKE 'qa-%'` en la misma consulta que lo busca (como
   `datos_aos._negocio_del_tester`). Todo corre en `tenant_scope` de ese negocio. Sin datos preparados, 409 «prepara
   tus datos primero». Los escenarios del Agent OS (turnos, escalación, calidad, versiones y canary) exigen además el
   Agent OS listo (v3 con las instancias Ventas y Agenda en el chat web, HU-QA-09) y `KERNEL_LEDGER_ENABLED`: sin el
   ledger el kernel no escribe turnos.
4. **Un candado por negocio.** `pg_try_advisory_xact_lock('qa_escenario:<tenant>')` en una sesión abierta durante el
   escenario (el patrón de `datos_aos.preparar`): dos pulsaciones a la vez dan 409. `version-candidata` y
   `canary-en-curso` toman también `CANDADO_SQL` (`agent_os:<tenant>`), porque construyen releases igual que
   «Preparar» y el panel de banderas.
5. **Deterministas, sin LLM y aditivos.** El contenido es fijo (textos, montos, etapas, conteos); solo cambian los ids y
   un sufijo corto que hace únicos los SKU, las claves de idempotencia y los nombres. Los textos y montos fijos de los
   packs viven en su `presets/escenarios.json`, no en `.py`. **Repetir crea otro caso** y no
   borra lo anterior: `audit_log`, `task_events`, `core_decisions`, `turn_outcomes` y `usage_ledger` son de
   solo inserción, y lo creado se va cuando el master purga el negocio de prueba. La única configuración que cambia es
   la del propio negocio de prueba: `version-candidata` y `canary-en-curso` cambian el nombre del asistente y
   `suscripciones` activa el pack.
6. **Funciones del core, nunca código de tests.** Tareas con `kernel.state.iniciar`/`registrar`; escalación con
   `orchestrator.escalation.escalate`; A2A con `kernel.a2a.registrar`/`liquidar` (la fase 2 del traspaso la corre el kernel al cerrar el turno);
   releases con `releases.build.construir` y `releases.canary.iniciar`; veredicto con `releases.veredicto.registrar`;
   avisos con `notifications.service.notify_standalone`; perfil con `memory.facts.escribir_hechos`; pasos con
   `shared.audit.record_audit`.
7. **Turnos: con el ledger del kernel, no a mano.** Cada turno sembrado corre dentro de `kernel.decisions.en_turno` y
   anota con sus funciones (`anotar_release` con el carril y la instancia que atiende, `anotar_ruta`, `anotar_foco`,
   `enrutar`/`bloquear`, `anotar_uso`, `anotar_veredicto`, `anotar_handoff`/`anotar_a2a`, `marcar_escalado`): el kernel
   escribe `core_decisions` y `turn_outcomes` y corre la fase 2 del handoff al cerrar. El escenario no escribe esas dos
   tablas. Los pasos van con `record_audit` (toma el `turn_id` del turno vigente) y `messages`/`usage_ledger` con sus
   modelos ORM, como `releases/veredicto.py`; en `usage_ledger`, `model = 'escenario-sin-llm'` y `credits = 0`: no
   consume el crédito del plan. El traspaso es un handoff real (`a2a.registrar` con el grafo congelado de la release
   activa, `anotar_handoff`): queda `core_decisions.active_instance_id` (Ventas) ≠ `turn_outcomes.final_instance_id`
   (Agenda). Como los turnos son de verdad, su `created_at` es el de la pulsación: la muestra de Calidad cae en el
   período por defecto y las latencias son las del escenario (milisegundos).
8. **Packs: un gancho opcional de `BaseModule`, sin importarlos.** Pedidos, promociones, leads, suscripciones y
   cobranza son tablas de pack: el core no las toca. `BaseModule` (`core/mcp/types.py`, junto a
   `sembrar_datos_de_prueba`) suma

   ```python
   async def sembrar_escenario(self, tenant_id: uuid.UUID, escenario: str,
                               entrada: EntradaDeEscenario) -> ResumenDeEscenario
   ```

   con tipos en `core/qa/contrato_datos.py` (entrada: sufijo, moneda, zona horaria, clientes y conversaciones que ya
   creó el core y, para `pack_subscriptions`, los planes que sembró Comercio como `PlanSnapshot` armados por el core
   con su variante; salida: conteos y textos para la tarjeta). Así ningún pack lee las tablas de otro. El default lanza `EscenarioNoDisponible` y el escenario
   responde 409 con el motivo. El core lo llama con `plugin_manager.get(pack)`. Los ganchos cubren solo **actividad**
   (pedidos, promociones, leads, suscripciones con cargos y pagos, promesas, cobros, accesos, dispositivos, reglas y
   cierres): catálogo, planes y cartera se siembran con el `sembrar_datos_de_prueba` que ya existe (ADR-0146). Clientes,
   perfil y notas del equipo son del core. Las suscripciones van en el gancho de `pack_subscriptions` (sus cargos,
   vencimientos y pagos son tablas del pack; la capacidad del core no los expone), sobre planes que crea Comercio.
   `suscripciones` activa el pack con `configure_plugin_for_tenant` (importación diferida, la misma de
   `PUT /admin/plugins/{id}/config`): crea una versión del AgentSpec, así que el próximo build lleva Suscripciones, y
   aplica la cuota del plan (`QuotaExceeded` → 409). Cuando un segundo pack provea suscripciones (HU-SUB-15), el
   escenario tendrá que fijar `settings.subscriptions.provider`. Ni el gancho ni los `presets/*.json` cambian el hash
   del pack (`manifiesto_de_pack`): no hay que subir versiones.
9. **Efectos externos acotados al negocio de prueba.** Ningún cliente sembrado lleva teléfono ni conversación
   `channel='whatsapp'` (el «cliente de WhatsApp» que piden las pruebas es `customers.channel='whatsapp'` sin
   `contact_phone`, con sus conversaciones en `web`), así que ningún aviso al cliente sale por WhatsApp. Sin
   `pack_payments`, nada cobra. `escalate()` y los avisos `warning`/`critical` sí envían push, pero solo a los
   `push_tokens` del negocio `qa-`: a cualquier admin con la APK abierta en ese negocio (las cuentas de prueba del
   tester y, si entró, el master u otro tester). Es lo que piden las pruebas de la APK. El mensaje fallido es una
   entrada del stream `agent_core:inbound:dlq` con el mismo payload que deja el consumer (un inbound `web` válido), y su
   cliente se crea antes con `bot_paused = true`: «Reintentar» lo procesa de verdad, pero sin turno ni envío. La
   entrada se va con el recorte de la DLQ (`inbound_dlq_retention_hours`), y `GET /admin/dlq` lee las primeras 200 del
   stream global: con una DLQ cargada puede no aparecer (bug de `dlq.py`, aparte). El evento de dominio fallido usa un
   topic sin handlers (`qa.escenario_de_prueba`).
10. **Estado único: se respeta, no se pisa.**
    - `version-candidata` cambia `app_settings.assistant_name` (la misma asignación que `PUT /settings/identity`) a un
      nombre único por pulsación (`Asistente QA <sufijo>`) y construye: si el dedupe devolviera la activa o una
      detenida, 409. **No promueve.**
    - `canary-en-curso` construye otra candidata e inicia la prueba al 20 % con `canary.iniciar`, más una muestra
      parcial (carriles `stable` y `canary`, por debajo de las 200 conversaciones: el job la deja en `sin_muestra`).
      Sus turnos cierran con `status = 'replied'` (ninguno `failed` ni `escalated`), así que no hay señal dura. Con una prueba ya en
      curso, 409 «detén la prueba primero»: no se detiene la del tester en silencio.
    - Con el gate de pass^k cerrado en el negocio (`gate_pendiente`/`gate_rechazado`) o el canary apagado
      (`CANARY_DISPONIBLE`), 409 con el motivo: el escenario **no fabrica un `approved`** para pasarlo. La muestra son
      turnos del ledger (punto 7) con `anotar_release(…, lane)`.
    - `calidad-con-muestra` registra un veredicto de consistencia **sobre la release activa** (`base_release_id`
      nulo, `origen='escenario'`, modelo `escenario-sin-llm`, costo 0 y `escenarios_sha256` centinela
      `escenario-qa:<sufijo>`, que nunca coincide con una medición real), que es lo que lee
      `veredicto.consistencia_vigente`. No aprueba candidatas: el gate solo vale con la base igual a la activa. Corta
      la cadena de `consistencia_vigente` (da igual en un negocio de prueba) y Versiones muestra una «aprobada»
      posterior a la promoción.
11. **«Preparar mis datos» con una prueba en curso.** `_activar_releases` construye igual, pero no promueve en la
    conexión que tiene `canary_release_id` (fallaría con `canary_en_curso`) y lo deja en el log; el Agent OS sigue
    `listo`. La tarjeta del escenario avisa que la prueba se detiene o se publica en Versiones. Una candidata sin
    prueba sí la publica «Preparar» al volver a pulsarlo (el build cae en ella por el hash): queda dicho en la tarjeta
    del escenario.
12. **Registro.** Cada escenario creado deja un `qa_events` (`campo='datos.escenario'`,
    `item_slug='datos-escenario-<slug>'`, `admin_id` del tester, `despues` con el resumen en JSON). El GET lee la
    última fila por tester y escenario.

**Consecuencias.** El panel tiene datos para probar cada módulo sin semanas de uso ni un LLM. A cambio, el negocio de
prueba acumula turnos, avisos y versiones de mentira, que solo se limpian purgando el negocio, y los escenarios de
turnos dependen del esquema del kernel: los tests de integración de `tests/integration/test_qa_escenarios.py` lo
cuidan.
