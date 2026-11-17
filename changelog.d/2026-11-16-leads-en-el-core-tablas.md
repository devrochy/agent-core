## Leads en el core, parte 1: tablas y copia desde commerce (ADR-0193, HU-AOS-70) (2026-11-16)

- Migración 0146: tablas `leads`, `lead_stage_changes`, `lead_followups` y `lead_settings`, con RLS forzada, un lead vivo por cliente, origen con catálogo cerrado y borrado lógico.
- Comando `agent-core db copiar-leads [--dry-run] [--tenant] [--lote]`: copia los leads de commerce, su historia, sus seguimientos y la config de intake al core, sin tocar los originales. Es idempotente y avisa de los leads que quedarían ocultos.
- Todavía nadie escribe en las tablas nuevas: el cambio de dueño llega en la parte 2.
