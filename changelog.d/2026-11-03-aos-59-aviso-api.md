## Aviso de referencias retiradas: API de solo lectura (2026-11-03)

- HU-AOS-59 (SPEC-26), entrega API (ADR-0184, adenda). Solo avisa; no escribe ni corrige.
- Master: `GET /api/v1/admin/platform/tenants/{tenant_id}/retired-references` devuelve lo mismo que `auditar-referencias --tenant` (agentes, versiones, rutas, códigos, releases activas afectadas, borradores y conexiones aparte). Solo `master`.
- Dueño: `GET /api/v1/admin/agents/retired-references` y `GET /api/v1/admin/agents/{id}/retired-references` devuelven, por agente, el aviso en palabras simples (qué ya no existe y qué hacer, sin códigos como `accion_inexistente`), aislado por negocio. Texto en `core/agents/aviso_retiros.py`.
- Publicar un agente cuyo borrador nombra algo retirado ahora da 422 `invalid_spec` con ese mismo texto por campo y `details.retired_references`, antes de publicar. Para un procedimiento retirado cambia el comportamiento: antes la versión se publicaba (200) y el build de la release fallaba después; ahora es 422 antes de publicar. La fixture de ventas de `test_agentes_versionados` ya no nombra procedimientos retirados.
- `docs/openapi.json` regenerado.
