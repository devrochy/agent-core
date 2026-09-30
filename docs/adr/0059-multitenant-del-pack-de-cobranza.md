# ADR-0059 — Aislamiento multi-tenant del pack de cobranza (1 tenant = 1 propietario)

- **Estado:** aceptado
- **Fecha:** 2026-09-24

## Contexto

`app-cobranza` es single-tenant, con jerarquía Admin → Propietario → Gestor de
cartera y FK físicas. Al portarlo como pack multi-tenant hay que decidir qué es
un tenant: ¿la plataforma con propietarios como sub-tenants, o un propietario por
tenant? El pack debe cumplir el invariante nº1 (toda tabla de negocio con
`tenant_id` + RLS).

## Decisión

**1 tenant = 1 propietario/prestamista.** El `propietario` es la raíz del pack
(sus carteras); `gestor` (gestor de cartera) es un **usuario interno del pack**
(no admin del core); `cartera`, `cliente`, `prestamo`, etc. cuelgan del tenant.
Toda tabla del pack lleva `tenant_id` + RLS + FORCE RLS + índice que empieza por
`tenant_id`; las FK al core son **lógicas** (UUID sin constraint). El
`propietario` del pack **no** es un tenant del core.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| 1 tenant = plataforma, propietarios = sub-tenants | RLS de dos niveles, roles duplicados; más complejo sin necesidad en v1 |
| Un tenant por gestor | Fragmenta la cartera del propietario; modelado incorrecto |
| Mantener sin `tenant_id` | Viola el invariante nº1 |

## Consecuencias

**Positivas:** cumple el invariante nº1; el alta de un prestamista es
`tenant create` + habilitar el pack; aislamiento por RLS estándar.

**Negativas / lo que aceptamos pagar:** un propietario con varias "oficinas" no
se modela como tenants separados (se modela dentro del pack); no hay migración de
datos de `app-cobranza` en v1 (el pack arranca en limpio).

**Qué haría que revisáramos esto:** que un cliente necesite varios propietarios
bajo una misma cuenta con datos compartidos (→ reconsiderar sub-tenants) o que
aparezca una fuga cross-tenant (→ auditoría `auditor-multitenant`).

## Impacto en el código

- `packages/pack-cobranza/pack_cobranza/models.py` y migraciones (RLS/FORCE).
- `core/onboarding.py` (habilitar el pack al alta).
- `docs/MULTITENANT.md`.
