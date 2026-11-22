## El turno corre sobre AgentSpecV3 y KernelSpec (2026-11-22, HU-AOS-90)

- El agente del turno (`core/agents/turno.py`, `AgenteDelTurno`) guarda el `AgentSpecV3` del foco y el `KernelSpec` de la release tal cual y expone lo que lee el loop. Se borra `core/agents/spec_de_release.py`, la vista v2: el turno ya no convierte al contrato `AgentSpec` v2 (ADR-0206 §1).
- Guardrails y tools declarativas salen de `AgentSpecV3.guardrails` y `declarativeTools`, no de `extensions`. Las declarativas con secretos en URL o body se siguen omitiendo (ADR-0206 §2).
- El loop y el toque de opciones dejan de llamar a `load_active_spec()` en cada turno (ya no siembran ni escriben `agent_specs`). Sin agente en foco el turno falla cerrado.
- El texto de escalamiento del ingreso sale del playbook `respuestas` (con el override del negocio) y `load_playbook`/`get_conversation_usage` usan la allowlist y el presupuesto del agente del turno, no los de `agent_specs` (ADR-0206 §4).
- El `usage_ledger.agent_spec_version` conserva `{key}@v3:{versión}` y `coordinador@recepcion:{versión}`.
