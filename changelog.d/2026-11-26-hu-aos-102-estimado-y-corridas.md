## Estimado de créditos y aprobación de las corridas de pruebas del agente (HU-AOS-102) (2026-11-26)

- `core/billing/pruebas.py::estimar_corrida`: por cada `k` de 1 al tope del negocio, `estimado`, `de_la_cuota`,
  `del_saldo` y `saldo_despues` (k = 2 es el doble de k = 1). La cuota gratis es la misma del playground
  (`max(0, 300 − usado)` sobre `medir_creditos`). Turnos del agente por nivel de IA, cliente simulado y juez; con llave
  propia el agente solo paga el fee. Cifras iniciales de HU-BILL-29, a recalibrar con HU-AOS-105.
- `POST /admin/agents/{agent_id}/pruebas/estimado` y `POST …/pruebas/corridas` (`core/api/routers/pruebas_corridas.py`,
  `core/pruebas/corridas.py`): la corrida exige `confirmo_creditos` igual al estimado de su `k` (409 `estimado_cambio`),
  `alcanza()` sobre lo que sale del saldo (409 `plan_limit_creditos`), una activa por negocio (409 `corrida_en_curso`),
  5 por día (429 `corridas_por_dia`) y máx. 20 escenarios aprobados. Crea la corrida `en_cola` con `tope_creditos` =
  estimado × 1,5; no ejecuta nada (el corredor es HU-AOS-103). El `costo_usd` por `k` solo lo ve el master.
- Tope de `k`: `PRUEBAS_K_MAXIMO` (4) y, por negocio, `tenants.settings.pruebas.k_maximo` que fija el master con
  `GET|PUT /admin/platform/tenants/{id}/pruebas` (1 a 10, auditado). `k` fuera del tope: 422 `k_fuera_de_rango`.
- Variables nuevas: `PRUEBAS_AGENTE_ENABLED` (apagada: las rutas del agente responden 404), `PRUEBAS_K_POR_DEFECTO`,
  `PRUEBAS_K_MAXIMO`.
