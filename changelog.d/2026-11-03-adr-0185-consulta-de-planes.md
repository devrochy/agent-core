## Informar planes sin iniciar la contratación (2026-11-03)

- ADR-0185 (aceptado), SPEC-43 · HU-SUB-37. Con el motor de procedimientos activo, `sub_listar_planes` deja de ser una acción solo de procedimiento: es una consulta de solo lectura que se ofrece también sin tarea en foco, así que un cliente que solo pregunta por planes recibe la respuesta del catálogo en una vuelta, sin abrir `contratar_plan` ni escalar. Cotizar, confirmar e inscribir siguen detrás del procedimiento. Sin el motor nada cambia (la tool no se ofrece, y no se tocan las cassettes legacy).
- El playbook `planes` dice que se informa con `sub_listar_planes` directo y que `contratar_plan` se inicia cuando el cliente quiere el plan.
- Se regraba la cassette `suscripciones/01-informar-planes` del gate (cambió el conjunto de tools del turno).
- Pendiente aparte: `precio` como clave de dinero del verificador (hoy un «$200.000» tomado del listado se bloquea por `monto_sin_evidencia`).
