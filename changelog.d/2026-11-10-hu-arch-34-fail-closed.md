## En v3 solo corre lo que concede la versión del agente (2026-11-10)

- HU-ARCH-34, parte 2. En un negocio v3, una acción sin binding, sin declarativa y sin procedimiento declarado que la incluya se niega y deja el evento `tool_sin_binding`. Tampoco se ofrece.
- Sin agente en foco se niega todo: un negocio v3 sin release, sin conexión o con release sin foco.
- Legacy, la bandera `AOS_CONNECTION_ROUTING` apagada y el Centro de pruebas siguen abiertos, con el evento `tool_sin_binding_legacy`.
- Antes de desplegar: `agent-core db reporte-bindings` en 0 y pasado `episode.idleMinutes`.
