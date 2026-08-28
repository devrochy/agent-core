# MONOLITH-MODEL · Los plugins son código embebido, no servicios

> **SUPERSEDIDO por ADR-0001** (multi-tenant pooled + RLS). Este documento describe el
> modelo single-tenant heredado ("un negocio = un despliegue"). El framework final conserva
> los plugins embebidos in-process (ADR-0005), pero el aislamiento entre clientes se resuelve
> con `tenant_id` + RLS y topologías pooled/silo/dedicado, no con un despliegue por cliente.
> Ver `docs/adr/0001-multitenant-pooled-rls.md`.

Este documento existe para eliminar una ambigüedad de raíz: **en agent-core un plugin
NUNCA es un microservicio, un contenedor aparte ni un endpoint remoto.**

## El modelo

```
UN asistente = UN despliegue = UN proceso principal con TODO el código adentro

┌─────────────────────────────────────────────────┐
│  Proceso: uvicorn core.main:app                 │
│                                                 │
│  core/               (el framework)             │
│  pack_commerce/     (pip install -e → embebido)│
│  pack_calendar/(pip install -e → embebido)│
│                                                 │
│  Ejecutar una tool = llamada de función Python  │
│  await tool.handler(arguments, context)         │
│  → CERO red · CERO serialización · CERO timeout │
└─────────────────────────────────────────────────┘
        + procesos hermanos: celery-worker, celery-beat (mismo código instalado)
```

## Por qué (decisión explícita del proyecto)

| Criterio | Plugins como servicios (rechazado) | Plugins embebidos (elegido) |
|---|---|---|
| Latencia por tool | +5-50ms de red por llamada × ~3 tools/turno | ~0ms (stack de Python) |
| Punto de falla | Cada plugin caído rompe turnos | PluginError → auto-disable, el asistente sigue |
| Operación | N contenedores, N healthchecks, service discovery | docker-compose up y ya |
| Transacciones | Distribuidas (sagas, outbox…) | Una sesión SQLAlchemy normal |
| Costo de VPS | Proporcional a N servicios | Un VPS de 2GB corre todo |

Un asistente conversacional tiene presupuesto de latencia estricto (el cliente está
mirando el "escribiendo…"). Gastar ese presupuesto en red interna no tiene retorno.

## Cómo se logra el aislamiento SIN procesos separados

El miedo clásico al monolito es "un módulo roto tumba todo". agent-core lo resuelve
en software, no en infraestructura:

1. **Frontera de import estricta** (PLUGIN-CONTRACT.md §13): los plugins solo importan
   la API pública del core. El orchestrator no conoce ningún plugin por nombre.
2. **PluginError → auto-disable**: cualquier excepción dentro de una tool desactiva
   ese plugin, notifica al panel, y el turno continúa con un resultado de error que
   el LLM sabe manejar ("discúlpate y ofrece un asesor").
3. **Guardrails fail-open**: un guardrail roto se ignora y se loggea (no silencia al bot).
4. **Eventos fire-safe**: un subscriber que lanza excepción se loggea y no corta la cadena.
5. **Cron guard**: las tareas de un plugin inactivo se saltan solas.

## Qué significa "instalar un plugin"

```
agent-core plugin install pack-commerce
  1. git clone (o carpeta local) → plugins/pack_commerce/
  2. pip install -e plugins/pack_commerce   ← EL CÓDIGO ENTRA AL PROCESO
  3. migraciones propias del plugin (árbol Alembic separado)
  4. wizards (datos + exposición) + actualización del SDD
  5. docker compose restart core-api          ← el proceso renace CON el plugin adentro
```

El entry_point de setuptools es solo el mecanismo de **descubrimiento** — no levanta
ningún proceso. `plugin_manager.scan()` hace `ep.load()` y obtiene una clase Python.

## Multi-asistente = multi-despliegue (no multi-tenant)

Lección directa de agent-sale (ADR-032: diseñaron multi-tenant con RLS y lo
desmontaron 36 migraciones después): **un negocio = un despliegue completo** con su
propia BD, su Redis y sus plugins. Aislar clientes por infraestructura es más simple,
más seguro y más barato de operar que aislarlos por filas.

¿Necesitas 10 asistentes? 10 clones del repo, 10 `agent-core init`, 10 docker-compose.
El SDD de cada uno documenta su configuración exacta.
