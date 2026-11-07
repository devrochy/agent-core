## Una inscripción creada desde el panel ya se puede aprobar en Alta automática (2026-11-06)

- Corrige agent-core#672: la espera de aprobación de una inscripción con `origen = panel` se lanzaba como revisión «por un dato» y `POST …/altas/{referencia}/aprobar` respondía 409 `alta_no_revisable`; solo se podía descartar, así que ningún alta desde el panel (Free o de pago) llegaba a crear el negocio.
- Ahora es un motivo más de la revisión **por riesgo** (ADR-0125 § 6), junto a los demás controles (interruptor, aceptación, Free repetido, recompra, señales): quien aprueba ve todos los motivos y la aprobación no salta controles que nunca se evaluaron. Un dato inválido (correo, plan) sigue siendo revisión por dato: se corrige y se reintenta.
- Sin cambios de API ni de esquema. Pruebas en `tests/integration/test_antiabuso_alta.py`.
