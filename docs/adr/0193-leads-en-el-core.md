# ADR-0193 — Los leads viven en el core y Captar leads no depende de comercio

- **Estado:** propuesto
- **Fecha:** 2026-11-13
- **Contexto:** [D-70](https://github.com/devrochy/solvershub-docs/blob/main/02-vision-y-decisiones/decisiones.md#d-70--los-leads-viven-en-el-core-y-captar-leads-no-depende-de-comercio) y [HU-AOS-70](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-44-habilidades-por-agente/hu/HU-AOS-70-leads-sin-comercio.md) ([SPEC-44](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-44-habilidades-por-agente/spec.md), REQ-AOS-24).
- **Enmienda:** [ADR-0190](0190-habilidades-por-agente.md) (decisión 1, «Captar leads», y la alternativa `pack_leads` rechazada porque el modelo vivía en commerce), [ADR-0022](0022-leads-como-vista-computada-de-customer.md) (dónde vive el enriquecimiento del lead) y [ADR-0057](0057-rutas-publicas-e-identidad-de-pack.md) (el intake de leads deja de ser una ruta pública de pack). [ADR-0055](0055-intake-publico-leads-landing.md) ya estaba reemplazado por ADR-0057; queda como registro.
- **Usa:** invariantes 1 (multi-tenant con RLS), 5 (un pack no importa de otro), 7 (lo de fuera es dato) y 8 (sin borrado físico); `core/admin/lead_registry.py`.

## Contexto
Los leads viven en `pack_commerce`: las tablas `pack_commerce_leads`, `pack_commerce_lead_stage_changes` y
`pack_commerce_lead_followups` (migraciones `crm_0001` a `crm_0019`), las tools `crear_lead`, `calificar_lead` y
`actualizar_lead`, los crons `follow_up_leads` y `recordar_seguimientos` y el intake público
`POST /api/pack/pack_commerce/leads/{slug}`. Si un negocio apaga comercio, el intake responde 403 `pack_disabled` y
nadie puede crear leads. El dueño decidió el 2026-11-13 que `solvershub` vende suscripciones, sin Ventas ni catálogo,
pedidos o cotizaciones, y que sus leads deben seguir entrando (D-70, opción A).

## Decisión
El enriquecimiento del lead (etapa, puntaje, interés, seguimiento, motivo de pérdida, valor y origen) y su captura
son del core; ningún pack es dueño del lead.

Detalle:
1. **Modelo.** Tablas del core `leads`, `lead_stage_changes` y `lead_followups`, con las columnas que hoy tienen las de
   commerce, más `origen` (catálogo cerrado: `landing`, `chat_web`, `whatsapp`, `instagram`, `manual`) y `deleted_at`.
   Todas con `tenant_id`, RLS forzada, índice compuesto que empieza por `tenant_id` e índice único parcial
   `(tenant_id, idempotency_key) WHERE deleted_at IS NULL`. ADR-0022 sigue en pie: el `Customer` es la identidad y
   todo cliente es un lead en la vista; la fila de `leads` es su enriquecimiento de embudo, que ahora vive en el core.
2. **Captar leads.** `crear_lead`, `calificar_lead` y `actualizar_lead` son tools del core en el grupo `leads`. Las
   recibe el agente que tenga la habilidad Captar leads (ADR-0190 §2, instalación por agente), de cualquier tipo:
   Ventas, Agenda, Cobranza o Suscripciones. El origen sale del canal de la conversación, no de un argumento del
   modelo. Commerce deja de declararlas.
3. **Lógica del embudo.** La etapa confiable (validación en el servidor e historia con actor), la próxima acción, los
   recordatorios y el resumen diario, el motivo de pérdida y el valor estimado pasan al core sin cambiar de conducta.
   Los crons `follow_up_leads` y `recordar_seguimientos` pasan al catálogo del core. `follow_up_quotes` sigue en
   commerce.
4. **Packs.** Commerce y suscripciones siguen aportando columnas, filtros, facetas y medidas a Clientes por
   `lead_registry` (pedidos, cotizaciones, plan). El estado comercial calculado de ADR-0022 (`con_pedido`,
   `con_cotizacion`…) sigue en commerce. Ningún pack importa el modelo de leads: lo leen y escriben por un servicio
   del core.
5. **Intake público.** El core expone el intake para todos los negocios, tengan o no comercio, con la misma seguridad
   de hoy: cupo por slug e IP en Redis antes de tocar la base, `X-Intake-Secret` comparado en tiempo constante,
   Turnstile que falla cerrado cuando el negocio lo exige, 404 por slug inexistente y deduplicación del cliente por
   correo o teléfono y del lead por `idempotency_key`. La ruta propuesta es `POST /api/public/leads/{slug}` y
   `GET /api/public/leads/intake-config/{slug}` (❓2 de HU-AOS-70, pendiente del dueño). La ruta vieja
   `/api/pack/pack_commerce/leads/{slug}` y su `intake-config` las sirve el core como alias hasta que ninguna landing
   la use; su retiro es otra HU.
6. **Configuración.** Los campos `lead_intake_*` salen de la config de commerce y pasan a la config del negocio de
   Captar leads: la lee la API pública, que no conoce al agente (criterio de ADR-0190 §3). El secreto y la llave de
   Turnstile van cifrados como hoy y se copian sin pasar por logs.
7. **Datos.** Un comando de CLI idempotente y por lotes copia `pack_commerce_leads` y sus tablas hijas a las del core,
   conservando ids, `customer_id`, historia y seguimiento. También importa el CSV de `GET /admin/leads/export.csv`,
   para los leads de producción que se exportan antes del salto. Las tablas de commerce no se borran: quedan sin
   escrituras hasta una HU que las retire con borrado lógico o `tenant purge`.
8. **API del panel.** `/admin/leads` conserva su forma; las columnas de etapa, seguimiento, motivo y valor las aporta
   el provider del core en vez del de commerce. `POST /api/v1/pack/commerce/leads/{customer_id}/seguimiento/hecho`
   pasa a una ruta del core con alias. Se revisa con oasdiff y se regeneran los tipos del panel y la APK.

## Alternativas consideradas
| Opción | Por qué no |
|---|---|
| Commerce instalado en el negocio solo por el grupo `leads`, con catálogo, pedidos y cotizaciones ocultos (D-70, opción B) | Comercio no se apaga, solo se esconde; el panel tendría que ocultar secciones de un pack encendido y el preset seguiría hablando de Ventas |
| Tabla de leads propia en `pack_subscriptions` (D-70, opción C) | Dos modelos de lead y dos embudos; los negocios con Ventas no la usan |
| Un pack transversal `pack_leads` | El lead es el enriquecimiento de un `Customer`, que es del core, y `lead_registry` ya es del core; un pack obligaría a los demás a coordinarse con él por otro registro sin ganar aislamiento. ADR-0190 lo rechazó porque el modelo vivía en commerce; con el modelo en el core tampoco aporta |
| Dejar el intake en commerce y crear otro en suscripciones | Dos endpoints públicos que mantener y asegurar; la landing tendría que saber qué pack tiene cada negocio |

## Consecuencias
**Positivas:**
- Un negocio capta leads con cualquier agente y puede apagar comercio sin perderlos.
- Un solo modelo de lead para todos los negocios; el embudo lead → demo → plan de suscripciones (HU-SUB-23) deja de
  depender de commerce.
- El origen del lead nace como columna (absorbe la parte de datos de HU-CRM-26).

**Negativas / lo que aceptamos pagar:**
- Refactor grande: se mueve lo construido en HU-CRM-18 a HU-CRM-23 y los crons de seguimiento; la HU se parte en
  plataforma y `solvershub` (❓8 de HU-AOS-70).
- Durante la transición conviven las tablas de commerce (sin escrituras) y las del core, y la ruta vieja como alias.
- Cambia la versión de commerce y su hash: hay que reconstruir las releases.
- Orden de despliegue estricto por entorno: intake nuevo con alias → regla del túnel de test → copia de los leads →
  variable de la landing → lead de prueba → recién entonces apagar comercio. Saltarse un paso pierde leads en
  silencio.

**Qué haría que revisáramos esto:**
- Un pack necesita guardar en el lead datos que no caben en `lead_registry` ni en una tabla propia con
  `customer_id` (por ejemplo, un embudo con etapas distintas por tipo de agente).
- El intake público del core recibe tráfico de abuso que el cupo por IP y Turnstile no contienen, y hace falta
  aislarlo en otro contenedor.

## Impacto en el código
- Core: modelos y migración nuevos (`core/db/models.py` o `core/leads/`), servicio de leads, tools del grupo
  `leads`, `core/admin/lead_registry.py` (provider del core), `core/api/routers/leads.py` y `leads_acciones.py`,
  ruta pública nueva y su allowlist en `core/main.py`, `core/tasks/cron_catalog.py`, CLI de copia e importación.
- `packages/pack-commerce`: `models.py` (`Lead`, `LeadStageChange`, `LeadFollowup` sin escrituras), `skills.py`
  (`LeadSkill` fuera), `module.py` (grupo `leads` fuera), `api.py` (intake fuera), `config.py` (`lead_intake_*`
  fuera), `jobs.py`, `etapas.py`, `seguimiento.py`, `valor.py`, `recordatorios.py`, `providers.py`, `admin_api.py`;
  versión nueva y `packages/pack-versions.lock.json`.
- `core/presets/plantillas/agenda.json` (`acciones_opcionales` de `pack_commerce` → grupo `leads` del core) y
  plantilla de Suscripciones con Captar leads opcional.
- `docs/openapi.json`, modelo C4 (el intake cambia de dueño), tipos del panel y la APK; en la landing,
  `LEAD_WEBHOOK_URL` y `LEAD_WEBHOOK_BASE`.
- Tests: intake (secreto, Turnstile, cupo, slug inexistente, deduplicación), tools con y sin comercio, copia
  idempotente, aislamiento cross-tenant y test estático de que ningún pack importa el modelo de leads.
