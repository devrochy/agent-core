# ADR-0157 — Avisos por evento declarados por las habilidades

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-04. **Enmienda:** ADR-0108 (la lista de eventos deja de ser solo del core). **Usa:** ADR-0107 (outbox).

## Contexto

`core/gateway/event_notices.py` tiene una lista cerrada `EVENTOS` (cargos, citas e invitación) y `core/events/core_handlers.py` un `_HANDLERS` fijo. Para agregar un aviso hay que tocar el core en cinco lugares (evento y rutas de variables, topic, handler, `notice_key_for` y las etiquetas del panel en `core/api/routers/settings.py:815`). La vertical necesita avisos propios: visita realizada, contrato por vencer, captación recibida, precio bajo. ADR-0023 no permite subir cada uno al core.

## Decisión

Un pack puede **declarar** sus eventos de aviso en el manifiesto. El core los atiende con un handler genérico. Lo demás de ADR-0108 se mantiene: el mapa evento → plantilla del negocio, la regla de ventana y `sin_plantilla`.

1. La declaración es `provides.avisos` en el manifiesto del pack (`core/releases/packs.py`). Es un campo aditivo del contrato `pack/2` (no sube a `pack/3`), pero cambia el hash de los packs que lo declaren y obliga a actualizar `packages/pack-versions.lock.json`. Cada aviso trae:
   - `evento` con prefijo del pack (`inmobiliario.visita_realizada`);
   - `variables` permitidas: rutas sobre un `context` que el pack arma y valida con un esquema pydantic;
   - `clave_idempotencia`, como plantilla sobre ids del payload;
   - `texto_respaldo`, la clave de configuración del texto por defecto (ADR-0017).
2. El pack registra cada aviso en su `on_activate` con `aviso_registry.register(plugin_id, evento, Esquema)`: el core nunca importa un módulo de pack por nombre. El handler genérico se suscribe **por proceso** (una vez por PID, como `ensure_core_handlers`) a todos los eventos registrados.
   - Toma el negocio de `get_current_tenant_id()`, nunca del payload, y comprueba que el pack esté habilitado.
   - Resuelve la conversación de WhatsApp del cliente igual que `_avisar_suscripcion`.
   - Llama a `send_event_notice` con las variables ya validadas.
   - El payload del evento en `domain_events` lleva solo ids y valores ya presentables: la dirección exacta de un inmueble no viaja en el payload si `revelar_direccion` no lo permite (ADR-0053, ADR-0156 §5).
3. El panel de plantillas (`/admin/settings/message-templates`) lista los eventos del core más los declarados por los packs habilitados. El mapeo de eventos de un pack deshabilitado se conserva: hoy `settings.py:905` descarta las claves que no están en `EVENTOS`, y el siguiente PUT lo borraría.
4. La primera versión no tiene cabecera con imagen ni botones de plantilla. Los botones son de HU-AOS-09.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Agregar cada evento de la vertical a `EVENTOS` | Contradice ADR-0023 y ata el core a una vertical |
| Que el pack envíe la plantilla directamente | Duplica la regla de ventana, la idempotencia y el respaldo de ADR-0108, y se saltaría el mapa del negocio |

## Consecuencias

**Positivas:** cualquier pack (inmobiliario, cobranza, commerce) declara sus avisos sin tocar el core. Los avisos del core quedan como están.

**Negativas / lo que aceptamos pagar:** un esquema de manifiesto más que validar al cargar el pack. Un aviso mal declarado se rechaza al activar, no en el turno.

**Qué haría que revisáramos esto:** que un pack necesite variables que no puede armar sin leer tablas de otro pack (invariante 5).

## Impacto en el código

- `core/gateway/event_notices.py` (registro abierto `aviso_registry`).
- `core/events/core_handlers.py` (handler genérico por proceso).
- `core/releases/packs.py` (`provides.avisos`, aditivo en `pack/2`) y `packages/pack-versions.lock.json`.
- `core/api/routers/settings.py` (catálogo `EVENTOS` en :870-947, etiquetas en :815, conservar el mapeo de packs deshabilitados).
- `docs/openapi.json`.
