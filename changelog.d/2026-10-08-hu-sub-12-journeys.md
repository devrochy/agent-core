## Journeys: conversaciones completas con pagos y reloj (2026-10-08)

- HU-SUB-12. `evals/journeys.py` corre conversaciones completas contra el loop real (`process_conversation`),
  con la pasarela simulada a nivel de proveedor (sin red, con el mínimo real), eventos (`payment.confirmed`), saltos
  de reloj y jobs de los packs, y verifica herramientas (desde `audit_log`), estados (consultas por negocio con RLS)
  y mensajes. Formato en `evals/journeys/README.md`; reutilizable por otros sets (`--set`).
- Set `suscripciones` (8 journeys): informar planes, comprar Free, comprar Basic y pagar, cargo del período
  siguiente, mora con recordatorio y reactivación, pausa, cancelación y cambio de plan.
- CI: gate con `01,05` (umbral 100 %). Nightly: set completo con umbral 95 % y reporte JSON como artefacto. La
  corrida se guarda en `eval_runs` (`journeys:<set>@<versión del spec>`).
