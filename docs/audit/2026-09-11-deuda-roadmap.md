# Auditoría de aislamiento multi-tenant — sesión 2026-09-11 (cierre de deuda ROADMAP)

**Fecha:** 2026-09-11
**Estado:** Sprint 4 · corrida sobre la rama `fix/roadmap-deuda-tier-a`
**Criterio:** `CLAUDE.md` (invariantes) + RLS por tenant

**Alcance:** el diff completo de la rama (cierre de 6 filas de "Deuda
registrada" del ROADMAP: bug de costo $0 con Anthropic, clave Redis de
idempotencia, `plugin scaffold`, eviction de `turn_gate`, retiro de
`llm_usage`, activación de plugins en `PluginManager`).

## Hallazgos

| severidad | archivo:línea | problema | corrección sugerida | estado |
|---|---|---|---|---|
| media | `core/plugin_manager.py:186,192` (antes del fix de esta corrida) | `module.config` sigue siendo un atributo de instancia compartido entre tenants (`self._modules[id]` es singleton de proceso); el fix del diff hace que se reescriba en cada `activate()`, no solo la primera vez. Hoy no hay explotación activa (ningún pack lee `self.config` fuera de `on_activate()`), pero `docs/PLUGIN-CONTRACT.md` instruía a futuros autores de packs a usar `self.config` en runtime sin aclarar el límite. | Documentar el límite real en `docs/PLUGIN-CONTRACT.md` y no sobre-representar el hallazgo como "cerrado". | **RESUELTO: `docs/PLUGIN-CONTRACT.md` § 6 reescrito con la advertencia explícita (usar `plugin_manager.load_config()` tenant-scoped en tools, nunca `self.config`); docstring de `tests/test_plugin_manager_activation.py` corregido de "cerrada" a "parcial".** |
| media | `core/plugin_manager.py::activate()` (rama de primera activación) | Race preexistente: dos tenants activando el MISMO plugin por primera vez casi a la vez pasan ambos el chequeo `plugin_id in self._active` (False para los dos) y compiten por `mcp_host.register_skill()`. El perdedor rompe con "Skill duplicada"; su cleanup (`mcp_host.remove_plugin`) borra TAMBIÉN las tools que el ganador acababa de registrar con éxito, y pisa `PluginRecord.status` global a `"error"` por un problema de timing ajeno a la config de nadie. | Serializar la primera activación con un `asyncio.Lock()` por `plugin_id`. | **RESUELTO: `PluginManager._activation_lock()` + todo el cuerpo de `activate()` bajo `async with`. Reproducido en vivo sin el lock (confirmado: el ganador queda con 0 skills registradas) y verificado que con el lock no pasa — `tests/test_plugin_manager_activation.py::test_concurrent_first_activation_does_not_wipe_out_winner`.** |
| baja | `core/gateway/webhooks.py` (`webhook_signature_trace`, marcado `# TEMP-DIAG`) | Logging de diagnóstico puntual (E2E de "yopoldo", sesión previa) sigue en el flujo de producción: imprime `phone_id`, prefijo de la firma HMAC y `app_secret_len` en cada webhook entrante. No es PII de cliente final, pero es diagnóstico temporal que expone metadata de la firma. Preexistente, no introducido por este diff. | Bajar a `log.debug` o quitar si el E2E ya no lo necesita. | Documentado en `docs/ROADMAP.md` (deuda nueva, prioridad baja) — no se tocó en esta corrida, fuera del alcance del diff auditado. |

## Sin hallazgos de severidad alta

Los tres mecanismos de aislamiento tocados por el diff (`ContextVar` de
tenant vía `core/shared/tenant.py`, clave Redis de idempotencia con
`tenant_id`, semáforos por tenant en `turn_gate.py`) están correctamente
implementados: `tenant_id` se resuelve antes de construir cualquier clave o
consultar el contador, y ninguno mezcla estado entre tenants.
