## Reglas y tools HTTP por agente en el editor (2026-11-22, HU-AOS-97)

- `GET/PUT /api/v1/admin/agents/{id}/guardrails` edita `AgentSpecV3.guardrails` del borrador y `GET/PUT /api/v1/admin/agents/{id}/declarative-tools` edita `declarativeTools`. Reemplazan las pantallas de Configuración que retiró HU-AOS-91 (D-78, decisión 4).
- El `PUT` de tools valida todo antes de escribir: literales en headers sensibles, dominios internos, secretos sin valor o valores que ninguna tool usa dan 422 por campo y no se guarda nada. Después cifra los secretos nuevos en la instalación `core` del negocio, atados a los dominios de sus tools (sumados a los que ya tenían, para no romper a otros agentes), y guarda el borrador.
- Las tools con secretos en la URL o el body se guardan con el aviso `omitted`: el turno no las ofrece (ADR-0206 §2). El Coordinador responde 409 `coordinador_inmutable`.
- Una regla con `action: escalate` ya no gasta reintentos correctivos: va directo a la acción de falla del negocio (ADR-0051 §3). Antes solo cambiaba el texto del reintento.
