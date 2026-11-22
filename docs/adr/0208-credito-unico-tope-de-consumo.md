# ADR-0208 — El crédito es el único tope de consumo: topes estructurales, colchón para conversaciones abiertas y tarifario v2

- **Estado:** propuesto (HU-BILL-17, 2026-11-22).
- **Fecha:** 2026-11-22
- **Contexto:** D-79 y SPEC-51 (`solvershub-docs`, #291), HU-BILL-18 a HU-BILL-21.
  **Enmienda:** ADR-0142 (métricas y punto de enforcement), ADR-0144 (deja de ser sombra; fees desde
  `messages`; tarifario `v1`) y ADR-0045 §2 (registro de métricas).
  **Usa:** ADR-0008 (ledger solo de inserción), ADR-0017 (textos de bloqueo en configuración),
  ADR-0117 §4 (episodio), ADR-0209 (saldo).

## Contexto

Hay dos sistemas a la vez:
- Los créditos se miden en `usage_ledger` (ADR-0144), pero solo bloquean la importación de fotos (`core/qa/importar_fotos.py`).
- Lo que corta el servicio son los topes de volumen de v1: `mensajes` y `costo_llm_usd` en el turno (`core/billing/enforcement.py:38`, `:202`), `conversaciones` en `core/orchestrator/consumer.py:766`, y audios e imágenes en `core/gateway/webhooks.py:923-950` y `core/waweb/inbound.py:92-105`.

Además, parte del gasto no llega al crédito:
- `agent_run`, embeddings, TTS y la caché no se cobran, o se cobran mal;
- notas de voz e imágenes pagan un fee fijo calculado desde `messages`.

D-79 fija que el plan concede créditos y que todo lo que gasta el negocio los consume.

## Decisión

**El crédito es el único tope de consumo del negocio. Los demás topes del plan son estructurales y solo bloquean un alta, nunca un turno.**

### Retiro de los topes de volumen
1. **Salen del registro** (`core/billing/metrics.py`): `conversaciones`, `conversaciones_audio`, `conversaciones_ocr`, `mensajes`, `costo_llm_usd` y `plugins`. `plugins` ya no la define ningún plan.
   - También salen sus puntos de enforcement: el de `consumer.py:766`, el `_media_metric`/`check_metric` de `webhooks.py` y el de `waweb/inbound.py`. Una nota de voz o una imagen sin saldo se guarda como cualquier mensaje.
   - Las funciones de conteo pueden quedar como lectura para tableros, sin tope.
   - **Se quedan:** `clientes` (`consumer.py:653`), `documentos` y `conocimiento_mb` (ADR-0045).

### El turno consulta el saldo
2. **Qué decide.** `turn_quota_metric` (`core/billing/enforcement.py:202`) deja de recorrer `TURN_METRICS`. Pasa a devolver una decisión con tres salidas: **normal**, **colchón** o **agotado**. Recibe la conversación para saber si está abierta (punto 4).
   - **El cálculo:** es el de `alcanza` (`core/billing/creditos.py:251`) sobre el saldo de ADR-0209. En el turno se lee sin el lock consultivo (*advisory lock*): una carrera entre dos turnos se resuelve dentro del colchón.
   - **Con `BILLING_BLOQUEAR_POR_CREDITOS=false`** (`core/config.py:391`, verdadero por defecto), el turno mide y deja pasar.
   - **Despliegue:** en test primero; en producción la bandera va apagada hasta la calibración de HU-BILL-29.
3. **Conversación nueva sin saldo.** El turno queda bloqueado con la respuesta `quota_exhausted` de configuración (`core/kernel/ingress.py:84-96`, ADR-0017) y no se llama al LLM.
   - **Cuántas veces se responde:** una sola vez por episodio, como el `primer_aviso` de `ingress.py:269`. Los mensajes siguientes del mismo episodio no reciben respuesta.

### Colchón para conversaciones abiertas (D-79, P-06 opción A)
4. **Qué es una conversación abierta.** Se define con filas observables, sin guardar el instante en que se acabó el saldo. Es una conversación con el episodio vigente (`conversations.episode_started_at`, `core/kernel/episodio.py`) que tiene al menos una fila de `usage_ledger` con:
   - `kind = 'turn'`;
   - `created_at >= episode_started_at`;
   - `input_tokens + output_tokens > 0`, el mismo criterio que `hubo_llamada` (`creditos.py:82`).

   **Por qué el filtro de tokens es obligatorio:** un turno bloqueado escribe una fila con 0 tokens (`core/orchestrator/loop.py:217`). Si contara, la respuesta `quota_exhausted` abriría la conversación y el cliente entraría al colchón con solo escribir dos veces.

   **Dónde y cuándo se evalúa:** en `politicas_del_turno` (`loop.py:358`), que corre después de `fijar_release_del_turno` (`loop.py:332`). Esa función ya abrió el episodio con `FOR UPDATE` (`core/kernel/release_del_turno.py:330-354`), así que no aparecen carreras nuevas.

   **Trampa a evitar:** el `episode_started_at` tiene que venir del `Episodio` de esa asignación o de una relectura. La fila `conversation` de `loop.py:314` se leyó antes de abrir el episodio y lo trae desactualizado.
5. **Colchón.**
   - **Tamaño:** el 5 % de `credito_mensual` del plan por ciclo. Los planes sin crédito mensual no tienen colchón.
   - **Cómo se mide:** derivado, sin columna ni contador: `colchón usado = max(0, consumido − saldo)` en la ventana del ciclo.
   - **Quién lo paga:** no se arrastra como deuda al ciclo siguiente; la plataforma absorbe lo que quede descubierto al cierre (ADR-0209 §3). Si el negocio compra una recarga en el mismo ciclo, esa recarga cubre primero lo consumido en el colchón.
6. **Colchón agotado.** El turno no llama al LLM y la conversación pasa a una persona por el camino sin modelo que ya usa el ingreso: `core.orchestrator.escalation.escalate(conversation_id, reason=..., summary=...)`, con `bloquear(..., status="escalated")` y `marcar_escalado()` (`ingress.py:219`, `:295-314`). `escalate_to_human` es una tool del LLM y no aplica aquí.
   - **Motivo nuevo:** `creditos_agotados` se suma a `MotivoCodigo`, `ETIQUETAS` y `_EXACTOS` (`core/orchestrator/motivos_escalacion.py`). Sin eso cae en «otro».
   - **Texto al cliente:** tiene su clave en las respuestas de configuración (invariante 3, ADR-0017).
   - **Después de una recarga:** la conversación sigue escalada hasta que una persona la devuelva al agente, como cualquier escalación (`conversacion_bloqueada`, `ingress.py:68`). El panel lo explica.
7. **Avisos al dueño.** Al 80 %, al 100 % y al entrar al colchón, por el centro de notificaciones (SPEC-38). Cada aviso tiene una clave idempotente por negocio, umbral y ciclo.

### Topes estructurales
8. **Métricas nuevas.** Las dos son de alcance `lifetime`, se suman a `GROWTH_METRICS` y se exigen con `enforce_growth` (`enforcement.py:145`) en **todas** las altas de conexión:
   - `core/gateway/connections.py:285` (`save_connection_in`) y `:461` (`set_active`);
   - `core/waweb/connections.py:145`;
   - `core/gateway/chat_web_conexion.py:182`;
   - `core/gateway/conexion_canonica.py:101` y `:226`, los números de Meta (los cinco caminos de ADR-0122 §6);
   - `core/orchestrator/consumer.py:743` (`asegurar_conexion_en_vuelo`).

   **Lo que no cuenta como alta:** volver a ligar la misma fila canónica. El canal de prueba (`core/gateway/conexion_pruebas.py:89`) queda exento.

   **Cuándo se mide en Meta:** antes de empezar el OAuth, para no cortar al dueño después de completarlo.

   - **`conexiones`:** conexiones activas de canales conversacionales. No cuentan `email` ni el canal `test`.
   - **`correos`:** conexiones activas con `channel = "email"`.
   - **Respuesta:** 409 `plan_limit_conexiones` / `plan_limit_correos` con `{metric, used, limit}` (`core/api/plan_limits.py`). Bajar de plan no apaga lo que ya existe, solo impide crecer (igual que ADR-0142).
9. **Canales.** `plan_incluye_canal` (`core/gateway/meta_messenger_alta.py:125`) se mueve a `core/billing` y se exige en las mismas altas del punto 8, con 409 `plan_channel_not_included`.
   - **El vocabulario es `ChannelConnection.channel` tal cual:** `whatsapp`, `instagram`, `messenger`, `telegram` y `web`. El catálogo v3 (`core/billing/plans.py`, HU-BILL-18) lista en `features.canales` **todos** los canales de cada plan:
     - Free: `whatsapp`, `web`;
     - Basic: + `telegram`;
     - Premium: + `instagram`, `messenger`;
     - Empresarial: todos.

     Sin esto, Telegram y el chat web quedarían bloqueados, porque hoy el catálogo solo nombra `whatsapp`, `messenger` e `instagram`, y `web` solo en Empresarial.
   - **WhatsApp Web y Twilio** cuentan como `whatsapp`.
   - **Quedan exentos** `test` y `email`, que ya tiene su tope en `correos`.
   - **Enmienda** la decisión del dueño del 2026-10-23: Messenger e Instagram pasan a ser solo de Premium y Empresarial.
10. **Habilidades.** El `min_plan` del pack (`core/mcp/types.py`) se exige al habilitar una habilidad en un agente, no solo al aplicar un preset, con 409 `plan_plugin_not_included`.
    - **Revierte** la nota «NO valida `min_plan`» de `core/agents/habilitacion.py:156`.
    - **Sale la cuota `plugins`:** `habilitacion.py:173-175`, `core/presets/apply.py:72`, `enforcement.py:198` y la fila de `core/admin/templates/plan.html:86`.
    - **Cambio de contrato:** desaparece el 409 `plan_limit_plugins`, que el panel consume, y se registra en `docs/openapi.json`.

### Todo el gasto cuenta: tarifario `v2`
11. **Cada gasto escribe su fila con créditos congelados al escribir** (ADR-0144 §3):
    - `agent_run` (`core/kernel/a2a.py:586`) los fija al escribir, igual que `turn`;
    - el TTS (`core/media/tts.py`, pack-voice) escribe su fila con un `kind = 'tts'` nuevo (`core/billing/tipos_de_uso.py`);
    - `embedding`, `stt` y `vision` dejan la sombra (`credits = 0`) y cobran por tarifario.
12. **Sin cobro doble y sin cambiar el pasado.** El fee de nota de voz y de imagen calculado desde `messages` (ADR-0144 §6) se cobra solo a los mensajes anteriores a `vigente_desde` del tarifario `v2`. Desde esa fecha cobra su fila del ledger. Los ciclos cerrados no cambian. El fee de plantilla sigue derivado de `messages` hasta que tenga su fila propia.
13. **Caché.** El costo del LLM cobra los `cached_tokens` a su precio de caché. Hoy `estimate_cost_usd` (`core/llm/base.py:41`) solo mira entrada y salida.
14. **Versión del tarifario.** Los cambios de los puntos 11 a 13 publican el tarifario **`v2`** (`core/billing/tarifario.py`) con su `vigente_desde`.
    - `v1` no se edita: las filas viejas se interpretan por su `tarifario_version`.
    - Las filas con `credits` NULL y sin `tarifario_version` (`creditos.py:105`, que hoy usa `TARIFARIO_VIGENTE`) se fijan a `v1`.
    - El margen (×3) y la TRM por setting no cambian.
15. **Lo que no cuenta o cuenta aparte.**
    - **`eval`:** sigue a cargo de la plataforma (ADR-0117).
    - **El playground** (canal `test`, `_sin_playground` en `creditos.py:67`): es gratis hasta **300 créditos por ciclo**. Lo que pasa de ahí descuenta del saldo como cualquier gasto. Se mide en la misma ventana y sin contador: `playground cobrado = max(0, consumo del playground − 300)`.
16. **Gastos que no pasan por el turno.** La ingesta de conocimiento (`embedding`), `generacion` (`core/ia/estructurado.py`), `agent_run` y el TTS consumen saldo pero no pasan por el punto 2.
    - **Lo que se pide antes de gastar:** `generacion` y la importación de fotos ya verifican `alcanza` (D-71). La ingesta de conocimiento también lo hará, con 409 `plan_limit_creditos`.
    - **Lo que no se corta:** `agent_run` y el TTS ocurren dentro de un turno ya admitido y no se interrumpen. Si se pasan del saldo, lo absorbe el colchón o, fuera de él, la plataforma al cierre.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mantener los topes de volumen junto al crédito | Son dos fuentes de verdad que se contradicen. El servicio se corta por un número (mensajes) que no refleja el costo |
| Corte inmediato al llegar a cero (P-06 B) | Deja a un cliente a mitad de una compra o una cita. El dueño eligió el colchón |
| Colchón con columna o contador propio | Sería un contador paralelo (ADR-0045). Derivarlo del consumo y el saldo da el mismo número y es reproducible |
| Gracia por tiempo (24 h) | El consumo no tiene techo durante la gracia |
| Seguir cobrando voz e imagen con fee fijo desde `messages` | Es mutable si cambia el fee (ADR-0144 §6 ya lo marcaba interino) y no refleja el costo de un modelo más caro |
| `min_plan` solo en presets | Cualquiera puede habilitar cobranza en Free a mano. D-79 pide las habilidades como diferenciador del plan |

## Consecuencias

**Positivas:**
- Una sola unidad de consumo, que refleja el costo real.
- El cliente no queda colgado a mitad de una conversación.
- Los planes se diferencian por estructura (agentes, equipo, conexiones, correos, canales y habilidades) y por beneficios, no por contadores.
- El ledger sigue siendo la única fuente de verdad del consumo.

**Negativas / lo que aceptamos pagar:**
- El colchón (hasta el 5 % del crédito mensual) y el playground (hasta 300 créditos) los absorbe la plataforma.
- El turno suma una lectura del saldo. Es la misma consulta indexada `(tenant_id, created_at)` que ya hace `/admin/plan`.
- **Los negocios de test cambian de comportamiento:**
  - Basic pierde Messenger e Instagram para conexiones nuevas;
  - Telegram y el chat web pasan a validarse contra el plan: el catálogo v3 los incluye desde Free (chat web) y Basic (Telegram); si una conexión de Telegram existe hoy en Free, sigue viva pero no se crean nuevas;
  - un negocio con habilidades por encima de su plan las conserva, pero no puede habilitarlas en otro agente.
- El replay de las evals puede cambiar si un escenario dependía de `quota_exhausted` por mensajes. Se regrabaría solo con OK del dueño.

**Qué haría que revisáramos esto:**
- La calibración (HU-BILL-29) muestra más de un 20 % de diferencia entre los créditos reales por conversación y los 9,9 del modelo.
- El colchón supera el 2 % del consumo mensual de la plataforma durante dos ciclos seguidos.
- Un negocio encuentra la forma de usar el playground como canal de atención.

## Impacto en el código

- `core/billing/metrics.py`, `enforcement.py`, `creditos.py`, `tarifario.py`, `tipos_de_uso.py` y `plans.py` (catálogo v3, HU-BILL-18).
- `core/kernel/ingress.py` y `core/orchestrator/loop.py` (`politicas_del_turno`): colchón y paso a una persona.
- `core/orchestrator/consumer.py`, `core/gateway/webhooks.py` y `core/waweb/inbound.py`: salen los topes.
- `core/gateway/connections.py` y `core/gateway/meta_messenger_alta.py`: `plan_incluye_canal` se mueve a `core/billing`.
- Todas las altas de conexión: `core/waweb/connections.py`, `core/gateway/chat_web_conexion.py`, `core/gateway/conexion_canonica.py` y `core/orchestrator/consumer.py`.
- `core/agents/habilitacion.py`.
- Las fuentes del ledger: `core/kernel/a2a.py`, `core/llm/embeddings.py`, `core/llm/base.py`, `core/media/ingesta.py`, `core/media/tts.py` y pack-voice.
- `core/api/plan_limits.py`: los 409 nuevos.
- `docs/openapi.json`.
- `core/notifications/`: los avisos.
- **Una migración, aditiva (HU-BILL-21):** suma `'tts'` al CHECK `ck_usage_kind` de `usage_ledger`, que hoy fija la lista de `kind` (migración 0129). Es la primera de las tres de SPEC-51; ADR-0209 §6 da el orden.
- `core/orchestrator/motivos_escalacion.py` (motivo `creditos_agotados`), `core/presets/apply.py` y `core/admin/templates/plan.html` (sin `plugins`).
