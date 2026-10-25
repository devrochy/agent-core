# ADR-0160 — Suscripciones de plazo fijo con identidad por referencia

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-08 y HU-INMO-23. **Enmienda:** ADR-0106 (suscripciones v1).

## Contexto

Un contrato de arriendo cobra un canon mensual con fecha de inicio, día de pago y fecha de fin. Con `pack_subscriptions` de hoy hay dos problemas:

- **`activate` puede reescribir la suscripción equivocada.** Con variante nula y sin coincidencia de referencia, toma cualquier suscripción vigente del cliente y, si el precio cambia, la reescribe (`capability.py:196-245`).
- **Las tools `sub_*` eligen sin preguntar.** Toman la suscripción más reciente no cancelada, sin parámetro para elegir. Un cliente con gimnasio y arriendo podría pausar el arriendo.

## Decisión

`SubscriptionCapability` acepta suscripciones identificadas por **referencia**, con plazo y sin autogestión.

1. **`activate` con `reference` no vacía y no interna:**
   - identifica la suscripción solo por `(cliente, reference)`;
   - nunca reutiliza ni reescribe otra suscripción del cliente.
2. **Argumentos opcionales:** `inicio`, `dia_pago`, `fin`, `valor_programado` y `autogestion` (por defecto `true`, como hoy). Sin ellos, la conducta es la actual.
3. **`autogestion=false`:**
   - `pause`, `cancel` y `change_plan` pedidos desde las tools `sub_*` del cliente se rechazan con una pista;
   - solo el panel o el pack dueño los ejecuta.
4. **Tools `sub_*`:**
   - excluyen las suscripciones con `autogestion=false` de la elección automática;
   - si quedan varias autogestionables, piden cuál en vez de tomar la más reciente.
5. **Antes de mergear** se corren los journeys de suscripciones y los de alta automática (HU-SH-03/04): `activate` lo usan commerce y la plataforma.

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

- `packages/pack-subscriptions/pack_subscriptions/capability.py` y su modelo (columnas de plazo y `autogestion`).
- `core/subscriptions/tools.py`.
- `core/subscriptions/__init__.py` (contrato).
