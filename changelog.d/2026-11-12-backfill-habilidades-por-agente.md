## Backfill de habilidades por agente (2026-11-12)

- `agent-core db habilidades-por-agente --todos|--tenant <slug> [--aplicar]` (HU-AOS-65, ADR-0190): instala en cada agente especialista existente las habilidades de su plantilla, sin borrar ningún negocio. Idempotente; dry-run por defecto.
