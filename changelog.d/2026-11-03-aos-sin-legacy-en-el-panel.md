## Agent OS como único motor desde el panel (2026-11-03)

- El panel del master ya no puede devolver un negocio a `legacy`: `PUT` y `POST …/agent-os/simular`
  responden 422 `agent_os_orden` si el cambio incluye el paso `runtime_legacy`. Un negocio que ya está
  en `legacy` sigue pudiendo pasar a v3, y repetir el mismo estado sigue siendo «sin cambios».
- El rollback de emergencia queda solo por consola: `agent-core db migrar-a-v3 --volver --tenant <slug>`.
