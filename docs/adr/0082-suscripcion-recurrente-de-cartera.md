# ADR-0082 — Suscripción recurrente de la cartera (Opción 2, base)

- **Estado:** aceptado
- **Fecha:** 2026-09-30
- **Modifica:** ADR-0062 (el propietario es el tenant), ADR-0067 (capacidad de pagos)
- **Relacionado:** ADR-0083 (pack de suscripciones de cliente final, opcional)

## Contexto

El asistente de cobranza solo puede **leer** la cartera de un cliente (`cob_consultar_saldo`,
`cob_estado_cuenta`) y registrar promesas/abonos. El modelo de dominio del pack es un
**préstamo finito**: `Prestamo` (`valor`, `saldo`, `num_cuotas`, `estatus`) con `Cuota`
(`numero_cuota`, `fecha_vencimiento`, `estatus`) y `tipo_interes`
(`packages/pack-cobranza/pack_cobranza/models.py:336-370`). No hay concepto de
**recurrencia indefinida** (cargo periódico que se renueva).

Existe un precedente de cobro **por período**: `CobroPropietario` (`periodo "YYYY-MM"`,
`estado pendiente|pagado|vencido`, único por tenant+periodo) + `LinkPago`
(`models.py:614-651`), pero es el cobro de la **plataforma al propietario** (ADR-0062),
no una suscripción del cliente final, y **no tiene tool de asistente**.

La agencia necesita tratar la **cartera de un cliente como una suscripción**: un servicio
recurrente que se factura por período (mensual), con estado (activa / pausada /
cancelada) y renovación, y que el asistente pueda **informar, crear, renovar y cancelar**
sin inventar nada.

## Decisión

Generalizar la cartera del pack-cobranza a un **modo suscripción**, reutilizando el modelo
existente y el precedente de cobro periódico:

1. **`Cartera.modalidad`** ∈ `prestamo | suscripcion` (nuevo campo, default `prestamo`).
   - En `suscripcion`, la cartera no persigue un préstamo finito: **cada `Cuota` es el
     cargo del período** (mensual por defecto) y se **regenera** al cobrarse/renovarse,
     sin `num_cuotas` fijo.
   - `Prestamo` queda **opcional** en modo suscripción (o se omite): la unidad pasa a ser
     `Cartera → Cuota(periodo)`.
2. **Ciclo y estado** de la suscripción a nivel `CarteraConfig`/`Cartera`: `periodicidad`
   (mensual), `dia_facturacion`, `estado` (`activa | pausada | cancelada`),
   `proximo_cargo`, `fecha_alta`/`fecha_cancelacion`.
3. **Generación de cargos por cron** (reusa el patrón de `jobs.py`): al iniciar el período
   se crea la `Cuota` del período; se emite `LinkPago` (precedente `CobroPropietario`) para
   el cobro en línea y/o se registra el `Pago` al confirmarse.
4. **Tools del asistente** (nuevas, prefijo `cob_`):
   - `cob_consultar_suscripcion` — estado, plan/cartera, periodicidad, próximo cargo, historial.
   - `cob_crear_suscripcion` — alta (requiere confirmación explícita del cliente).
   - `cob_renovar_suscripcion` / `cob_pausar_suscripcion` / `cob_cancelar_suscripcion`.
   - Se mantienen `cob_consultar_saldo`, `cob_estado_cuenta`, `cob_evaluar_negociacion`,
     `cob_registrar_promesa_pago`, `cob_registrar_abono`.
5. **Grounding**: guardrail de completion-claim para "suscripción creada/renovada/cancelada"
   (exige la tool en el turno) y reglas duras en el playbook del vertical.
6. **Nada nuevo de ventas**: la Opción 1 (entidad `Subscription` sobre planes/productos) es
   **opcional y separada** (ADR-0083); este ADR no la introduce.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Crear `Suscripcion` nueva dentro de pack-cobranza | Duplica `Cartera`/`Cuota`/`Pago`; dos modelos para el mismo cobro recurrente |
| Usar solo `CobroPropietario` | Es el cobro plataforma→propietario, no del cliente final; sin relación con la cartera |
| No hacer nada (solo leer saldo) | El asistente no puede vender/gestionar el servicio recurrente sin escalar |
| Meterlo en `pack_subscriptions` (Opción 1) | Ese pack es de ventas (planes/productos); la cartera es de cobranza |

## Consecuencias

**Positivas:** un solo modelo de cobro recurrente; el asistente puede informar/gestionar la
suscripción con tools reales (sin alucinar); reusa `Cuota`/`Pago`/`LinkPago`/cron.

**Negativas / lo que aceptamos pagar:** `Cartera` gana semántica dual (préstamo/suscripción)
y migración; el cron de generación de cargos es nuevo; hay que decidir el cobro real
(link por período vs recurrente automático, ADR-0067).

**Qué haría que revisáramos esto:** que se necesite facturación recurrente automática con
reintentos/tarjeta (→ ADR de débito recurrente, ya diferido en `docs/ROADMAP.md`).

## Impacto en el código

- `packages/pack-cobranza/pack_cobranza/models.py` (`Cartera.modalidad`, `CarteraConfig`
  ciclo/estado) + migración.
- `pack_cobranza/jobs.py` (cron de generación de cargos del período).
- `pack_cobranza/skills.py` (tools de suscripción) + `module.py` (guardrail/claims).
- `pack_cobranza/flow.json` + playbooks del vertical + `evals/` (escenarios de suscripción).
- Tests unit + integración.
