# ADR-008 · Single-tenant por despliegue + SDD vivo generado por CLI

**Estado**: SUPERSEDED por [ADR-0001](../adr/0001-multitenant-pooled-rls.md) · **Fecha**: 2026-08-19

> Este ADR describía el modelo single-tenant heredado ("un negocio = un despliegue").
> El framework actual es multi-tenant pooled con RLS: cada cliente es un tenant en
> el mismo despliegue (ADR-0001), y los artefactos del tenant viven en
> `TENANTS_ROOT/<slug>/` — no en `sdd/`. Se conserva como historia; no refleja el
> framework vigente (ver `docs/MULTITENANT.md`).

## Contexto

agent-sale nació multi-tenant con RLS y lo desmontó en la migración 0036 (ADR-032):
nunca operó más de un negocio y el aislamiento por filas fue fricción pura. Además,
la documentación de proyectos muere porque se escribe a mano y nadie la actualiza.

## Decisión

1. **Un negocio = un despliegue completo** (BD, Redis y plugins propios). La tabla
   `app_settings` es un singleton. Multi-asistente = multi-clon del repo.
2. **El SDD es un artefacto generado**: el estado vive en `sdd/.state.json`
   (escrito por `init`, `plugin install`, `configure`) y los documentos
   `sdd/*.md` se REGENERAN desde plantillas Jinja2 (`agent-core sdd update`).
   Regla: nunca editar los .md a mano.
3. `sdd/skills-config.md` es el punto de entrada para IA: contexto completo del
   asistente + reglas inviolables + mapa de archivos, para que Claude/OpenCode/DeepSeek
   personalicen el asistente sin romper invariantes.

## Justificación

- Aislar clientes por infraestructura es más simple, más seguro y más barato de
  operar que RLS (evidencia: las 36 migraciones que le costó a agent-sale).
- Documentación generada desde el estado real = nunca miente. La documentación
  manual de agent-sale requirió correcciones explícitas en MASTER_PLAN_V2 porque
  los README quedaron desactualizados.

## Consecuencias

- (+) Cero lógica de tenancy en queries, prompts y rate limiting.
- (+) El SDD siempre refleja el asistente real; sirve de contexto de calidad para LLMs.
- (−) Operar 50 asistentes = 50 despliegues (aceptado: a esa escala se construye
  tooling de flota, no multi-tenancy en el core).
