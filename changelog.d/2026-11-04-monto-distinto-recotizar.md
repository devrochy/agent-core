## El error de monto al confirmar una inscripción dice cómo corregirlo (2026-11-04)

- `sub_confirmar_inscripcion`: cuando el `monto_a_pagar` no coincide con el total vigente del plan (p. ej. tras cambiar de plan el modelo repetía el de la cotización anterior), el error ahora le indica cotizar de nuevo ese plan con `sub_cotizar_inscripcion`. Antes decía solo «no coincide» y el modelo repetía el mismo monto turno tras turno hasta el tope (corrida de pass^4 del 2026-11-04, `cambia-de-plan-antes-de-confirmar` 2/4).
- El resumen de confirmación ya no repite la periodicidad que el nombre del plan dice («Premium anual», no «Premium anual anual»).
