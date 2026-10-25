## Playbook de suscripciones: `vencida` ya no es «uso futuro» (pack_subscriptions 0.3.0) (2026-10-24)

El playbook decía que `vencida` era de uso futuro, pero el cron `marcar_vencidas` ya la usa (cargo impago pasados los días de gracia, o Free que termina su prueba sin pasar a un plan pago). El texto ahora lo explica: no genera cargos nuevos y pagar la reactiva a `activa`.

- Cambia el contenido de `playbooks/suscripciones.md`, así que sube la versión del pack a **0.3.0** y se regenera `packages/pack-versions.lock.json` (ADR-0118 §1).
- Sin cambios de código, de esquema ni de API.
