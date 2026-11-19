# ADR-0132 — Modelo por riesgo: thinking con tools en etapas de riesgo alto y respaldo del verificador

- **Estado:** aceptado (dueño, 2026-10-11)
- **Fecha:** 2026-10-11
- **Contexto:** HU-AOS-09 (SPEC-26, design §7.1 "Política de modelo por riesgo"; D-SPEC-26-6).
- **Supersede parcialmente:** ADR-0086 §2 (thinking solo sin tools). **Usa:** ADR-0050 (fallback),
  ADR-0092 (reintentos y acción ante un fallo), ADR-0112 (banderas), ADR-0113 §3 (`modelPolicy`),
  ADR-0119 (verificador).

## Contexto

- ADR-0086 §2 fija el thinking de DeepSeek "solo cuando el difficulty router lo decide y el turno no
  requiere tools", y el router lo repite en código. La razón: no pagar razonamiento en cada respuesta.
- SPEC-26 pide más deliberación donde un error cuesta: una etapa `risk: high` de un procedimiento
  (crear el pedido, cobrar) es justo donde se llama la acción, así que sin tools no sirve.
- DeepSeek exige devolver `reasoning_content` en las vueltas siguientes de un turno con tools; si no,
  responde 400 (guía "Thinking mode" de DeepSeek, consultada el 2026-10-11).
- El design (§7.1) pide además que, tras un fallo del verificador, el reintento use el respaldo del
  agente (`model.fallback`). `_resolved_provider_fallback` reusa el proveedor y la clave del primario y
  solo cambia el modelo: con `deepseek-flash` y `gpt-4o-mini` de respaldo, ese reintento sale contra
  `api.deepseek.com` y falla siempre.
- Regla del dueño: producción con el costo más bajo posible.

## Decisión

Solo con el motor activo (`tenants.aos_procedures`); con la bandera apagada no cambia nada.

1. **Thinking con tools solo en etapas `risk: high`.** Si la etapa en foco **al empezar el turno**
   declara `risk: high`, las llamadas del turno usan `KernelSpec.modelPolicy.highRiskStage`
   (`thinking`, por defecto `true`, y `model`, por defecto el del agente). Fuera de esas etapas, el
   modelo de siempre, sin thinking (ADR-0086 §1 sigue igual). El caro solo si la etapa lo declara.
   - El modo se decide una vez por turno. La acción irreversible está detrás de la puerta de
     confirmación, así que se ejecuta en un turno que ya empezó en la etapa de riesgo alto.
   - Un `highRiskStage.model` de otra familia que el modelo del turno se ignora: el proveedor del
     negocio no lo sirve.
   - Si el router ya eligió el modo de razonamiento sin tools (`complex_signals`), la política no
     interviene.
2. **`reasoning_content`.** El proveedor lo lee de la respuesta y el loop lo devuelve en la vuelta
   siguiente, **solo** en llamadas de la política y solo hacia DeepSeek con thinking encendido (otro
   proveedor no conoce el campo). No se guarda en `messages`, en la memoria ni en los spans. El
   cassette de las evals lo incluye en la clave y en la grabación solo si existe, y lo escanea en
   busca de credenciales como el resto del texto.
3. **Degradación.** Si una llamada con thinking falla (`LLMProviderError`), el turno sigue hasta el
   final con el proveedor base, sin volver a intentar el thinking. En esa vía un 4xx distinto de 429
   no se reintenta (el request está mal y repetirlo cuesta unos 4,5 s). Si el base también falla, rige
   el fallback de ADR-0050 como hoy.
4. **Respaldo del verificador.** Cuando el verificador rechaza el borrador y queda reintento
   (`guardrail_max_retries`, ADR-0092), esa vuelta usa `model.fallback` **solo si es de la misma
   familia** que el modelo del turno (se reusa la configuración del negocio, BYOK o plataforma). Si es
   de otra familia, no se llama y se anota `respaldo_no_disponible`; si el respaldo falla, el
   reintento sigue con el base. No se usa la clave de OpenAI de la plataforma para chat: hoy solo la
   usan los embeddings y cambiarlo es una decisión de costo del dueño (ver Consecuencias).
5. **Registro.** `usage_ledger.model` es el de la última llamada (como hoy) y `usage_ledger.result`
   suma `politica_modelo` **solo si la política hizo algo**: `riesgo_alto` (llamadas), `modelo`,
   `thinking` efectivo (solo DeepSeek lo aplica), `reasoning_tokens`, `degradada`,
   `respaldo_verificador`, `respaldo_fallido` y `respaldo_no_disponible`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Etapa de riesgo alto sin tools | La etapa existe para llamar la acción que compromete |
| Dos llamadas (entender → kernel → redactar), D-SPEC-26-6 B | Duplica costo y prompt; el motor ya decide y la puerta de confirmación ya protege lo irreversible |
| Cambiar el modo a mitad del turno | La vuelta con thinking llevaría mensajes con tools sin `reasoning_content`: el 400 que se quiere evitar |
| Respaldo de otra familia con la clave de OpenAI de la plataforma | Factura a la plataforma el uso de un negocio BYOK (ADR-0101) y `gpt-4o-mini` cuesta más que `deepseek-flash` ($0.60 contra $0.28 por MTok de salida). Lo decide el dueño |

## Consecuencias

**Positivas:** más deliberación solo donde un error cuesta, con el modelo barato en el resto; la
política nunca hace una llamada que se sabe que va a fallar; queda medida en `usage_ledger`.

**Negativas / lo que aceptamos pagar:**
- Los tokens de razonamiento de un turno de riesgo alto (del orden de $0.001) pueden adelantar el
  cierre por `budgetUsdPerConversation`.
- Con la configuración de hoy (`deepseek-flash` y `gpt-4o-mini` de respaldo) el reintento del
  verificador **no** usa el respaldo en producción (`respaldo_no_disponible`). Para que lo use hay dos
  caminos, a decidir por el dueño: un respaldo de la misma familia (p. ej. `deepseek-v4-pro`, más
  caro) o habilitar la clave de OpenAI de la plataforma para chat.
- El protocolo `reasoning_content` no se valida en CI (develop no usa LLM real): antes de encender
  `aos_procedures` en un negocio con una etapa `risk: high` hace falta una llamada real de
  verificación, con OK del dueño.

**Qué haría que revisáramos esto:** que pass^k en las etapas de riesgo alto no mejore con thinking
(medido en el nocturno), o que la degradación aparezca en más del 5 % de esos turnos.

## Impacto en el código

- `core/kernel/politica_modelo.py` (nuevo): `decidir`, `misma_familia`, `PoliticaDelTurno`,
  `ProveedorDeLaPolitica` y `UsoDePolitica`.
- `core/orchestrator/loop.py`: `_politica_del_turno` y cuatro enganches (envolver el proveedor,
  `reasoning_content`, el reintento del verificador y el ledger).
- `core/kernel/procedures/ejecucion.py`: `ContextoMotor.riesgo_alto`.
- `core/llm/types.py`, `openai_compatible.py` (`reasoning_content`, `reasoning_tokens`,
  `reintentar_4xx`) y `cassette.py`.
