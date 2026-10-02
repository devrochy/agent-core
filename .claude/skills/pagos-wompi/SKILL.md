---
name: pagos-wompi
description: >
  Conocimiento vigente de pagos con Wompi para el motor: firma de integridad del checkout,
  checksum de eventos del webhook, sandbox vs producción, montos mínimos, una URL de eventos
  por entorno y conciliación. Úsala al implementar HU-COM-01, HU-COM-06, HU-SUB-* y HU-SH-04.
  Cada dato lleva fecha y fuente.
---

Datos verificados el **2026-10-02** en `docs.wompi.co`. El algoritmo del checksum de eventos
ya está implementado en `packages/pack-payments/pack_payments/providers/wompi.py`.

## Firma de integridad (checkout)

Para crear un link/checkout, Wompi valida que el monto y la referencia no se alteraron.
Se calcula **en el servidor** (nunca en el frontend) con el **secreto de integridad**
(`prod_integrity_…`), distinto de la llave pública y la privada:

```
SHA256( "<referencia><monto_en_centavos><moneda><secreto_integridad>" )
```

- Con `expiration-time` el orden es: `<referencia><monto><moneda><fechaExpiracion><secreto>`.
- `monto` va **en centavos** (si el precio es COP y el monto es entero de pesos, ×100).
- Se envía como `signature:integrity`.

**Consecuencia para COM-01:** el link se arma en el servidor desde `Order.total`; el LLM
nunca pasa monto ni referencia (tool `confirmar_pago_pedido(order_id)`).

## Eventos del webhook (checksum)

Wompi envía un `POST` con `event`, `data`, `environment` (`test`/`prod`), `signature`
(`properties[]` y `checksum`), `timestamp` y `sent_at`. El checksum viene también en el
header **`X-Event-Checksum`**.

Validación (SHA256, secreto de eventos `prod_events_…`, distinto de la API key):

```
SHA256( <valores de signature.properties en orden, sin separador> + <timestamp> + <secreto_eventos> )
```

- **No asumir** las `properties`: vienen en el evento y pueden variar; extraerlas del array.
- Comparar contra `X-Event-Checksum` o `signature.checksum`. Si no coincide, **descartar**.
- Responder `200` (cualquier 2xx sirve); si no, Wompi reintenta **hasta 3 veces más**.
  Por eso el handler debe ser **idempotente** por id de transacción/referencia.
- HTTPS obligatorio y URL pública.

## Sandbox vs producción

- `environment: "test"` (sandbox) vs `"prod"`. Las credenciales y secretos cambian entre
  ambos; no mezclar.
- Límites: el **monto mínimo** de Wompi (del orden de COP 1.500–2.000 según el método)
  deja fuera a los cobros pequeños → [HU-SUB-13](03-fase-0-go-live/SPEC-06-planes-y-suscripciones/hu/HU-SUB-13-cobros-bajo-minimo.md)
  (cobros por debajo del mínimo). Un error de monto por debajo del mínimo debe ser un
  **error de negocio** (resultado controlado), no una excepción que apague el pack
  (HU-ARCH-18).

## Una sola URL de eventos por entorno (N-32)

- Wompi admite **una URL de eventos por entorno**: el billing de plataforma y el
  `pack_payments` del tenant `solvershub` pueden pisarse (por verificar). El billing de
  plataforma por dos canales (HU-SH-04) debe usar una **fuente única**
  (`tenant_subscriptions`) y una referencia de período compartida (`plat-<slug>-<AAAAMM>`)
  para no cobrar dos veces el mismo período.

## Conciliación

- Job diario que compara transacciones de Wompi con cargos/pedidos y alerta diferencias.
- El pago marca el cargo como pagado y **reactiva** una suscripción vencida; el evento
  aplica el pago a `tenant_subscriptions` (D-13).

## Reglas del motor

- Idempotencia por `idempotency_key`/referencia en `create_link`.
- Guardrail de montos: hoy bloquea todo valor ≥ 1.000.000 (N-06); debe permitir el plan
  Empresarial y validar contra el pedido, no por umbral ciego.
- Sin borrado físico de pagos; `deleted_at` cuando aplique (invariante 8).
