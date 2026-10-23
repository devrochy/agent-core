## Messenger: salida por la Página, ventana de 24 h y etiqueta HUMAN_AGENT (ADR-0152, HU-CHAN-01) (2026-10-23)

Segundo paso del rescate de Messenger: la parte de envío, sin el alta ni la UI (siguen).

- `core/gateway/channels/registry.py`: adaptadores de salida por `(proveedor, canal)`. `("meta", "messenger")` usa
  el nuevo `MessengerMetaOutbound`; `meta` sin canal sigue siendo WhatsApp. **Instagram sin adaptador ya no cae al
  de WhatsApp** (mandaría el identificador del cliente como si fuera un teléfono): falla explícito y permanente.
- `core/gateway/meta_messenger.py` + `channels/messenger/outbound.py`: Send API de la Página con el token de la
  PROPIA Página. `plan_de_envio` decide antes de llamar a Meta: `RESPONSE` dentro de las 24 h; fuera, `HUMAN_AGENT`
  solo si lo envía una persona y hasta 7 días desde el último mensaje del cliente; si no, error permanente sin
  llamada ni reintentos. Solo `HUMAN_AGENT` (verificado contra la Send API el 2026-10-23): no se admiten
  `CUSTOMER_FEEDBACK` (exige su plantilla) ni las tres etiquetas retiradas en 2026.
- `core/gateway/sender.py`: `resolve_credentials` resuelve la Página de la conexión canónica (pausa, borrado y
  tenant como WhatsApp; Página viva de esa cuenta; sin respaldo a otra Página, a WhatsApp ni al `.env`).
  `validate_service_window` distingue al asistente de una persona en Messenger e Instagram.
- **Corrección:** `consumer.py` solo renovaba la ventana de 24 h en WhatsApp; en Messenger e Instagram quedaba sin
  ventana y el validador la trataba como abierta para siempre. Ahora la renuevan los tres canales de Meta.
- Sin migración ni cambios de API.
