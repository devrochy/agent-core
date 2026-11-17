## Interruptor global de la plataforma (HU-ARCH-38, primera parte, 2026-11-16)

- Nuevo interruptor que detiene en ≤ 5 s todos los agentes y los envíos automáticos de todos los negocios, sin desplegar (ADR-0200, D-76). Vive en la tabla de sistema `platform_kill_switch` (migración 0140, sembrada apagada) con su historial `platform_kill_switch_events` (solo inserciones).
- Se opera con `PUT /api/v1/admin/platform/kill-switch` (solo master; un no master recibe 403 y queda registrado) o con `agent-core admin kill-switch --activar --motivo "…"` / `--desactivar`. Activarlo exige motivo; cada cambio avisa a Slack y suma `agentcore_kill_switch_changes_total`.
- Con el interruptor activo: lo que entra se guarda y el turno se cierra en silencio con `kill_switch`; el guardián de HU-ARCH-37 frena todo lo automático con ese motivo; los crons programados que escriben a clientes (`CronMeta.writes_to_customers`) no corren, y a mano corren pero sus envíos se frenan. Lo que escribe una persona sale.
- Falla cerrado: si no se puede leer el interruptor se trata como activo, con evento, métrica `agentcore_kill_switch_read_failures_total` y alerta.
- Al reactivar no se responde en lote: la recuperación de pendientes no reprograma lo que llegó antes de la última reactivación.
- La pausa por agente llega en la segunda parte de HU-ARCH-38.
