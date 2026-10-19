## Dogfooding de solvershub con especialistas (2026-10-19)

- HU-AOS-22: la plantilla `suscripciones` suma la regla `preset_suscripciones_mi_plan` («cambiar mi plan», «mi
  plan actual», «subir/bajar/cancelar/pausar mi plan») con id nuevo, así que `presets-v2 --aplicar` se la agrega
  también a los negocios que ya tenían el preset. Adenda de ADR-0139: las reglas de una plantilla evolucionan
  con ids nuevos; las frases de un negocio van por el editor (`overrides.examples`).
- Tests: rutas del preset en orden (unitario), golden de ruteo con las frases del dogfooding y un punta a punta
  con el modelo guionado (sombra, luego activo: demo → Agenda, contratar Premium → Ventas, cambiar mi plan →
  Suscripciones, saludo → General).
- Sin migración ni cambios en el loop o el router. Con el ruteo apagado nada cambia; el gate en replay sigue
  verde sin regrabar.
