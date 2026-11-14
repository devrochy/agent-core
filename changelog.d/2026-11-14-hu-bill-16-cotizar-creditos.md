## Cotizar y verificar créditos antes de una acción de IA (HU-BILL-16, 2026-11-14)

Primera pieza del cobro de las fotos de catálogo (SPEC-45, ADR-0194, D-71): el ciclo
cotizar → confirmar → verificar saldo → registrar. Aquí van las piezas de `core/billing`; el endpoint
de fotos (HU-COM-yy) las usa después.

- **`cotizar(concepto, unidades, key_mode)`** (`core/billing/creditos.py`): créditos enteros, hacia
  arriba. Con llave propia solo el fee (1,0 por foto); con la de la plataforma, el costo de referencia
  `BILLING_VISION_COSTO_REFERENCIA_USD` (0,003 USD por defecto) más margen y fee.
- **`alcanza(session, tenant_id, créditos)`**: toma `pg_advisory_xact_lock` por negocio, mide el crédito
  del mes (misma ventana y límite que `/admin/plan`) y, si no alcanza, levanta
  `QuotaExceeded("creditos")`, que `plan_limit_error` convierte en 409 `plan_limit_creditos` con
  `{metric, used, limit}`. Sin suscripción o sin crédito en el plan no limita. Interruptor
  `BILLING_BLOQUEAR_POR_CREDITOS`, **encendido** por defecto (apagado solo mide). Sin reserva (el
  ledger es append-only): dos peticiones concurrentes pueden pasarse en una foto, aceptado.
- **`imagen_ya_cobrada(session, tenant_id, image_key)`**: una foto ya registrada (`kind = 'vision'`,
  `result->>'image_key'`) no se cobra dos veces.
- **Desglose por `kind`:** `medir_creditos` suma las filas `vision` del ledger a «Imágenes» en vez de
  «IA». `by_concept` de `GET /plan` mantiene sus cuatro claves.
- **Migración 0129:** el CHECK `ck_usage_kind` admite `'vision'`. Antes no la admitía y las filas en
  sombra de la ingesta de WhatsApp (`credits = 0`) fallaban en silencio; desde aquí se registran.
  `vision` cuenta como gasto pero no como turno (`NO_SON_TURNOS`).
- **Despliegue:** migración 0129 (CHECK `NOT VALID` + `VALIDATE`, sin bloquear escrituras). Ninguna
  ruta usa todavía `alcanza`, así que el bloqueo no cambia nada hasta el endpoint de fotos.
