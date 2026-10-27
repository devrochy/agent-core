## El General enlaza a cada especialista y, con bandera, deja sus poderes (2026-10-26)

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
