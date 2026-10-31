# ADR-0181 — Etapa del lead confiable y con historia: una sola puerta, reglas por actor y registro de cambios

- **Estado:** propuesto (HU-CRM-18, SPEC-33 tanda 3, D-57 de solvershub-docs).
- **Fecha:** 2026-10-30
- **Contexto:** `pack_commerce` (leads de venta). **Enmienda:** ADR-0149 (el hook de edición de la ficha
  ahora pasa por la puerta de la etapa). **Usa:** ADR-0056 (`lead_registry` y su línea de tiempo),
  ADR-0147 (línea de tiempo), invariantes 1 (tenant + RLS forzada), 5 y 7.

## Contexto

La etapa del lead (`pack_commerce_leads.status`) se escribía en cuatro sitios sin reglas comunes:

- `actualizar_lead` guardaba `str(arguments["status"])` sin validarlo en el servidor (solo el esquema de la
  tool trae el `enum`, que el modelo puede ignorar).
- `calificar_lead` forzaba `calificado` aunque el lead ya estuviera `contactado`, `convertido` o `perdido`.
- El hook de la ficha (`_escribir_lead`) y el seguimiento automático (`follow_up_leads`, que ponía
  `contactado`) escribían directo.
- Ningún cambio dejaba rastro: no se sabía quién movió el lead ni cuándo.

Todo lo que se construye encima (pestañas con contador, embudo, conversión, tiempo por etapa) calcula sobre
la etapa.

## Decisión

1. **Una sola función.** `pack_commerce.etapas.cambiar_etapa(session, lead, nueva, actor=…,
   idempotency_key=…)` es el único código que asigna `Lead.status`. `tests/test_pack_commerce_etapa_unica.py`
   lo vigila con AST (cualquier asignación a `<…lead…>.status` fuera de `etapas.py` falla). Crear un lead con
   `Lead(status=…)` es un alta, no un cambio, y no pasa por ella.
2. **Etapas válidas:** `nuevo`, `calificado`, `contactado`, `convertido`, `perdido` (`ETAPAS`; `ESTADOS_LEAD`
   del panel se afirma igual). La entrada se normaliza (`strip().lower()`); cualquier otra lanza
   `EtapaInvalida`, cuyo mensaje lista las válidas. La tool `actualizar_lead` responde
   `{"error": "…", "etapas_validas": […]}` y **no guarda nada** del llamado (la etapa se valida antes que el
   resto de los campos). No se agrega CHECK a `pack_commerce_leads.status`: hoy puede haber texto libre
   anterior a esta HU; salir de él es válido y la historia lo conserva como etapa anterior.
3. **Reglas por actor** (`agente`, `persona`, `sistema`):
   - Misma etapa → no hace nada y no deja registro.
   - **Salir de `perdido` (reabrir) es solo de una `persona`** (panel o APK). Para agente y sistema lanza
     `TransicionNoPermitida`; la tool responde `{"error": "… solo una persona …", "etapa_actual": "perdido"}`.
   - `calificar_lead` solo mueve a `calificado` un lead `nuevo` (`etapas.calificar`); el score siempre se
     actualiza. Devuelve la etapa real en que queda, no `calificado` fijo.
   - `actualizar_lead` es una orden explícita del agente: puede mover a cualquier etapa válida salvo salir de
     `perdido`, **incluido retroceder** (p. ej. `convertido → contactado` si el cliente se arrepiente).
     La HU solo prohíbe el retroceso de `calificar_lead` y la reapertura; endurecer más es otra decisión.
   - El seguimiento (`sistema`) solo mueve `calificado → contactado` y sólo si el lead sigue `calificado`
     al terminar el envío (si una persona lo movió mientras tanto, no lo pisa).
4. **Historia.** Tabla nueva `pack_commerce_lead_stage_changes` (`tenant_id`, `lead_id`, `customer_id`,
   `from_stage`, `to_stage`, `actor`, `idempotency_key`, `changed_at`), RLS forzada, CHECK en `to_stage` y
   `actor`, índice único parcial `(tenant_id, idempotency_key)` y índice `(tenant_id, customer_id,
   changed_at, id)` para la línea de tiempo. `customer_id` se copia del lead porque un cliente puede tener
   varios leads. El registro, `status`, `stage_changed_at` y `last_activity_at` se escriben en la misma
   transacción, con el lead bloqueado (`SELECT … FOR UPDATE`).
5. **Idempotencia.** Con `idempotency_key`, si ya hay un registro con esa clave el cambio no se vuelve a
   aplicar (ni deshace un cambio posterior). Claves: `agente:<tool>:<idempotency_key de la tool>` y
   `sistema:seguimiento:<lead>:<fecha>`. El hook de la ficha no trae clave: su reintento es idempotente por
   el criterio «misma etapa → sin registro».
6. **«Desde cuándo».** `pack_commerce_leads.stage_changed_at` (nullable, **sin backfill**). El resolver del
   pack entrega `etapa_desde` (ISO UTC) y `etapa_desde_aproximada`: para un lead anterior a esta HU
   (`stage_changed_at` nulo) usa `last_activity_at` y la marca aproximada. No se inventan cambios pasados.
   Viajan en `extras` de la ficha (`GET /admin/leads/{id}`), sin cambiar el contrato OpenAPI.
7. **Línea de tiempo.** El pack aporta eventos `kind="etapa"`, fuente `pack_commerce.lead_etapa`, con título
   «Etapa: Calificado → Contactado» y detalle «Lo hizo el agente | una persona | el sistema». No toca el core.
8. **Migración `crm_0015`**, aditiva: `SET LOCAL lock_timeout`, columna nullable sin default y tabla nueva.
   El `downgrade` se niega si hay cambios registrados (borraría la historia) y si no, quita tabla y columna.

## Consecuencias

- Un modelo que mande una etapa inventada recibe el error con las válidas; puede reintentar o abandonar el
  lead. Los casos de replay existentes (`27-seguimiento-calificar-lead`) siguen verdes: no cambian prompts,
  esquemas ni descripciones de las tools, así que no hay que regrabar.
- El actor `persona` es una categoría: no se guarda qué administrador (el hook `LeadEdicion.escribir` no
  recibe al usuario). Si hace falta auditar por persona, es una ampliación del hook (ADR-0149).
- Los demás escritores del lead (intake, borrador automático, escenarios de prueba) solo crean leads con su
  etapa inicial y no generan registro.
- Un cliente con varios leads cambia el más reciente (`_ultimo_lead_del_cliente`), igual que hoy.
- HU-CRM-21 y HU-CRM-23 agregan columnas al lead: sus migraciones encadenan después de `crm_0015`.
