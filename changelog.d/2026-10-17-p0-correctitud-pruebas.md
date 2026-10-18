## P0 de correctitud del ciclo de pruebas 2026-10-13 (2026-10-17)

- **Formularios ya no aceptan negativos.** Los modelos de entrada y las configuraciones de los
  packs llevan el rango explícito (`ge`/`le`): reglas de cobranza (prórroga, reprogramaciones,
  abono mínimo, umbral) y su config (tolerancia, anticipación, mora, aviso); calendar (duración,
  bloque, margen, recordatorios, duración de tipos de cita); commerce (seguimiento, conteos y
  días de segmentación, montos, mínimo de pago en línea, precio, stock, días de prueba, orden);
  payments (vigencia del link); subscriptions (días de prueba, precio); AppSettings (reintentos
  de guardrails, umbrales de dificultad, turnos de contexto 1–200, top-k 0–20, umbral 0–1,
  temperatura 0–2, tools por turno 1–50, presupuesto); `extend_days` de la plataforma.
  `GET /admin/plugins/{id}/config` ya no revienta con una config guardada antes fuera de rango.
- **Errores de validación con el sobre de la API.** Bajo `/api/v1` un cuerpo inválido responde
  422 `validation_error` con `error.fields` por campo en español (`core/shared/validacion.py`),
  y la config de un pack inválida responde 422 `invalid_config` con los mismos campos (antes
  400 con mensajes en inglés). `ApiError` admite `details` (estado real para el cliente).
- **Habilidad al tope del plan:** 409 `plan_limit_plugins` con `details = {metric, used,
  limit, enabled: false}` (antes 403); la cuota se decide antes de guardar la configuración.
- **Plantillas de WhatsApp:** el botón «Sincronizar» ya no espera el candado horario del cron
  (lo toma al terminar) y se limita a una vez por minuto (429 `sync_rate_limited`). Eliminar una
  plantilla que Meta no permite borrar (las de ejemplo, `(#100) Invalid parameter`) responde 409
  `template_not_deletable` con el motivo y la fila no se marca eliminada; ningún error de red
  termina en 500.
- **Conexiones de Meta:** los números dados de baja ya no aparecen en el listado ni cuentan
  (`deleted_at`); el límite de números al agregar uno se explica como límite de Meta (no hay
  cuota de números por plan); `primary` sobre una cuenta de Meta responde 409
  `primary_not_applicable` con la regla (ADR-0069/0115); el alta BYOK con la misma app y
  entorno devuelve `reused: true` porque actualiza la conexión existente en vez de crear otra.
