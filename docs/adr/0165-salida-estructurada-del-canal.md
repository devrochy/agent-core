# ADR-0165 — Entrada de ubicación y salida estructurada del canal (listas, ubicación e imágenes)

- **Estado:** propuesto (se acepta al firmarse el piloto, D-31 de solvershub-docs).
- **Fecha:** 2026-10-24
- **Contexto:** SPEC-28, HU-INMO-02 y HU-INMO-03, D-SPEC-28-9. **Usa:** el `safe_renderer` (`docs/SAFE-RENDERER.md`) y HU-AOS-09 (botón).

## Contexto

`core/gateway/channels/whatsapp/inbound.py:81` descarta los mensajes de tipo `location`. `list_reply.id` ya se conserva en `interactive_id`. En la salida, el `safe_renderer` solo conoce `_safe_append` (texto) y `_image_url` (una imagen). No hay listas, ni ubicación, ni varias imágenes. La vertical las necesita para mostrar inmuebles, ofrecer horarios y compartir la dirección. Commerce también las usaría (listas de productos).

## Decisión

1. **Entrada.**
   - El gateway conserva la ubicación entrante (`latitude`, `longitude`, `name`, `address`) en el mensaje normalizado, detrás de una bandera por negocio (`canal_ubicacion_entrante`).
   - Con la bandera apagada, la conducta es la de hoy.
   - La ubicación es dato no confiable: el servidor la usa solo como candidata a confirmar.
2. **Salida: tres claves reservadas nuevas** del resultado de una tool, que el `safe_renderer` oculta al modelo (`strip_reserved_keys`) y entrega al canal:
   - `_interactive`: lista de hasta 10 filas con id opaco y título;
   - `_location`: punto con nombre y dirección;
   - `_media`: hasta N imágenes con URL firmada.
3. **Reglas.**
   - Los ids de fila son índices opacos (`inm:3`), nunca uuids.
   - Al volver, el payload se valida contra lo que el servidor guardó de la conversación.
   - Fuera de la ventana de 24 h no se envían.
   - En el chat web se degradan a texto numerado.
4. **El botón queda en HU-AOS-09** de punta a punta (D-SPEC-28-9).
5. **No cambia el contrato de canal:** las claves son aditivas y los adaptadores que no las soportan las degradan.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Que el modelo arme el JSON interactivo | Ids inventados y contenido sin control. Viola la regla de que el servidor genera links y datos |
| Una tarjeta de imagen por inmueble | Más mensajes y más costo de Meta. La lista resuelve la elección con un solo mensaje |

## Consecuencias

**Positivas:** listas, ubicación e imágenes para cualquier pack, con control del servidor.

**Negativas / lo que aceptamos pagar:** tres claves reservadas más que documentar y probar en cada adaptador.

**Qué haría que revisáramos esto:** que Meta cambie los límites de las listas o que un canal nuevo (Messenger, Instagram, ADR-0152) necesite otra forma de elección.

## Impacto en el código

- `core/gateway/channels/whatsapp/inbound.py` (ubicación).
- `core/gateway/channels/whatsapp/outbound.py`.
- `core/orchestrator/safe_renderer.py` (claves `_interactive`, `_location`, `_media`).
- `docs/SAFE-RENDERER.md`.
- Adaptador del chat web (degradación).
