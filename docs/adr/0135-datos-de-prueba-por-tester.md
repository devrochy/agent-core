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
3. **Agentes del preset v2** (`aplicar_agentes`) con un manifiesto en memoria: las plantillas `ventas`, `agenda` y
   `cobranza` (constante `AGENTES`, las habilidades del negocio de prueba). Ningún preset de archivo cubre
   Comercio + Agenda + Cobranza, y uno en `core/presets/` aparecería en el alta de clientes reales. No escribe
   `settings.preset.id`: `presets-v2 --todos` lo omite.
4. **Release activa** en cada conexión conversacional (`construir` + `promover`, que pasa el gate).
5. **`aos_procedures = true` y `aos_routing = 'active'`** en un solo UPDATE con el rol dueño
   (`MIGRATIONS_DATABASE_URL`, que tiene el contenedor de la API), con la defensa en la misma consulta: id, slug
   `qa-` del tester, vivo y en `v3`.
6. **Volver a pulsar** con los datos listos solo pone al día el Agent OS: 200, sin contraseñas, sin tocar la
   corrida ni las cuentas. Es idempotente, también para los negocios `qa-` creados antes en legacy.
7. **Si un paso falla**, el motivo queda en `qa_tester_datasets.error` (con estado `listo`: las cuentas y los
   datos sirven igual) y la respuesta lo muestra en `agent_os.motivo`. El negocio queda como esté: antes de
   `migrar_a_v3` sigue en legacy; después ya es un v3 funcional con el General y el reintento completa. **No se
   vuelve a legacy**: reconstruir en legacy refrescaría el Coordinador desde el v2 y perdería las reglas `preset_*`,
   que la lápida de ADR-0139 ya no repone.

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
