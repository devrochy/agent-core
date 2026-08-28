# ADR-0017 — Copy al cliente es CONFIG; los playbooks son instrucciones

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

La migración de CULTO mezclaba dos clases de texto: **instrucciones** que el modelo debe
seguir (nunca inventar precios, confirmar antes de crear un pedido) y **copy** que el cliente
ve (follow-ups, confirmación de pedido, estados). Tratarlos igual produce fugas: los crons y
las tools no pueden leer un playbook, y el ensamblador solo sustituye 3 placeholders
(`{assistant_name}`, `{business_description}`, `{language}`) — un `FOLLOW_UP_QUOTE_MESSAGE`
con `{total}` no cabe en un `.md`.

## Decisión

- **Instrucciones al modelo** (cuándo/cómo actuar) → playbooks `SKILL.md` versionados en el
  pack, referenciados en `AgentSpec.playbooks`.
- **Copy al cliente** (mensajes de follow-up, confirmación, estados, transferencia) → campos
  de configuración del pack (`CRMConfig` o `templates jsonb`), editables por tenant en el
  panel. El copy es dato de negocio, no contenido de prompt.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Todo a playbooks | Los crons/tools no leen playbooks; placeholders limitados; el copy debe poder editarse sin release |
| Todo hardcodeado | Sigue siendo prompts/copy en código; un tenant #2 querría su propio copy |

## Consecuencias

**Positivas:** el copy se edita por tenant sin release; las instrucciones siguen versionadas
en el pack; clasificación alineada con el inventario (CONFIG vs PACK).

**Negativas / lo que aceptamos pagar:** campos de configuración nuevos en el schema del pack
(cada uno necesita migración); el copy por defecto del pack es el "estándar" y cada cliente
lo ajusta.

**Qué haría que revisáramos esto:** que un playbook necesite variables de turno o que el copy
deba cambiar según el estado del modelo — señales de que la frontera está mal trazada.

## Impacto en el código

`plugins/pack_commerce/pack_commerce/config.py` (campos de copy), `skills.py`/`jobs.py`
(leer copy de config), `plugins/pack_commerce/playbooks/` (nuevo), `AgentSpec` de CULTO.