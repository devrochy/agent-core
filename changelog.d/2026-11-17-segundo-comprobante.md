## Un segundo comprobante se revisa (2026-11-17, HU-COM-24)

- Tras un comprobante en revisión o rechazado, el agente decía «no necesitas enviar otro» sin revisar la foto nueva. El paso «resultado» de `comprobar_transferencia` (1.1.0) y la pista de `comprobar_comprobante` piden volver a llamarla; el servidor ya distingue foto nueva (se evalúa) de la misma (devuelve lo guardado, ahora con un detalle que lo dice). `pack_commerce` 4.1.0; gate regrabado.
