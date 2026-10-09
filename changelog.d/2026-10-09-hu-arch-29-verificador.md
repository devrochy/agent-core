## Verificador de afirmaciones con modelo de evidencia (HU-ARCH-29, 2026-10-09)

- **Nuevo:** `core/orchestrator/verificador/`. Antes de enviar cada respuesta, el loop compara el borrador con lo que
  el sistema hizo (ADR-0119, adenda de implementación):
  - **Montos:** toda cifra en `$`/`COP` tiene que haber salido de una tool, de este turno o de uno anterior. Si no,
    reintento con los montos verificados del turno.
  - **Afirmaciones de cierre** que declaran los packs ("tu pedido quedó confirmado", "te envío el link de pago"):
    exigen una tool del turno que las respalde. Las preguntas, las negaciones ("todavía no generé el link") y los
    condicionales ("para generarte el link necesito…", "con tu sí te lo envío") ya no cuentan como afirmación.
  - **Links:** una URL que no devolvió una tool del turno ni está en el prompt o en los mensajes del cliente se quita
    del texto, sin reintento. El link exacto lo sigue entregando `_safe_append`.
  - **Referencias y fechas:** se cuentan en el veredicto, sin bloquear (modo sombra).
  - Mismo reintento (`guardrail_max_retries`) y misma acción al reincidir (`guardrail_fail_action`, ADR-0092). Si el
    verificador falla, el turno sigue (fail-open para montos y cierres) y los links se quitan todos.
- **Retirados** `crm_amount_check` y `crm_completion_check` de pack-commerce. Sus montos y sus afirmaciones pasan al
  verificador, y pack-commerce las declara con `MCPHost.register_afirmaciones`. El link de pago lo respalda cualquier
  tool del turno que haya devuelto un link (antes, `sub_cambiar_plan` escalaba compras válidas). Nuevas
  afirmaciones `cotizacion_generada` y `cotizacion_aceptada` ("Ya dejé tu cotización aceptada" sin
  `confirmar_cotizacion`, visto en los journeys). `calendar_completion_check` sigue igual.
- **Interruptores por negocio:** `verificador_montos`, `verificador_cierre` y `verificador_links` en "Guardrails
  activos". Los nombres viejos se traducen solos: un negocio que había apagado `crm_amount_check` lo mantiene apagado.
- **Veredicto por turno** en `audit_log` (`core:verificador` / `verificacion`), solo códigos y conteos.
- **Medición:** `python evals/verificador.py` corre el verificador sobre el set etiquetado
  `evals/verificador/respuestas_etiquetadas.jsonl` (borradores reales de los journeys con sus resultados de tools, más
  los incidentes reales). `tests/test_verificador_set.py` exige falsos positivos ≤ 2 % y ningún falso negativo.
  Hoy: 205 correctas con 0 falsos positivos y 14 falsas con 0 falsos negativos. En los mismos 90 borradores, los
  guardrails viejos tenían 15 falsos positivos (16,7 %). Journeys locales con k = 4: pass@1 95 %, pass^4 91 %.
- **Seguridad** (revisión del diff): las URLs se comparan normalizadas e incluyen las que empiezan por `www.`; se
  quitan por posición hasta que no quede ninguna sin respaldo; se verifica el texto ya sin markdown; la memoria y el
  perfil que escribe el asistente no cuentan como evidencia; si el verificador falla se quitan todos los links;
  `parse_money` descarta cifras no finitas y el patrón `cerr… pedido` deja de ser cuadrático ante espacios.
- `core/shared/dinero.py`: `parse_money`, `MONEY_KEYS` y `MONEY_IN_TEXT` pasan de pack-commerce al core
  (`docs/PLUGIN-CONTRACT.md` §11.1 y §13).
- Instrucciones de reintento en el playbook `respuestas` (`verificador_monto_sin_evidencia`,
  `verificador_status_sin_evidencia`).
