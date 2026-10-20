## Un traspaso a una persona exige la tool del turno (2026-10-20)

- El verificador ya no deja pasar «ya le pasé tu caso a una persona del equipo», «te comunico con una
  persona» o «una persona del equipo te va a contactar» sin `escalate_to_human` en el turno (afirmación
  `asesor_humano` de pack-commerce, adenda 2026-10-20 de ADR-0119). Smoke de test 2026-10-20: con la
  escalación ya resuelta, el asistente lo dijo otra vez y nadie lo iba a atender.
- «Si quieres, te comunico con…» no cuenta como afirmación (el condicional reconoce «quieres»).
