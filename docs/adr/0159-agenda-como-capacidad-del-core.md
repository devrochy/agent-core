# ADR-0159 — Agenda como capacidad del core (`SchedulingCapability`)

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-06, D-SPEC-28-4. **Usa:** el molde de ADR-0067 (`payment_registry`) y ADR-0106 (`SubscriptionRegistry.capability_for_tenant`), y ADR-0158.

## Contexto

Para agendar una visita, el pack inmobiliario tiene que reservar en la agenda del asesor del inmueble. Un pack no puede escribir tablas de otro (invariante 5). Que el LLM llame las tools de calendar hace dos escrituras sin vínculo con el inmueble. El encargo A2A del Agent OS no existe antes de R3.

## Decisión

El core define `SchedulingCapability`, que implementa pack-calendar, con interfaz mínima:

- `disponibilidad(admin_id, desde, hasta, duracion)`;
- `reservar(customer_id, admin_id, inicio, duracion, titulo, idempotency_key, correo=None)`;
- `reagendar(reserva_ref, inicio)`;
- `cancelar(reserva_ref)`.

1. **Registro y resolución.** `scheduling_registry.register(plugin_id, cap)` en el `on_activate` del pack. La resolución es solo `capability_for_tenant(get_current_tenant_id())`, que cruza con los packs habilitados del AgentSpec (molde de `core/subscriptions/__init__.py`). Nunca `capability_for(None)`.
2. **Las operaciones no reciben `tenant_id`.**
   - El core pone la `referencia` con el id del pack que llama (ADR-0158).
   - `reagendar` y `cancelar` rechazan reservas de otro pack.
3. **Choques.** Un choque devuelve horarios alternativos, no una excepción.
4. **Idempotencia y estado.**
   - La idempotencia es por `idempotency_key`.
   - No hay `obtener`: el estado llega por los eventos `appointment.*`.
5. **Sin estado en la instancia:** nada de cachés de configuración ni de tokens.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| `encargar(agenda, …)` del Agent OS | No existe antes de R3, agrega un salto y parte la visita en dos tareas |
| El LLM llama las tools de calendar | Dos escrituras del modelo, correo obligatorio, cita sin inmueble |

## Consecuencias

**Positivas:**
- Una escritura determinista que funciona igual antes y después del Agent OS.
- Sirve a cualquier pack que agende (cobranza para visitas de cobro, servicios técnicos).

**Negativas / lo que aceptamos pagar:** una interfaz nueva del core. Su primer consumidor es la vertical, por eso espera al piloto (ADR-0023).

**Qué haría que revisáramos esto:** que el encargo A2A del Agent OS (R3) cubra el caso con una sola tarea y sin salto extra.

## Impacto en el código

- `core/scheduling/` (nuevo registro).
- `packages/pack-calendar/pack_calendar/module.py` (registro).
- `packages/pack-calendar/pack_calendar/capability.py` (nuevo).
