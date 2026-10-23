## `calendar_completion_check`: un paso futuro no es una afirmación (2026-10-23)

- `pack-calendar`: "cuando confirmes el resumen, la cita queda agendada" y "…y ahí queda agendada" ya no
  cuentan como afirmar que la cita quedó creada. Con el motor de procedimientos el asistente describe la
  confirmación antes de que exista la cita, y la regla de "quedó/queda agendada" lo escalaba a un humano
  (pass^4 de los procedimientos, 2026-10-23: `agendar-una-demo` 7/8, borradores capturados con #519).
- Solo aplica a las frases con "quedó/queda" (`agendada`, `reagendada`, `cancelada`) y a oraciones donde el
  marcador (`cuando`, `una vez que`, `en cuanto`, `apenas`, `luego/después de`, `al confirmar`, `para que`,
  `ahí`) viene ANTES de la afirmación. "Si todo está bien, tu cita quedó agendada" y "Tu demo quedó agendada
  y cuando llegue el día…" siguen bloqueando sin la tool. Sin cambio de contratos ni de la evidencia exigida.

### Promesas de consultar (misma fecha)

- `calendar_completion_check`, claim `disponibilidad`: "si me dices el día, **reviso los horarios libres**" ya no
  cuenta como afirmar que un horario está libre; anuncia que se va a consultar (pass^4 del 2026-10-23, modo sin
  motor: `agendar-una-demo` 3/4 con el borrador capturado). Solo verbos en presente, futuro o infinitivo
  (`reviso`, `consulto`, `verifico`, `busco`, `miro`, `voy a ver`…) antes de la frase y sin una hora concreta en
  la oración. "Revisé los horarios libres…" (pasado), "Tenemos horarios libres el martes" y "reviso y los horarios
  libres son a las 10:00" siguen bloqueando sin `check_availability`/`find_next_available`.
