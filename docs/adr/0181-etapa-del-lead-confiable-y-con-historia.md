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

## Enmienda 2026-11-02 — Próxima acción y fecha de seguimiento (HU-CRM-21)

Lo que decide la HU; el ADR sigue siendo uno solo porque el seguimiento cuelga del mismo lead y de la misma
puerta de edición (ADR-0149).

1. **Dos columnas en el lead** (`crm_0018`, aditiva): `next_action_at timestamptz` y `next_action_note text`
   (CHECK ≤ 200), índice parcial `(tenant_id, next_action_at) WHERE next_action_at IS NOT NULL`. La fecha va
   siempre en UTC. Una nota sin fecha se rechaza («Elige la fecha del seguimiento»); quitar la fecha quita la nota.
2. **Tipo `fecha` en el hook de edición.** `LeadCampoEditable.tipo` y `LeadEditable.tipo` admiten `"fecha"`:
   ISO 8601, **sin zona es hora del negocio** (`AppSettings.timezone`, `America/Bogota` por defecto), con zona
   se respeta; vacío la quita. El core solo valida que parsee; el pack convierte a UTC (`pack_commerce.seguimiento`).
   La ficha devuelve la fecha **en hora del negocio y sin zona** (`2026-11-03T09:00`) para el selector, y la lista
   trae `seguimiento_en` (ISO UTC), `seguimiento_nota` y `seguimiento_estado`, calculado en el servidor para que
   panel y APK no repitan la cuenta de la zona.
3. **Cuatro opciones de la HU más una.** El filtro `seguimiento` ofrece `vencidos`, `hoy`, `proximos_7_dias`,
   `mas_adelante` y `sin_seguimiento`. Cada lead cae en **exactamente una** (`vencidos` = ya pasó; `hoy` = lo que
   queda del día del negocio; `proximos_7_dias` = de mañana a 7 días; `mas_adelante` = después), para que las
   pestañas con contador sumen el total de la lista. La quinta (`mas_adelante`) no estaba en la HU: sin ella, los
   leads con fecha lejana habrían salido como «sin valor» en el contador. Los cortes se arman con medianoches
   locales (un cambio de hora no corre el día) y se comparan en UTC sobre la columna, sin `AT TIME ZONE`, para que
   el índice sirva. Un cliente sin lead cuenta en `sin_valor` de la faceta.
4. **Orden `seguimiento`.** `orden=seguimiento` pone primero el más atrasado y al final a quien no tiene
   seguimiento. El core no conoce columnas de packs: el provider declara `sort_query(orden) → Select(customer_id,
   valor)` (como `facet_query`) y el core hace el `OUTER JOIN`. Un negocio sin pack que lo declare responde 400
   `orden_invalido`. `Orden` sigue siendo un `Literal` (el core nombra `seguimiento`).
5. **Historia.** `pack_commerce_lead_followups` (RLS forzada, `crm_0018`) guarda cada seguimiento que deja de estar
   vigente: nota, fecha que tenía, `outcome` (`hecho`, `reprogramado`, `cancelado`) y actor. `pack_commerce_lead_stage_changes`
   no sirve: su CHECK solo admite las cinco etapas. Se cierra uno al **cambiar la fecha** (reprogramado), **quitarla**
   (cancelado) o **completarlo** (hecho); cambiar solo la nota no es otro seguimiento y no deja fila. Aparece en la
   línea de tiempo como `kind="seguimiento"`, fuente `pack_commerce.lead_seguimiento`.
6. **Completar.** `POST /api/v1/pack/commerce/leads/{cliente}/seguimiento/hecho` (permiso de escritura de `leads`)
   cierra el vigente como «hecho» y, en el mismo gesto, programa el siguiente si llega `siguiente_en`. El hook de
   edición no alcanza para «hecho» (sería indistinguible de «cancelado»), por eso la ruta propia.
7. **No mueve la actividad.** Programar un seguimiento no toca `last_activity_at` (ADR-0149: solo un cambio de etapa
   lo mueve); si lo moviera, anotar «llamar el viernes» dejaría al lead dentro del reenganche automático de `jobs.py`.
8. **El agente no escribe aquí.** No hay tool nueva ni cambia ninguna existente: es una acción de personas. Que el
   agente cree seguimientos («te escribo mañana») es otra HU con evals.
9. **Un cliente sin lead.** Programar le crea el lead `manual:<cliente>` (el mismo camino que el estado y el interés);
   «Hecho» sobre un cliente sin lead responde 404.
10. **Reversa de `crm_0018`:** se niega si hay seguimientos programados o historia (los perdería).

## Enmienda 2026-11-02 · Motivo de pérdida y valor estimado (HU-CRM-23)

1. **Perder pide motivo.** `cambiar_etapa` rechaza llevar un lead a `perdido` sin un `loss_reason` del catálogo cerrado
   (`precio`, `sin_respuesta`, `compro_en_otro_lado`, `no_era_para_el`, `otro`) con `MotivoDePerdidaInvalido`, que lista los
   permitidos; la etapa no cambia ni se guarda nada. Es la misma puerta para la persona (hook de la ficha, ADR-0149) y para el
   agente (`actualizar_lead`). Ningún camino automático (`sistema`) pierde leads, así que el job de reenganche no cambia.
2. **El motivo vigente es del lead; el de la pérdida, de la historia.** `pack_commerce_leads.loss_reason/loss_note`
   (`crm_0019`) solo valen mientras el lead está perdido: reabrirlo los limpia. Para que reabrir no borre la causa, el cambio
   de etapa a `perdido` guarda el motivo y la nota también en `pack_commerce_lead_stage_changes` (extensión mínima de lo que
   pedía la HU) y la línea de tiempo lo muestra («Motivo: Precio — nota»).
3. **Perdidos de antes.** Un lead perdido anterior a la HU queda con motivo vacío y se muestra como «Sin motivo registrado»
   (texto del servidor, el mismo en lista, ficha y exportación). Se puede anotar después sin crear un cambio de etapa falso.
4. **Valor estimado.** `estimated_value numeric(12,2)` con `CHECK >= 0`, opcional, en la moneda del negocio (sin conversión).
   Se escribe como número simple (punto o coma decimal): no se aceptan separadores de miles porque `150.000` es 150 mil en
   Colombia y 150 en otros países, y adivinar guardaría un valor mil veces mal. Nuevo `tipo="numero"` en `LeadCampoEditable`
   (validado por campo en el core, como `fecha`) que el pack vuelve a validar con su tope.
5. **Filtrar y ordenar.** Filtro `motivo_perdida` (cinco motivos más `sin_motivo`) y filtro `valor` en **tramos fijos**
   (`hasta_100000`, `100000_500000`, `500000_2000000`, `mas_de_2000000`, `sin_valor`; cortes semiabiertos, en pesos): el
   contrato de filtros del core es `clave:valor` con opciones fijas, un rango libre exigiría cambiarlo. Órdenes `valor` y
   `valor_desc`; los leads sin valor van siempre al final (el core aplica el sentido a la columna del provider).
6. **Contrato de la tool.** `actualizar_lead` gana `loss_reason` (enum cerrado) y `loss_note`, opcionales en el esquema y
   obligatorios cuando `status` es `perdido`; así ningún uso existente se rompe. Cambiar el esquema cambia la huella de las
   grabaciones del LLM: las nueve que incluyen la tool se regraban (`scripts/gate_evals.sh record`, con OK del dueño).
7. **Reversa de `crm_0019`:** se niega si hay motivos o valores guardados (los perdería).
