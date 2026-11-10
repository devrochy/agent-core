## Reporte de acciones sin conceder en v3 (2026-11-09)

- HU-ARCH-34, parte 1, sin cambio de conducta. `agent-core db reporte-bindings` lista por negocio y agente lo que una release activa o canary debe conceder y no concede.
- Los agentes nuevos desde un preset llevan binding de las acciones esenciales del core.
- Enmiendas de ADR-0019 y ADR-0118: en v3 una acción sin concesión no corre, y declarar un procedimiento concede sus acciones.
