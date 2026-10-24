## Agent OS: runbook de migración y comando del *contract* (HU-AOS-15)

- **Runbook** `docs/runbooks/migracion-agent-os.md`: orden de banderas, backfills, `migrar-a-v3`, enrutamiento, pass^4 antes de encender `aos_procedures`, apagado en orden inverso, el *contract* y el checklist de cierre.
- **`agent-core db contract-aos`** (`core/releases/contrato.py`, adenda 2026-10-24 de ADR-0115): cierra el expand/contract de la conversación por conexión. En seco por defecto; `--aplicar --confirmo-sin-vuelta-atras` aplica solo si todo se cumple (`AOS_CONNECTION_ROUTING`, todos los negocios en `v3`, ninguna conversación sin conexión, 2 releases estables sin rollback). Deduplica sin cerrar, crea el índice único, `connection_id` NOT NULL, CHECK de `core_decisions` y valida lo `NOT VALID`. **No es una migración de Alembic: no corre sola al desplegar.**
- `docs/despliegue-coolify.md`: fila de `AOS_CONNECTION_ROUTING`.
