## Las citas ya no se escalan como promesas de pago (2026-10-19)

- `cob_promesa_check` (pack-cobranza) solo toma como promesa una afirmación de cobranza: un
  verbo de registro junto a promesa, acuerdo, compromiso, pago, abono o cuota en la misma
  frase, o "acordamos", "prometiste", "quedamos en que". Un "tu cita quedó agendada" a secas
  ya no cuenta. Incidente en test: en un negocio con cobranza y calendario, cada cita bien
  creada con `create_calendar_event` terminaba escalada con `summary=cob_promesa_check`.
- "Agendé el pago/la cuota" lo respalda también una cita creada o movida con éxito en el
  mismo turno (`create_calendar_event`, `update_calendar_event`, por nombre, sin importar
  pack-calendar); "registré tu promesa" o "acordamos" siguen exigiendo
  `cob_registrar_promesa_pago` o `cob_registrar_promesa_saldo`.
- Tests en `tests/test_pack_cobranza_guardrails.py`.
