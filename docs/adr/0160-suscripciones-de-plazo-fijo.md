# ADR-0160 — Suscripciones de plazo fijo con identidad por referencia

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-08 y HU-INMO-23. **Enmienda:** ADR-0106 (suscripciones v1) y ADR-0111 (§4-5: idempotencia por `(referencia, plan)` y `varias_por_cliente`).

## Contexto

Un contrato de arriendo cobra un canon mensual con fecha de inicio, día de pago y fecha de fin. Con `pack_subscriptions` de hoy hay dos problemas:

- **`activate` puede reescribir la suscripción equivocada.** Sin coincidencia de referencia y con `varias_por_cliente=false` o variante nula, toma cualquier suscripción vigente del cliente y, si el precio cambia, la reescribe (`capability.py:196-245`). Para planes eso es lo correcto (HU-SUB-06, HU-SH-04: quien compra un plan pago después de su Free sigue con su suscripción), pero para un contrato es un error.
- **Las tools `sub_*` eligen sin preguntar.** Toman la suscripción más reciente no cancelada, sin parámetro para elegir. Un cliente con gimnasio y arriendo podría pausar el arriendo.

## Decisión

`SubscriptionCapability` acepta suscripciones identificadas por **referencia**, con plazo y sin autogestión.

1. **`activate` con `plazo_fijo=True` y `reference` no vacía** (opt-in explícito):
   - identifica la suscripción solo por `(cliente, reference)`;
   - nunca reutiliza ni reescribe otra suscripción del cliente.
   - **Sin `plazo_fijo`, `activate` conserva exactamente la conducta de ADR-0106 y ADR-0111**, incluida la reutilización de la Free vigente que usa commerce (`pack_commerce/planes.py:107`).
2. **Argumentos opcionales** con `plazo_fijo=True`: `inicio`, `dia_pago`, `fin`, `cobrar_periodo_inicial`, `autogestion` (por defecto `false` en plazo fijo) y `pack_duenio` (el plugin que la creó, que se guarda en la suscripción).
3. **`autogestion=false`:**
   - `pause`, `cancel` y `change_plan` pedidos desde las tools `sub_*` del cliente se rechazan con una pista;
   - solo los ejecutan el panel o el pack dueño, que se identifica pasando `solicitante=<plugin_id>` y se compara contra `pack_duenio`.
4. **Tools `sub_*`:**
   - `sub_pausar`, `sub_cancelar` y `sub_cambiar_plan` excluyen las suscripciones con `autogestion=false`;
   - `sub_consultar` y `sub_link_pago` las incluyen (el arrendatario consulta y paga el canon por chat);
   - con varias candidatas, piden cuál en vez de tomar la más reciente.
5. **Cargos y eventos:**
   - los jobs no generan cargos después de `fin`;
   - `subscription.activated` sigue emitiendo `referencia`; una referencia de contrato (`inmobiliario:contrato:*`) no dispara el alta automática (HU-SH-03).
6. **Antes de mergear** se corren los journeys de suscripciones y los de alta automática (HU-SH-03/04): `activate` lo usan commerce y la plataforma.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Que el pack inmobiliario cobre por su cuenta con pack-payments | Duplica recordatorios, mora, links y eventos de pack-subscriptions |
| Una variante de plan por contrato | Llena el catálogo de "planes" falsos y no resuelve la elección de las tools |

## Consecuencias

**Positivas:** cobros recurrentes por contrato (arriendos, cuotas de crédito, matrículas) sin chocar con las suscripciones de autogestión.

**Negativas / lo que aceptamos pagar:** más ramas en `activate` y en las tools, y tests de no regresión sobre commerce y el alta automática.

**Qué haría que revisáramos esto:** que el modelo de cargos no alcance para el canon (por ejemplo, prorrateos o pagos parciales frecuentes), lo que pediría un pack de cartera.

## Impacto en el código

- `packages/pack-subscriptions/pack_subscriptions/capability.py` y su modelo (columnas de plazo, `autogestion`, `pack_duenio`).
- `packages/pack-subscriptions/pack_subscriptions/jobs.py` (sin cargos después de `fin`).
- `core/subscriptions/tools.py`.
- `core/subscriptions/__init__.py` (contrato y `SubscriptionView` con `autogestion` y `reference`).
- Filtro del alta automática por referencia (`core/shared/events.py`, `CLAVES_DE_CORRELACION`).
