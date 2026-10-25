## Gate de release por pass^k, parte 2: qué tocó la candidata y exenciones (HU-AOS-21)

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
