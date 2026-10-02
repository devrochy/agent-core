---
name: agentcore-conventions
description: >
  Convenciones y límites de capa de agent-core (Python). Usar siempre que se escriba o
  modifique código en core/, packages/ o cli/, cuando haya duda sobre dónde va un archivo, o
  cuando se decida si algo es core, pack, configuración o plugin del tenant.
---

Las invariantes de `CLAUDE.md` mandan sobre esta skill. Aquí está cómo se aplican en el
código real.

## Dónde va cada cosa

| Va al… | Si… |
|---|---|
| **core** (`core/`) | lo necesitan ≥2 verticales, no menciona el dominio del cliente, su ausencia rompe el runtime |
| **pack** (`packages/pack-<x>/`) | nombra una entidad de nicho, sus tablas llevan prefijo natural, un tenant podría no tenerlo |
| **configuración** | es un número, texto, umbral o booleano que el cliente cambiaría solo (`config_schema` del pack, AgentSpec, `app_settings`) |
| **artefacto del tenant** (`TENANTS_ROOT/<slug>/`) | es dato o documentación de UN cliente: catálogo, voz de marca, override de playbook, evals |
| **plugin del tenant** | solo un cliente lo pide y no es generalizable (Fase 2, SPEC-22) |

Ante la duda: configuración primero, pack después, core al final. Subir algo al core es
difícil de revertir.

## Mapa del repo

- `core/`: monolito modular (gateway, orchestrator, mcp, db, api `/api/v1`, billing,
  tasks…).
- `packages/pack-<x>/`: un pack instalable con entry point `agent_core.plugins`
  (`pack_<x> = "pack_<x>.module:<X>Module"`). Contiene `pack_<x>/` (código),
  `alembic/` (su árbol), `flow.json` y `playbooks/`.
- `packages/channel-*`: adaptadores de canal con entry point `agent_core.channels`.
- `packages/contracts` (`agentcore_contracts`): tipos pydantic congelados; cambiarlos
  exige ADR.
- `cli/`: comando `agent-core` (Typer).
- **Sin código nuevo** en `apps/*`, `packages/{core-crm,core-runtime,channel-whatsapp}`
  (stubs, HU-REP-01) ni en `plugins/` (heredado).
- `core/admin/templates/` (panel SSR) está en retiro (ADR-0076): las vistas nuevas van al
  repo `solvershub-panel` y consumen `/api/v1`.

## Límites de capa

- Un pack importa del core, **nunca de otro pack**. Coordina por los registros del core
  (`payment_registry`, `subscription_registry`) y por eventos (`event_bus.emit`,
  `host.subscribe`). La única excepción deliberada es el CLI
  (`cli/commands/tenant.py::seed_pack_plugins`).
- Las tools reciben `ToolContext` y acceden a datos solo con `get_session()` y
  `get_current_tenant_id()`. A la red, solo con `context.http` (`ScopedHttp`). Nunca el
  pool de Postgres ni `os.environ`.
- La configuración del negocio sale del `config_schema` del pack por tenant.
  `get_settings()` dentro de un pack solo para valores de plataforma (URL pública,
  secreto de firma), y nunca dentro de una tool.
- Los adaptadores de canal normalizan el mensaje entrante y no conocen el dominio.
- Toda ruta nueva de `/api/v1` se refleja en `docs/openapi.json`
  (`uv run python scripts/export_openapi.py`).

## Nombres

| Qué | Convención | Ejemplo |
|---|---|---|
| Pack | id `pack_<x>`, carpeta `packages/pack-<x>`, módulo `pack_<x>` | `pack_subscriptions` |
| Tabla de core | plural, sin prefijo | `agent_specs`, `webhook_events` |
| Tabla de pack | `pack_<x>_<entidad>` en plural, en el idioma que ya use el pack | `pack_subscriptions_charges`, `pack_cobranza_pagos` |
| Migración | core `00NN_<slug>.py` (`"00NN"`); pack `NNNN_<slug>.py` (`"pack_<x>_NNNN"`) | skill `db-migration` |
| Tool | español, `snake_case`, `<verbo>_<objeto>`, prefijo del pack si puede chocar | `consultar_inventario`, `sub_link_pago` |
| Evento | `<entidad>.<participio>` como constante `ON_*` en `core/shared/events.py` | `payment.confirmed` |
| Escenario de eval | `evals/golden_scenarios/NN-<tema>.json` | `07-venta-consultar-inventario.json` |

Nunca se renombran: los IDs `pack_*`, la BD `agent_core`, los streams de Redis ni las
cookies del panel clásico.

## Prohibiciones

- Prompts o instrucciones de comportamiento en `.py`: van al AgentSpec o a
  `playbooks/*.md`. La `description` de una tool sí va en código.
- Montos, precios o referencias de pago decididos por el LLM: los arma el servidor.
- Excepciones de negocio que escapan de una tool: hoy desactivan el pack para el tenant
  (ADR-0024). Se devuelven como `{"error": …}`.
- Tablas de negocio sin `tenant_id` + RLS forzada + policy, o con borrado físico.
- Caches o variables de módulo con datos de un tenant sin la clave del tenant.
- Logs con contenido de mensajes o datos personales.
- Credenciales en el repo, en `.env` versionados o en documentos.

## Antes de terminar

`uv run ruff check core cli scripts alembic evals tests packages` y `uv run pytest -q`
(más `AGENT_CORE_INTEGRATION=1 uv run pytest tests/integration -q` si toca BD o API). Antes
de cerrar la tanda, el subagente `auditor-multitenant`.
