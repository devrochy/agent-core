# ADR-0150 — Envío de plantillas a un segmento de Clientes y consentimiento para promociones

- **Estado:** propuesto (HU-CRM-06, SPEC-33). Decisiones del dueño del 2026-10-21: D-SPEC-33-5 (consentimiento con el interruptor de la ficha), D-SPEC-33-7 (tope de 200 por envío y 7 días entre promociones al mismo cliente) y D-SPEC-33-8 (solo el propietario envía; la promoción por segmento sale aparte, solo panel). D-SPEC-33-6 (siempre plantilla) se toma con la recomendación y la confirma el dueño al aceptar este ADR.
- **Fecha:** 2026-10-21
- **Contexto:** SPEC-33 «CRM» en `solvershub-docs`, HU-CRM-06. **Usa:** ADR-0107 (outbox de dominio), ADR-0108 (plantillas por evento: variables, `clean_param`, `client_message_id` estable), ADR-0122 (conexión pausada), ADR-0047 (permisos), ADR-0144 (fee `template_sent`). **Enmienda:** ADR-0147 (fuente nueva de la línea de tiempo) y se aparta de la regla 1 de ADR-0108 (ver §4).

## Contexto

Clientes (`/leads`) filtra por canal, conexión, clasificación y estado del lead (HU-UX-16), pero no deja actuar sobre el resultado. En agent-sale, desde Leads se crea una promoción para la clasificación del cliente y se le manda una plantilla de promoción **a un cliente por vez** («Envío individual, no masivo»). El dueño pidió llevarlo a SolversHub, y para todo el filtro.

Antes de este ADR no había consentimiento para recibir promociones: solo el hecho `optin` del formulario web, que se guarda en un cliente del canal web (los de WhatsApp nunca lo tienen). Tampoco había envío masivo, idempotencia en el envío de una plantilla del operador ni manejo de la baja.

## Decisión

1. **Consentimiento en el core** (`customer_consents`, migración 0100), **solo inserción**: la última fila por cliente y propósito manda.
   - Guarda estado (`otorgado`/`revocado`), origen (`ficha`, `palabra_de_baja`, `meta_131050`), evidencia, quién y cuándo. La Ley 1581 pide poder probar la autorización. Un hecho de memoria no sirve: se reemplaza, se olvida (ADR-0053/0114) y llega al prompt.
   - Va en el core porque lo escriben el panel, el inbound y la cola de salida. HU-CHAN-03 lo reutiliza.
   - **Cómo nace:** el dueño lo marca en la ficha y elige cómo lo obtuvo (`POST /admin/leads/{id}/consentimiento`).
   - **Cómo se pierde:**
     - El cliente escribe un mensaje que es **entero** una palabra de baja. Hay valores por defecto y el negocio los cambia en `behavior_config.palabras_de_baja`.
     - Meta rechaza el envío con 131050 (el usuario dejó de recibir marketing).
   - Escribirle al negocio **no** es consentimiento.
2. **Envío en tres pasos con instantánea.**
   - La vista previa reutiliza `_consulta` de Clientes bajo `tenant_scope` y guarda el envío en `borrador` con su lista de destinatarios (`segment_send_recipients`).
   - Confirmar (`Idempotency-Key`, único por negocio) cambia solo el estado. No hace falta un token: lo que se envía es lo que el dueño vio.
   - La vista previa vence a los 10 minutos.
   - **Exclusiones por motivo:** no es de WhatsApp, sin conversación en esa conexión, sin consentimiento, se dio de baja, escalación abierta, promoción en los últimos 7 días, no permitido en test, sin nombre ni valor de respaldo.
3. **Un evento del outbox por lote, no por destinatario.**
   - El despachador de `domain_events` toma 200 eventos por vuelta, cada 30 s, para todos los negocios: mil eventos de un envío demorarían los pagos de los demás.
   - Cada lote (`segmento:<envio>:lote:<n>`, 25 destinatarios) envía y emite el siguiente. El despacho cada 30 s da el ritmo, sin cola ni cron nuevos.
   - La tabla de destinatarios es la cola durable.
   - **Idempotencia por destinatario:** `client_message_id = segmento:<envio>:<cliente>` contra `uq_messages_tenant_client_message_id`.
   - Antes de enviar a cada destinatario se vuelven a mirar la baja, la pausa de la conexión y la pausa por uso de la API. Con esta última el sender descarta el id estable, así que el lote se reintenta sin crear el mensaje.
4. **Siempre plantilla `MARKETING` aprobada, nunca texto libre**, con la ventana abierta o cerrada.
   - Se aparta de la regla 1 de ADR-0108, que manda texto con la ventana abierta. Un texto promocional a muchos clientes es spam aunque la ventana esté abierta, y con el precio por mensaje de Meta una plantilla de marketing cuesta lo mismo con la ventana abierta o cerrada.
   - Solo plantillas con variables en el cuerpo.
   - Cada variable es un valor fijo del dueño o el nombre del cliente (acotado a 60 caracteres, con un respaldo). Nunca sale del LLM.
5. **Topes** (D-SPEC-33-7):
   - 200 destinatarios por envío; por encima no se puede confirmar.
   - Un solo envío en curso por conexión, garantizado por el índice único parcial `uq_segment_sends_en_curso`.
   - 7 días entre promociones al mismo cliente.
   - El límite diario del número (`messaging_limit_tier`) solo se informa: cuenta también los avisos.
6. **Permiso:** `require("leads", write=True)` más `require_owner_or_master`. Un colaborador no ve la acción y recibe 403.
7. **Guarda fuera de producción real:** en desarrollo y en test (`QA_TESTERS_ENABLED`) solo reciben los números de `SEGMENT_SEND_ALLOWLIST`. Con la lista vacía no sale nada.
8. **Una promoción que no llega no escala** la conversación ni pausa al agente (`outbound_send._mark_failed`).
9. **Línea de tiempo** (enmienda ADR-0147): fuente `core.plantilla` desde `Message.template_name` (plantillas del envío, del operador y de los avisos), con un LEFT JOIN al envío para «quién la mandó». El estado sale del `Message`, que actualiza el webhook. Usa el índice existente `ix_messages_tenant_conv_created`.
10. **Promoción por segmento:** no cambia nada en el backend. `eligible_segments` de pack_commerce ya existe y `aplicar_promocion` lo respeta. Es un PR del panel aparte.

## Consecuencias

- **Costo:** cada plantilla cobra el fee `template_sent` (0,3 créditos). Meta cobra el marketing aparte. Sin HU-BILL-06 no hay bloqueo por créditos: la vista previa solo informa el costo estimado. El dueño lo acepta con este ADR.
- **El envío arranca casi sin destinatarios:** solo cuentan los clientes que el dueño marque en la ficha.
- **Si la API de Meta queda en pausa por uso** más allá de los 5 reintentos del outbox (unos 20 min), el lote queda `fallido` en la Bandeja de fallos y el envío, `enviando`, bloquea esa conexión hasta reintentarlo a mano.
- **Borradores:** las vistas previas que no se confirman quedan como filas `borrador`, sin borrado físico (invariante 8).
- **La reversa de la 0100** se niega si hay envíos o consentimientos.
- **Pendiente para HU-CHAN-03:** programar envíos, segmentos guardados, métricas de ventas atribuidas y límite por minuto configurable.
