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
2. **Qué devuelve.** `turn_quota_metric` deja de recorrer `TURN_METRICS` y devuelve `"creditos"` cuando lo consumido en el ciclo alcanza el saldo. Qué es el saldo lo define ADR-0209.
   - El cálculo es el de `alcanza` (`core/billing/creditos.py:251`), pero en el turno se lee sin el lock de asesoría: una carrera entre dos turnos se resuelve dentro del colchón, nunca pasa a deuda.
   - Con `BILLING_BLOQUEAR_POR_CREDITOS=false` (`core/config.py:391`, verdadero por defecto) el turno mide y deja pasar.
3. **Conversación nueva sin saldo.** El turno queda bloqueado con la respuesta `quota_exhausted` de configuración (`core/kernel/ingress.py:84-96`, ADR-0017). No se llama al LLM.

### Colchón para conversaciones abiertas (D-79, P-06 opción A)
4. **Qué es una conversación abierta.** La que tiene el episodio vigente (`core/kernel/episodio.py`, `episode_started_at`, `episode.idleMinutes`, 720 min por defecto) con al menos un turno atendido **antes** de que el saldo llegara a cero. Una conversación que reabre después de un episodio nuevo cuenta como nueva.
5. **Colchón.**
   - **Tamaño:** el 5 % de `credito_mensual` del plan por ciclo. Planes sin crédito mensual no tienen colchón.
   - **Cómo se mide:** es derivado, sin columna ni contador: `colchón usado = max(0, consumido − saldo)` en la ventana del ciclo.
   - **Quién lo paga:** no se cobra, no se descuenta de la siguiente recarga ni pasa al ciclo siguiente. Lo absorbe la plataforma.
6. **Colchón agotado.** El turno no llama al LLM y la conversación pasa a una persona por el camino de `escalate_to_human`, con motivo de créditos agotados. El texto al cliente sale de configuración (ADR-0017).
7. **Avisos al dueño.** Al 80 %, al 100 % y al entrar al colchón, por el centro de notificaciones (SPEC-38). Cada aviso tiene una clave idempotente por negocio, umbral y ciclo.

### Topes estructurales
8. **Métricas nuevas.** Las dos son de alcance `lifetime` y se suman a `GROWTH_METRICS`. Se exigen con `enforce_growth` (`enforcement.py:145`) al crear o activar una conexión (`core/gateway/connections.py:285` y `:461`).
   - **`conexiones`:** conexiones activas de canales conversacionales. No cuentan `email` ni el canal `test`.
   - **`correos`:** conexiones activas con `channel = "email"`.
   - **Respuesta:** 409 `plan_limit_conexiones` / `plan_limit_correos` con `{metric, used, limit}` (`core/api/plan_limits.py`). Bajar de plan no apaga lo que ya existe; solo impide crecer (igual que ADR-0142).
9. **Canales.** `plan_incluye_canal` (`core/gateway/meta_messenger_alta.py:125`) se mueve a `core/billing` y se exige en **todas** las altas de conexión, no solo en Messenger e Instagram. Responde 409 `plan_channel_not_included`. Enmienda la decisión del dueño del 2026-10-23: Messenger e Instagram pasan a ser solo de Premium y Empresarial.
10. **Habilidades.** `min_plan` del pack (`core/mcp/types.py`) se exige al habilitar una habilidad en un agente, no solo al aplicar un preset. Responde 409 `plan_plugin_not_included`. Revierte la nota «NO valida `min_plan`» de `core/agents/habilitacion.py:156`.

### Todo el gasto cuenta: tarifario `v2`
11. **Cada gasto escribe su fila con créditos congelados al escribir** (ADR-0144 §3):
    - `agent_run` (`core/kernel/a2a.py:586`) los fija al escribir, igual que `turn`;
    - el TTS (`core/media/tts.py`, pack-voice) escribe su fila con un `kind = 'tts'` nuevo (`core/billing/tipos_de_uso.py`);
    - `embedding`, `stt` y `vision` dejan la sombra (`credits = 0`) y cobran por tarifario.
12. **Sin cobro doble.** El fee de nota de voz y de imagen calculado desde `messages` (ADR-0144 §6) se retira cuando su fila del ledger cobra el costo real. El fee de plantilla sigue derivado de `messages` hasta que tenga su fila propia.
13. **Caché.** El costo del LLM cobra los `cached_tokens` a su precio de caché. Hoy `estimate_cost_usd` (`core/llm/base.py:41`) solo mira entrada y salida.
14. **Versión.** Los cambios de los puntos 11 a 13 publican el tarifario **`v2`** (`core/billing/tarifario.py`). `v1` no se edita: las filas viejas se interpretan por su `tarifario_version`. El margen (×3) y la TRM por setting no cambian.
15. **Lo que no cuenta o cuenta aparte.**
    - **`eval`** sigue a cargo de la plataforma (ADR-0117).
    - **El playground** (canal `test`, `_sin_playground` en `creditos.py:67`) es gratis hasta **300 créditos por ciclo**. Lo que pasa de ahí descuenta del saldo como cualquier gasto. Se mide en la misma ventana y sin contador: `playground cobrado = max(0, consumo del playground − 300)`.

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
  - un negocio con habilidades por encima de su plan las conserva, pero no puede habilitarlas en otro agente.
- El replay de las evals puede cambiar si un escenario dependía de `quota_exhausted` por mensajes. Se regrabaría solo con OK del dueño.

**Qué haría que revisáramos esto:**
- La calibración (HU-BILL-29) muestra más de un 20 % de diferencia entre los créditos reales por conversación y los 9,9 del modelo.
- El colchón supera el 2 % del consumo mensual de la plataforma durante dos ciclos seguidos.
- Un negocio encuentra la forma de usar el playground como canal de atención.

## Impacto en el código

- `core/billing/metrics.py`, `enforcement.py`, `creditos.py`, `tarifario.py`, `tipos_de_uso.py` y `plans.py` (catálogo v3, HU-BILL-18).
- `core/kernel/ingress.py`: colchón y paso a una persona.
- `core/orchestrator/consumer.py`, `core/gateway/webhooks.py` y `core/waweb/inbound.py`: salen los topes.
- `core/gateway/connections.py` y `core/gateway/meta_messenger_alta.py`: `plan_incluye_canal` se mueve a `core/billing`.
- Todas las altas de conexión.
- `core/agents/habilitacion.py`.
- Las fuentes del ledger: `core/kernel/a2a.py`, `core/llm/embeddings.py`, `core/llm/base.py`, `core/media/ingesta.py`, `core/media/tts.py` y pack-voice.
- `core/api/plan_limits.py`: los 409 nuevos.
- `docs/openapi.json`.
- `core/notifications/`: los avisos.
- **Una sola migración, aditiva (HU-BILL-21):** suma `'tts'` al CHECK `ck_usage_kind` de `usage_ledger`, que hoy fija la lista de `kind` (migración 0129). Lo demás es código.
