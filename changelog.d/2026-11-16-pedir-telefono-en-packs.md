## Cuándo pide el teléfono el agente en cobranza, citas y suscripciones (HU-CONN-26) (2026-11-16)

- **Cobranza** (`pack_cobranza` 0.7.0, playbook `cobranza.md`):
  - Si `cob_consultar_saldo` o `cob_estado_cuenta` no encuentran al cliente, el agente llama `pedir_telefono` una vez y vuelve a consultar cuando el cliente comparte el número.
  - En un canal sin botón, o si el cliente no lo comparte, no le pide que escriba el número (un número escrito no encuentra su cuenta) y le ofrece un gestor.
- **Enlace de cobranza** (adenda de ADR-0179 §7):
  - El camino perezoso (`enlazar_customer`) acepta también el teléfono verificado de Telegram. El alta y el backfill siguen solo con WhatsApp.
  - Un cliente de Telegram sin teléfono ya no deja caché negativo (resultado `sin_telefono`): al compartirlo se enlaza en el turno siguiente.
  - `tests/test_contact_phone_escritores.py` fija quién escribe `Customer.contact_phone`.
- **Citas** (`pack_calendar` 2.5.0, procedimiento `agendar_cita` 1.1.0, etapa `datos`):
  - El teléfono se pide solo si la política del negocio lo pide (por defecto `no_pedir`), con `pedir_telefono` si la tool existe y, si no, por texto.
  - No reemplaza el correo.
- **Suscripciones** (`pack_subscriptions` 0.7.0, procedimiento `contratar_plan` 2.1.0, etapa `datos`):
  - Si el perfil ya tiene `contact_phone`, el agente lo usa para `telefono` sin pedirlo. **Cambio visible también en WhatsApp:** ahí el número siempre está, así que ya no se le pregunta al cliente.
  - Sin número, lo pide con `pedir_telefono` o, si el canal no tiene botón, dentro de la lista numerada.
- **Evals:**
  - El golden admite `spec` (otro spec de `evals/specs/`) y `connection_types` por escenario.
  - Escenarios nuevos: `37-cobranza-pide-telefono-telegram` (`must_call_tool: pedir_telefono`) y `38-cobranza-sin-boton-no-pide-numero`.
  - Sin grabación todavía y fuera del gate hasta el OK del dueño. El gate en replay no cambia.
- Las reglas de citas y suscripciones van en la etapa del procedimiento y no en `citas.md` ni en `planes.md`, para no cambiar el prompt de los journeys grabados.
