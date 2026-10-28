## Runbook: el alta manual desde el panel y su correo de acceso (2026-10-28)

- `docs/runbooks/altas-en-revision.md` gana la § 10 «Negocio creado a mano desde Plataforma» (ADR-0175):
  estados del envío (`enviado`, `sin_smtp`, `fallido`, `ya_emitida`, `ya_es_miembro`), «Reenviar acceso»,
  los errores 409/400 del alta y del reenvío, `PANEL_BASE_URL` y el límite conocido del reenvío por tester.
  Las §§ 5 y 6 y las referencias a «crear el negocio a mano» apuntan a ella.
- ADR-0175 pasa de «propuesto» a «aceptado». Solo documentación: sin cambios de código ni de contrato.
