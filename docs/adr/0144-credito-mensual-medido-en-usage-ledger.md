# ADR-0144 — Crédito mensual medido: créditos congelados en `usage_ledger`, TRM por setting y mes del ciclo

- **Estado:** propuesto (HU-BILL-04/06, 2026-10-19). Es un **precursor en modo sombra** de HU-BILL-04, no su
  cierre: mide y avisa, no bloquea. Las decisiones revisables están marcadas.
- **Fecha:** 2026-10-19
- **Contexto:** D-35, `modelo-de-precios.md` §2, §5 y §6 (docs), HU-BILL-04 y HU-BILL-06, SPEC-03 `design.md`.
  **Enmienda:** ADR-0008 (columnas nuevas en el ledger) y ADR-0142 §8 (`credit.used` deja de ser `null`).
  **Usa:** ADR-0045 (métricas derivadas, sin contadores paralelos), ADR-0116/0117 (`kind` del ledger),
  ADR-0123 (calendario del cobro).

## Contexto

ADR-0142 dejó el crédito mensual del plan (2.500 / 20.000 / 75.000 / 240.000 créditos, 1 crédito = COP 10)
como tope informativo con `used: null`: no había tasa USD→COP ni créditos en el ledger. El panel necesita la
barra real y los avisos al 80 % y al 100 % antes del enforcement (HU-BILL-06).

`usage_ledger` registra una fila por turno de IA con su costo en USD (ADR-0008), append-only, con RLS forzada
e índice `(tenant_id, created_at)`. STT, visión y plantillas no escriben al ledger: solo dejan su rastro en
`messages` (`media_type`, `template_name`).

## Decisión

1. **Dónde viven los créditos: en `usage_ledger`** (§6 opción A, ADR-0045). Migración 0096, columnas
   nullables sin default ni reescritura: `credits numeric(12,1)`, `key_mode` (`platform`|`byok`),
   `tarifario_version` y `trm_cop`, con CHECKs `NOT VALID`. No hay tabla de movimientos aparte: sería un
   segundo registro del mismo consumo.
2. **Fórmula (tarifario `v1`, `core/billing/tarifario.py`):**
   `créditos = ⌈costo_usd × TRM × (1 + 2,0) / 10⌉ (a 0,1) + fee`, con fees 0,2 por turno de IA, 0,5 por nota
   de voz, 1,0 por imagen y 0,3 por plantilla enviada. Con llave propia (BYOK), solo el fee. Meta queda fuera
   (D-06). Margen y fees son de la versión: cambiarlos es publicar `v2`, nunca editar `v1`.
3. **Se calculan al escribir.** `_write_usage_ledger` congela `credits`, `key_mode`, `tarifario_version` y
   `trm_cop` en la fila. Mismo ledger → mismo saldo (determinismo de HU-BILL-06): cambiar la TRM no reescribe
   lo medido.
   - Sin llamada al LLM en el turno (presupuesto agotado antes de la primera) no hay fee: 0 créditos.
   - Si el cálculo falla, la fila se escribe igual con esas columnas en NULL. Si lo que falla es el INSERT
     con los créditos (0096 sin aplicar, valor fuera de rango), se reintenta una vez sin ellos. La fila y su
     `cost_usd` son la fuente de la facturación y no se pierden por el crédito.
   - La TRM se cuantiza a 2 decimales antes de calcular: la fila guarda exactamente la tasa que usó.
   - `key_mode` sale de `_usa_llave_propia`, la misma condición con la que el loop usa la llave del negocio.
4. **TRM configurable por setting**, `BILLING_TRM_COP` (`core/config.py`), por defecto **4000** (el supuesto
   de `calc_precios.py`), acotado a 2.000–8.000. **Pendiente del dueño:** fijar la TRM oficial (HU-BILL-01).
   - *Revisable:* el arquitecto propuso la TRM como constante dentro de `v1` (cambiarla = versión nueva). Se
     dejó en setting porque el encargo pide no tenerla en duro; el determinismo se conserva porque cada fila
     guarda la TRM que usó. El riesgo es que test y producción midan distinto si el env difiere.
5. **Filas viejas (sin `credits`)** se calculan al leer con la misma fórmula y la TRM vigente, como
   plataforma. Es **interino**: afecta solo al mes en curso al desplegar y se retira antes de HU-BILL-06.
6. **Notas de voz, imágenes y plantillas: solo el fee, derivado de `messages` al leer.** Inbound
   `audio/%` × 0,5, inbound `image/%` × 1,0 y outbound con `template_name` en `sent|delivered|read` × 0,3. No
   se filtra `deleted_at`: borrar un mensaje no devuelve un consumo que ya ocurrió.
   - Es **interino y mutable** (si un fee cambia en `v2`, los meses pasados cambian). Pasa a filas del ledger
     con su costo de proveedor (`stt`, `vision`, `template_sent`, con `source_ref` único) antes de HU-BILL-06.
   - Queda abierto cómo se nombra el concepto: el `kind` de 0070 (`turn|agent_run|…`) es otra cosa que el
     concepto de `design.md`. Hay que decidir entre una columna `concept` nueva y reutilizar `kind` antes de
     cerrar HU-BILL-04.
7. **Qué filas cuentan:** todas las de `usage_ledger` salvo `kind = 'eval'` (a cargo de la plataforma,
   ADR-0117); el gasto A2A (`agent_run`) cuenta (ADR-0116). El fee de turno solo aplica a `kind = 'turn'`. Se
   excluye el playground del panel (canal `test`), igual que `costo_llm_usd`.
8. **Métrica en el registro (ADR-0045 §2):** `creditos` (alcance `period`) en `core/billing/metrics.py`, para
   que el enforcement de HU-BILL-06 la lea sin reescribir nada. El desglose lo calcula
   `core/billing/creditos.medir_creditos`.
9. **Mes de consumo alineado con el ciclo:** `periodos.ventana_mensual(period_start, dia_ancla, now)` parte
   de `period_start` y avanza mes a mes por calendario con el día ancla del cobro hasta el tramo que contiene
   hoy. Así el crédito se reinicia cada mes aunque `period_start` no se mueva (renovación anticipada, cron
   atrasado, período vencido).
   - `conversaciones`, `mensajes` y `costo_llm_usd` siguen usando `period_start` a secas: son dos ventanas
     distintas en la misma página. *Revisable:* unificarlas al hacer HU-BILL-06.
10. **`GET /admin/plan`:** `credit` trae `used` (créditos con décimas), `percent` (sin tope de 100: hoy se
    puede pasar), `period_start`/`period_end`, `by_concept` y `by_agent`. Mismo endpoint y mismo permiso
    `plan`. Si la medición falla, `used`/`percent` vuelven a `null` y el panel muestra «medición pendiente».
    - `by_agent` agrupa por agente, no por instancia (ADR-0142 §1), con como mucho 50 agentes con nombre más
      un renglón sin agente (turnos anteriores al runtime v3 y fees de medios), así la suma cuadra con `used`.
    - La TRM, el margen y el costo en USD no se exponen al negocio.
    - *Revisable:* el desglose por agente lo ve cualquier colaborador con el permiso `plan` (es del negocio
      completo) y conserva el nombre de agentes ya borrados (el consumo ocurrió).
    - `medir_creditos` exige correr dentro del `tenant_scope` del negocio medido: con otro scope la RLS
      devolvería 0 en silencio, así que lanza `RuntimeError`.
11. **Sin bloqueo.** Al llegar al tope solo se avisa (panel, 80 % y 100 %). Siguen activos los topes de
    volumen actuales (`costo_llm_usd`, `mensajes`, etc., ADR-0045). El enforcement de créditos, el colchón del
    5 % y `usage_alerts` son HU-BILL-06.

## Consecuencias

- El panel muestra el consumo real del mes con su desglose.
- Deploy: primero la migración y después el código (el ORM inserta las columnas nuevas). Rollback: primero el
  código anterior y después el `downgrade` de 0096, que borra los créditos congelados; la lectura los vuelve a
  calcular con la TRM vigente **y como plataforma**, así que los turnos BYOK ya medidos pasan a contar costo +
  margen. Si eso importa, se vuelve el código y no se corre el `downgrade` (las columnas son nullables).
- Rendimiento: la suma del ledger usa `ix_usage_ledger_tenant_created`; la de `messages` filtra por
  `(tenant_id, created_at)` sin un índice exacto. Revisar con `EXPLAIN` si `/admin/plan` se vuelve caliente
  o en negocios grandes (caché corta por negocio o índice CONCURRENTLY).
- Los CHECKs de 0096 quedan `NOT VALID`; validarlos es seguro (las filas viejas tienen todo en NULL) y va con
  el cierre de HU-BILL-04.

## Pendientes

- **Dueño:** TRM oficial (HU-BILL-01); si las plantillas fallidas cobran; si el desglose por agente se queda.
- **HU-BILL-04:** filas `stt`/`vision`/`template_sent` con costo de proveedor y `source_ref` único; decidir
  `concept` frente a `kind`; retirar el cálculo en lectura de filas viejas y de fees derivados.
- **HU-BILL-06:** enforcement con colchón, `usage_alerts`/`audit_log` de avisos y bloqueos, y una sola ventana
  para todas las métricas de período.
