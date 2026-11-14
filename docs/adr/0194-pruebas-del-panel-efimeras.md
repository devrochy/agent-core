# ADR-0194 — Las pruebas del panel son efímeras: simulan acciones y se purgan al cerrar

- **Estado:** aceptado (2026-11-14, por decisión del dueño).
- **Fecha:** 2026-11-14
- **Contexto:** [HU-ARCH-14](https://github.com/devrochy/solvershub-docs/blob/main/03-fase-0-go-live/SPEC-02-agente-confiable/hu/HU-ARCH-14-playground-sandbox.md) y [D-SPEC-02-3](https://github.com/devrochy/solvershub-docs/blob/main/03-fase-0-go-live/SPEC-02-agente-confiable/spec.md#d-spec-02-3--sandbox-del-playground), [HU-AOS-57](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-41-onboarding-obligatorio-y-primer-agente/hu/HU-AOS-57-probar-el-agente-nuevo-en-el-playground.md) y el incidente del tenant Licorería 24/7 (2026-11-14): una prueba creó un pedido, sumó un cliente y consumió el consecutivo real.
- **Usa:** invariantes 1 (multi-tenant y RLS), 7 (lo de fuera es dato) y 8 (sin borrado físico).
- **Enmienda:** [ADR-0115](0115-una-conversacion-siempre-tiene-conexion.md) (la conversación de prueba sigue atada a una conexión) y la [enmienda de ADR-0115 del 2026-10-24](../MULTITENANT.md). No cambia el contrato de conversaciones.

## Contexto
El playground del panel ejecuta el loop real sobre una conversación `channel = "test"`. Hasta hoy esa
conversación se guardaba y las tools escribían en las tablas del negocio: `crear_pedido` incrementaba
`crm_order_counters`, creaba `Order`/`OrderItem`, descontaba stock y avisaba el pedido. La conversación
tampoco se purgaba. Eso produce medidas irreales y consume consecutivos reales.

## Decisión
La prueba del panel es un **sandbox efímero**. Sus acciones se simulan; lo que queda de la prueba se purga
al cerrarla.

1. **Contrato de tools.** Cada `MCPTool` declara `solo_lectura=True` o `side_effects=True`. Una tool con
   efectos declara `simular` o falla cerrado en `test`; la única excepción es `permite_en_prueba=True` para
   memoria/perfil/notas, que solo escriben sobre el cliente sintético. Una prueba de CI falla si una tool
   nueva no declara nada o declara efectos sin simulador.
2. **Simuladores.** Las tools con efectos de comercio, agenda, cobranza, suscripciones, pagos, correo y
   plantillas no ejecutan su handler en `test`. Devuelven un resultado ficticio marcado `simulado: true` y
   guardan su estado (cotización, pedido, cita, suscripción, promesa…) en Redis, por negocio y conversación,
   para que el flujo funcione entre mensajes. Las lecturas de catálogo, inventario y disponibilidad son
   reales. Las tools declarativas POST también se simulan.
3. **Numeración y stock.** El pedido simulado usa `PRUEBA-####` con un contador de Redis: nunca toca
   `crm_order_counters`, no descuenta stock ni crea pedidos reales. Ningún simulador llama a una pasarela,
   Google Calendar, correo o WhatsApp.
4. **Contadores.** Todo agregado de negocio excluye las conversaciones `test`: cuota, actividad por agente,
   consumo por conexión, Dashboard, Costos, fiabilidad/canary, medios y listas de packs. Se aplica también a
   los datos de prueba que ya existan, sin borrarlos.
5. **Purga al cerrar.** `POST /api/v1/admin/playground/close` (y el reinicio) purga físicamente la
   conversación `test`, su cliente sintético y lo que colgó de ellos: mensajes, tareas/eventos, `usage_ledger`,
   `core_decisions`/`turn_outcomes`, `audit_log`, memoria, `option_sends/activations` y tablas de packs con
   `conversation_id`/`customer_id`. La función SQL `purge_test_conversation(tenant, conversation)` valida
   `channel = 'test'` y usa `session_replication_role = replica` para atravesar triggers de FK e
   inmutabilidad, igual que `purge_tenant`. Es una excepción **acotada** al invariante 8: solo una
   conversación de prueba del negocio que llama, nunca datos de negocio.
6. **Red de seguridad.** Una tarea horaria purga pruebas huérfanas más viejas que
   `PLAYGROUND_RETENTION_HOURS` (6 h por defecto). El panel y la APK cierran la prueba al salir del chat, al
   cambiar de pantalla, al terminar el onboarding y al reiniciar. Redis (historial y sandbox) se limpia con
   la misma operación.
7. **UI.** El chat muestra «Acción simulada: no se creó ni modificó data real» cuando corresponde y explica
   que la prueba no altera el negocio.

## Consecuencias
- Probar el agente es seguro en producción: no crea pedidos, clientes, citas, cobros ni consecutivos.
- El realismo depende de los simuladores; una tool nueva debe declararlos y la auditoría de CI lo exige.
- Las pruebas siguen gastando tokens reales (costo de plataforma), pero no consumen la cuota del negocio.
- Lo ya creado antes de este ADR no se limpia automáticamente: el dueño decidió no tocarlo. Los contadores y
  listas lo excluyen, pero el consecutivo ya avanzado y el stock descontado no se revierten.
