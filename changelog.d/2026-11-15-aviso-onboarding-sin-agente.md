## Onboarding: el aviso solo insiste si no hay agente (2026-11-15)

- HU-SH-10. `recorrido.avisar` y `recorrido.abrir` se apagan en cuanto el negocio tiene un agente
  propio (`agents.kind <> 'core'`), aunque el recorrido esté pendiente, en curso u omitido. El
  Coordinador del alta no cuenta como agente creado. Los endpoints del recorrido siguen disponibles.
