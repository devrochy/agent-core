## Opciones propias del Coordinador: voz, horario, nivel de IA y reglas de la recepción (2026-11-23, HU-AOS-98)

- Nuevas `GET` y `PUT /api/v1/admin/coordinator` (escritura parcial, `settings`): voz e identidad, horario del negocio, nivel de IA y reglas de la recepción. 422 por campo; 409 `plan_nivel_ia_not_included`.
- La release congela las reglas y el nivel en la clave `recepcion` del manifiesto (se omite si está vacía: el hash de las releases existentes no cambia); la vista de recepción las aplica. Una regla guardada inválida hace fallar el build (`reglas_recepcion_invalidas`), no el turno.
- Las rutas del agente Coordinador (reglas, tools declarativas, habilidades, packs) siguen en 409 `coordinador_inmutable`. ADR-0211 aceptado.
