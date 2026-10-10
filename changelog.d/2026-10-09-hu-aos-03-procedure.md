## Contrato procedure/1, linter y conversión de flujos (2026-10-09)

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
