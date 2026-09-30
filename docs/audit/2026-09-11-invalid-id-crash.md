# Auditoría de aislamiento multi-tenant — sesión 2026-09-11 (fix de IDs inválidos)

**Fecha:** 2026-09-11
**Estado:** incidente real en producción de test — `pack_commerce`/`pack_payments`
quedaron desactivados globalmente para todos los tenants
**Criterio:** `CLAUDE.md` (invariantes) + RLS por tenant

**Alcance:** el fix que valida IDs de tipo UUID en las tools de
`pack-commerce` antes de consultarlos, para que un argumento inventado por
el LLM no propague una excepción que dispare `auto_disable` globalmente.

## Hallazgos

| severidad | archivo:línea | problema | corrección sugerida | estado |
|---|---|---|---|---|
| alta | `core/mcp/host.py` + `core/plugin_manager.py` (`PluginRecord.status` sin `tenant_id`) | Causa raíz real del incidente: CUALQUIER excepción no manejada en un tool handler dispara `auto_disable` GLOBAL (todos los tenants), no solo el vector de UUID mal formado que motivó este fix. | Hacer `auto_disable`/`PluginRecord` scoped por `(tenant_id, plugin_id)`, o al menos degradar a error solo para el tenant de la invocación cuando el `ToolContext` lo resuelve. Toca el contrato de plugins — amerita un ADR aparte. | Documentado en `docs/ROADMAP.md` como deuda de arquitectura, no resuelto en esta tanda (fuera de alcance de un fix puntual). |
| media | `pack_commerce/skills.py::calificar_lead` | `int(arguments.get("score", 0))` sin `try/except` — un `score` no numérico repetía el mismo apagón global. | Envolver en `try/except`. | **RESUELTO.** |
| media | `pack_commerce/skills.py::generar_cotizacion` / `agregar_item_pedido` | Un elemento de `items` que no es un dict (string/null/número) lanzaba `AttributeError` no manejado. | Validar `isinstance(item, dict)` antes de `.get(...)`. | **RESUELTO** (validación movida antes de abrir la sesión en `agregar_item_pedido`, para no depender de una conexión real a Postgres). |
| media | `pack_commerce/skills.py::crear_pedido` | `customer_data` sin validar tipo — un valor no-dict rompía `_validate_delivery_data`. | Validar `isinstance(customer_data, dict)` antes de usarlo. | **RESUELTO.** |
| baja | `pack_commerce/skills.py::_parse_uuid` | Vive en `skills.py`, se reimplementaría si otro pack necesita el mismo patrón. | Si aparece un tercer caso, mover a un helper compartido. | No se toca ahora — solo un pack lo necesita hoy. |

## Sin hallazgos de severidad alta de fuga entre tenants

`_parse_uuid` y las validaciones de tipo solo agregan un `return` temprano
cuando el argumento ni siquiera tiene la forma esperada — no tocan ningún
filtro de `tenant_id`/RLS existente. Confirmado que el aislamiento real de
`pack_commerce_*` sigue siendo RLS vía `set_config('app.tenant_id', ...)`
(`core/db/session.py`), sin cambios. `pack-payments`/`pack-calendar`
revisados: no tienen hoy este patrón (no reciben IDs de tipo UUID como
argumento de tool contra su propio ORM).
