## Editor de agentes desde plantillas (2026-10-18)

- HU-AOS-24 (#386), adenda de ADR-0139. API para que el dueño cree un especialista desde una plantilla (Ventas,
  Cobranza, Agenda, Suscripciones) y lo publique sin ayuda técnica:
  - `GET /admin/agent-templates` y `POST /admin/agent-templates/{key}/agents`: plantillas con sus superpoderes y
    frases, y el agente con su borrador derivado (sin publicar; 409 `key_taken` o `template_unavailable`).
  - `GET /admin/agent-superpowers`: superpoderes que ofrece el negocio con sus frases.
  - `GET /admin/agent-models`: modelos de la tabla de precios del core, cuáles sirve el proveedor del negocio, el
    modelo BYOK si manda y el costo estimado por 1.000 turnos con el consumo real (sin consumo, sin estimado).
  - `GET /admin/agents/{id}/publish-targets` y `POST /admin/agents/{id}/publish`: D-35, no se publica sin al menos
    una conexión. Publica la versión, instala el agente y construye la release candidata de cada conexión, sin
    activarla. Errores por campo (superpoderes con menos de 3 frases, modelo de otro proveedor, conexiones).
- Sin migración. El límite de agentes del plan Free queda pendiente (no hay métrica).
