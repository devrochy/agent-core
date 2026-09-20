# ADR-0031 — Ciclo de vida de plantillas de WhatsApp: máquina de estados por webhook y resolución de tenant sin RLS

- **Estado:** aceptado
- **Fecha:** 2026-09-20

## Contexto

Se implementa la gestión completa de plantillas de WhatsApp (CRUD, subida de
cabecera multimedia vía Resumable Upload API, sync paginado y máquina de
estados) sobre el esquema `message_templates`/`waba_accounts` ya existente
(migración 0016). Los tres webhooks de Meta relevantes
(`message_template_status_update`, `template_category_update`,
`message_template_quality_update`) traen únicamente el `message_template_id`
numérico de Meta -- nunca el `tenant_id`. Como `message_templates` tiene RLS
habilitado (invariante nº1 de CLAUDE.md), no hay forma de hacer el `SELECT`
inicial sin haber seteado ya `app.tenant_id`: el mismo problema
chicken-and-egg que ya se resolvió para teléfonos/WABAs/Páginas/cuentas de
Instagram (ADR implícito de la migración 0021,
`resolve_tenant_for_phone`/`_waba`/`_page`/`_ig_user`).

## Decisión

Se agrega una cuarta función `SECURITY DEFINER` (migración 0026),
`resolve_tenant_for_message_template(meta_template_id)`, que hace exactamente
lo mismo que sus tres hermanas: un `SELECT tenant_id ... LIMIT 1` exento de
RLS, usado SOLO para resolver el tenant antes de abrir `tenant_scope`. Todo
el resto de la lectura/escritura real de la fila (`core/gateway/
meta_template_webhooks.py`) ocurre DESPUÉS, dentro de `tenant_scope(tenant_id)`,
con una segunda consulta por `meta_template_id` que sí pasa por RLS -- la
función `SECURITY DEFINER` nunca se usa para nada más que resolver el
tenant, nunca para leer datos de negocio directamente.

Como `meta_template_id` es único a nivel de toda la tabla (índice parcial
único `uq_message_templates_meta_id`, ya existente desde la migración 0016),
no hay ambigüedad posible entre qué fila resuelve el tenant y cuál fila
termina mutando el handler: son la misma fila, localizada dos veces por la
misma clave única -- el auditor-multitenant (2026-09-20) señaló este punto
como riesgo de determinismo del `LIMIT 1` sin `ORDER BY`, pero el índice
único ya existente lo vuelve moot (no puede haber dos filas con el mismo
`meta_template_id`).

El auditor también encontró un hallazgo real de severidad media: el
re-query dentro de `tenant_scope(tenant_id)` en los tres handlers filtraba
SOLO por `meta_template_id`, sin repetir `tenant_id` de forma explícita --
se apoyaba únicamente en la RLS activada por `tenant_scope`, rompiendo la
convención de defensa en profundidad que sí respeta el resto del código
nuevo (`meta_templates.py`). Se corrigió agregando
`MessageTemplate.tenant_id == tenant_id` a las tres queries.

El cron de sync (`core/tasks/message_template_sync.py`) sigue el patrón ya
establecido por `meta_subscriptions_health.py`: itera TODAS las
`WabaAccount` conectadas sin filtrar tenant en la query inicial (necesario,
es un cron de sistema), pero cada operación real ocurre dentro de
`tenant_scope(connection.tenant_id)` de esa fila puntual, y una excepción en
una WABA nunca interrumpe el procesamiento de las demás.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Guardar `tenant_id` en texto plano en el payload del webhook y confiar en él | El payload viene de Meta, dato externo -- invariante nº7 de CLAUDE.md, nunca se ejecuta como instrucción de negocio sin verificar contra el estado propio. |
| Reusar `resolve_tenant_for_waba` uniendo por conexión | `message_templates` no tiene `connection_id` directo (cuelga de `waba_account_id`), y agregar el join complica la función sin necesidad -- una función dedicada, simple y con el mismo nombre-patrón que las otras tres, es más legible. |

## Consecuencias

**Positivas:** los tres webhooks de plantillas quedan simétricos con el
resto del pipeline de resolución de tenant ya auditado; no hay superficie
nueva de bypass de RLS más allá de lo ya aceptado para teléfonos/WABAs/
Páginas/Instagram.

**Negativas / lo que aceptamos pagar:** una función `SECURITY DEFINER` más
en el catálogo (cuarta de su tipo) -- mismo costo de mantenimiento que las
anteriores, no es una superficie nueva de riesgo sino una extensión del
patrón ya aceptado.

**Qué haría que revisáramos esto:** si Meta empezara a mandar `tenant_id` (o
un identificador propio del negocio) directamente en el payload del
webhook, dejaría de hacer falta esta función -- se podría resolver el tenant
sin tocar la tabla de negocio en absoluto.

## Impacto en el código

- `alembic/versions/0026_message_template_lifecycle.py`:
  `resolve_tenant_for_message_template`.
- `core/gateway/meta_template_webhooks.py`: los tres handlers.
