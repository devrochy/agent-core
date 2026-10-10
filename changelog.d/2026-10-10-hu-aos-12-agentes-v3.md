## Agent OS: contratos AgentSpec v3 y KernelSpec v1 (2026-10-10)

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
- Pendiente (parte 2): tablas y API de agentes (borrador con `lock_version`, publicación con
  hash), editor de tools declarativas en `/api/v1` y secretos en la instalación `core`. Las
  versiones viejas de `agent_specs` conservan los literales en claro; quedan fuera de alcance.
