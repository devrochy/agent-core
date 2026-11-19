# ADR-0119 — Fiabilidad medida: verificador de afirmaciones y evals de consistencia (pass^k)

- **Estado:** aceptado (2026-10-08, con HU-ARCH-28)
- **Fecha:** 2026-10-08
- **Contexto:** HU-ARCH-28 y HU-ARCH-29 (SPEC-02, Hito 0A, decisión D-28), HU-AOS-06, HU-AOS-21 y HU-AOS-31 (SPEC-26).
  Enmienda ADR-0051 §4 (evals persistidos) y ADR-0025 (las frases de cierre del negocio las lee el verificador).
  Usa ADR-0092 sin cambiarlo. **Es el primero de la serie que se acepta**, porque lo usan HU del Hito 0A.

## Contexto

- La línea base de evals (34/35 en el framework, 7/8 en `solvershub`) mide **un** intento por escenario.
- El gate de CI falla a veces y se relanza a ciegas.
- El runner mira solo el primer paso del modelo.
- `crm_amount_check`, `crm_completion_check` y `calendar_completion_check` escalan compras válidas. Qué pasa al fallar lo configura cada negocio (`guardrail_fail_action`, ADR-0092; `escalate` por defecto).
- τ-bench (Sierra, 2024) muestra que los agentes de atención al cliente pierden mucha consistencia cuando el mismo caso se repite k veces.

## Decisión

La consistencia se mide con pass^k sobre conversaciones simuladas, y lo que dice el asistente se verifica contra la evidencia de forma determinista.

1. **Evals de consistencia:**
   - Escenarios con usuario simulado (objetivo, datos, estilo y semilla), datos sembrados, **aserciones sobre el estado final de la base** y tope de turnos, corridos contra el loop real sin efectos.
   - k intentos (4 en el nocturno, 8 antes de una release); se guardan pass@1 y **pass^k** (columnas aditivas en `eval_runs` y `eval_results`).
   - Los escenarios inestables se marcan con sus intentos fallidos.
   - En el Hito 0A el umbral vigente (≥ 95 % durante tres noches) no cambia y pass^4 se reporta.
   - Desde la Fase 1, el gate de release exige pass^4 ≥ 0,85 por procedimiento tocado y no-regresión (umbral inicial, se ajusta con la primera medición).
   - En el 0A corren contra una base efímera (CI y stack local), así que su consumo no llega al ledger de ningún negocio real. La marca `kind = eval` en `usage_ledger` llega con HU-AOS-02.
2. **Verificador de afirmaciones**, determinista:
   - Extrae montos, links, fechas y horas, referencias, productos y cantidades, y frases de cierre: las que declara la habilidad y las `completion_claims_custom` del negocio (ADR-0025, siempre literales).
   - Los compara con la evidencia: acciones del turno y de turnos anteriores (`audit_log`) y, desde la Fase 1, el estado de las tareas. También exige las reglas `required_tool` (ADR-0051) y respeta `disabled_guardrails`.
   - Ante un fallo: reintento correctivo (`guardrail_max_retries`) y luego la acción que configuró el negocio (`guardrail_fail_action`, ADR-0092: neutral o escalar). La mejora frente a hoy viene de menos falsos positivos, no de cambiar esa configuración.
   - Reemplaza los tres guardrails heurísticos solo cuando los falsos positivos son ≤ 2 % en un set etiquetado.
3. **LLM-juez en producción** (Fase 1): solo calibrado contra conversaciones etiquetadas por personas, con un acuerdo mínimo. Nunca decide en línea, solo audita.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Seguir con pass@1 | Oculta la inconsistencia: un error de 1 de cada 4 veces no se ve |
| LLM-juez en línea por turno | Costo, latencia y no determinismo en el camino del cliente |
| Mantener los guardrails heurísticos | Escalan compras válidas y pausan el bot |
| Pasar a respuesta neutral por defecto | Es una decisión de cada negocio (ADR-0092); el problema medido son los falsos positivos |

## Consecuencias

**Positivas:** la consistencia se mide y se exige; menos escalamientos falsos; cada fallo deja evidencia para corregirlo.

**Negativas / lo que aceptamos pagar:** más cómputo en el nocturno (en base efímera, no se factura a ningún negocio); parsers por tipo de afirmación; mantener el set etiquetado.

**Qué haría que revisáramos esto:** que pass^k alto no se correlacione con la calidad en producción (el simulador no es realista) o que el verificador siga con falsos positivos por encima del 2 %.

## Impacto en el código

- `evals/runner.py` y `evals/journeys.py`: simulador, k y aserciones.
- Migración aditiva de `eval_runs` y `eval_results`.
- Verificador en el core, integrado en `core/orchestrator/loop.py`. Lee `behavior_config` (ADR-0092) y la configuración de cada pack (`completion_claims_custom`).
- Retiro de los tres guardrails tras medir.

## Adenda de implementación (2026-10-09, HU-ARCH-29)

Lo que fijó la implementación del verificador, revisada por el agente `arquitecto` antes de escribir código:

- **Dónde vive:** `core/orchestrator/verificador/` (`evidencia.py` y `verificador.py`). Corre en `end_turn` después de
  las validaciones de persona y antes de los guardrails de packs, que ven el texto ya corregido. Comparte el contador de
  reintentos y `guardrail_fail_action` (ADR-0092). Es fail-open para montos y cierres: si lanza una excepción, el turno pasa y el
  veredicto queda como `error`, pero los links se quitan todos.
  *Adenda de ADR-0050 (2026-10-10):* de las validaciones de persona, solo `forbiddenPhrases` corre antes del
  verificador. `persona.maxLength` corre al final, cuando el verificador y los guardrails de packs ya pasaron, y no
  comparte el contador de reintentos ni `guardrail_fail_action`.
- **Punto de extensión para packs:** `MCPHost.register_afirmaciones(fn)` con `Afirmacion(label, patron, tools,
  basta_url)` en `core.mcp.types`. Es API de plugins, no `packages/contracts`. Se filtra por packs habilitados
  (ADR-0019) y se limpia en `remove_plugin`. La polaridad (preguntas, negaciones y condicionales) es español genérico
  y vive en el core, no en cada pack.
- **Links:** las URLs se comparan normalizadas (sin esquema ni `www.`, host en minúsculas) y se detectan también las
  que empiezan por `www.`. Una URL sin respaldo **se quita**, no se reemplaza, y no consume reintento: el link exacto lo entrega
  `_safe_append`. Son evidencia las URLs de tools del turno actual y las del contexto (prompt del sistema y mensajes
  del cliente). No lo son las de turnos anteriores, porque un link de pago pudo quedar vencido (ADR-0087), ni las de
  mensajes anteriores del asistente, porque lavarían un link inventado.
- **Montos:** `core.shared.dinero` (antes en pack-commerce). `MONEY_KEYS` es una convención del core para los packs.
  Las cifras abreviadas ("$1,8 millones") no se verifican.
- **Fechas y referencias:** se extraen y se cuentan en el veredicto, pero no bloquean (modo sombra) hasta tener
  incidentes que lo justifiquen.
- **`required_tool` (ADR-0051):** sin cambios. Sigue evaluándose en `run_guardrails` con su semántica actual.
- **Instrucciones de reintento:** secciones `verificador_<motivo>` del playbook `respuestas` (invariante 3). En código
  solo queda un texto genérico de respaldo.
- **Veredicto por turno:** una fila en `audit_log` (`actor = core:verificador`, `action = verificacion`) con códigos
  y conteos, sin texto. Se escribe desde una sola función (`registrar_veredicto`) para que HU-AOS-10 la mueva a
  `turn_outcomes`. Los turnos que escala una tool no llegan a `end_turn` y no dejan veredicto.
- **Interruptores:** `verificador_montos`, `verificador_cierre` y `verificador_links`. Los nombres viejos
  (`crm_amount_check`, `crm_completion_check`) se traducen solos (`normalizar_desactivados`), así un negocio que los
  había apagado los mantiene apagados.
- **Retiro de los guardrails:** `crm_amount_check` y `crm_completion_check` se retiran porque el set etiquetado
  (`evals/verificador/`, `python evals/verificador.py`) da falsos positivos ≤ 2 % y ningún falso negativo. Lo vigila
  un test de la suite unitaria. Medición del 2026-10-09: 205 correctas con 0 falsos positivos y 14 falsas con 0 falsos
  negativos; en los mismos borradores, los guardrails viejos tenían 16,7 % de falsos positivos. `calendar_completion_check` sigue registrado: no tuvo falsos positivos medidos y su
  respaldo depende del `status` de cada acción. Se mide y se retira en una HU siguiente.
- **Veredicto en `turn_outcomes` (HU-AOS-10, 2026-10-10):** `registrar_veredicto` lo escribe también en
  `turn_outcomes.verifier` (`[{kind, verdict, count}]`) y `verifier_retries`. La fila de `audit_log` sigue de forma
  transitoria, aunque esta adenda decía "mueve": la leen tests y journeys, y se retira en una HU siguiente.

## Adenda (2026-10-09): URLs de una tool que terminó con error (HU-SH-07)

Aprobada por el dueño el 2026-10-09. Las URLs que devuelve una tool **del turno actual** cuentan
como evidencia de links aunque la tool haya terminado con error (`_ok=False`): las dio el servidor,
no las inventó el modelo. Caso: `crear_pedido` responde `terminos_pendientes` con `error` y los
enlaces de términos, privacidad y DPA (ADR-0125). El verificador los quitaba todos y el cliente
aceptaba sin verlos.

De una tool fallida no cuentan las URLs de su texto libre (`error`, `hint`, `message`): podrían
repetir un argumento con una URL que inventó el modelo. Una tool fallida sigue sin respaldar nada más: no entra en `acciones_ok` (cierres) ni en
`urls_enlace` («te envío el link de pago»). Una URL que no devolvió ninguna tool se sigue quitando,
y las de turnos anteriores tampoco cuentan.


**Set etiquetado (2026-10-10).** Un link sin respaldo no rechaza el borrador, se quita, así que el
set no veía el caso de arriba. Desde ahora `evals/verificador.py` cuenta como falso positivo una
respuesta correcta a la que se le quitó un link. Una falsa por un link inventado
(`urls_sin_respaldo`) cuenta como detectada si se quitó. El incidente de test del 2026-10-10
(`incidente-2026-10-10-terminos-pendientes`) entró al set con `origen: incidente`. Un test exige
que ningún caso de incidente falle, aunque el total quede bajo el umbral del 2 %. Medición: 206
correctas con 0 falsos positivos y 17 falsas con 0 falsos negativos. Sin esta adenda, el caso del
incidente es un falso positivo (0,5 %).

## Adenda (2026-10-10): grabar y reproducir el LLM de las evals

El dueño la aprobó el 2026-10-09 para no gastar saldo del proveedor en pruebas. La revisó el agente `arquitecto` antes
de escribir código.

- **Qué es:** `core/llm/cassette.py` envuelve al proveedor que resuelve `resolve_provider`, tanto el del asistente
  como el del cliente simulado. Hay tres modos:
  - `off`: el de siempre.
  - `record`: llama al proveedor real y guarda el request normalizado y la respuesta, en un JSON por escenario o
    journey.
  - `replay`: responde desde ese JSON **sin construir el proveedor real**. Si no hay coincidencia, falla con
    `GrabacionDesactualizada` y dice qué cambió. Nunca cae a la red.

  El cableado está en `evals/grabaciones.py` (`--grabacion`, `LLM_CASSETTE`).
- **Guard de producción:** el modo es un estado del proceso que solo encienden los runners de evals
  (`cassette.activar`). El servidor no lee la variable. `activar` falla fuera de `APP_ENV` development o test. El
  error no hereda de `LLMProviderError`, para que el loop no lo convierta en fallback o escalamiento; los runners
  cortan la corrida con el código de salida 3.
- **Normalización:** la clave quita lo volátil:
  - el bloque de fecha y hora de ADR-0091;
  - UUIDs, hex de 32 y tokens de 16 hex, slugs de la corrida, timestamps y fechas ISO, e ids de tool_calls.

  Los valores se cambian por marcadores numerados, y la respuesta grabada se rehidrata con los valores de la corrida
  actual. La semilla del intento es parte de la clave. Los requests idénticos (k del golden) se guardan como
  secuencia. El detalle está en `evals/README.md`.
- **Sin secretos:** antes de escribir se rechaza la grabación si aparece la API key usada (`.env` o BYOK), un valor
  secreto de la configuración o algo con forma de credencial. Al cargarla se valida como dato no confiable: una
  respuesta grabada no puede llamar una tool que no se ofreció.
- **Replay no es medición:** repite lo grabado, así que no mide la consistencia del modelo. Una corrida en replay no
  se guarda en `eval_runs`. El umbral de esta ADR y pass^k se miden siempre en vivo: desde el 2026-10-10, en la PR de release hacia `main` y en las corridas a demanda de `evals-nightly.yml` (adenda de ADR-0120: develop no gasta LLM).
- **Dónde viven las grabaciones:**
  - las del framework, en `evals/cassettes/`;
  - las de un negocio, junto a su golden en el repo del negocio (`TENANTS_ROOT/<slug>/evals/cassettes/`), nunca
    en el framework.

  En la v1 el replay apunta a los sets del framework. Un cambio de playbook del core invalida las grabaciones de
  todos los negocios.
- **Regrabar:** `record` gasta saldo, así que solo se hace con OK del dueño. Se regraba por escenario
  (`python evals/runner.py 06 --grabacion record`). En record y replay los embeddings de memoria se apagan, porque
  saldrían a la red y cambiarían el prompt.
- **CI:** el gate de las PRs a `develop` corre en replay sin `LLM_API_KEY` (job `evals-replay`). Lo fija la adenda del
  2026-10-10 de ADR-0120.

## Adenda (2026-10-11, HU-AOS-06): el verificador usa el estado de las tareas

**Estado:** propuesta; requiere la aprobación del dueño antes del merge (cambia "referencias en
sombra" de la adenda de HU-ARCH-29, aunque solo con `tenants.aos_procedures`). Revisada por el
agente `arquitecto` antes de escribir código.

- **Solo con el motor activo** (`aos_procedures`). Con la bandera apagada la verificación es
  idéntica, conteos de sombra incluidos: no hay estado y nada de lo de abajo aplica.
- **Evidencia del estado** (`EstadoTareas`, `core/orchestrator/verificador/evidencia.py`): las
  tareas del episodio en curso (abiertas y las que cambiaron desde `episode_started_at`, tope 20),
  más las referencias vigentes del bloque dinámico (ADR-0130), que son dato del servidor.
  - Solo cuentan los datos que fijó una acción (`source = action:*`). Lo que escribió el modelo,
    un botón o el contenido del cliente no es evidencia. Un número que citó el cliente tampoco:
    hay que consultarlo con una acción.
  - **Montos y referencias:** de todas esas tareas.
  - **Cierres:** una afirmación queda respaldada si alguna de sus `tools` fijó, en la tarea en foco
    o en una que se completó después de la última respuesta del asistente, un dato que una
    afirmación de estado o de link del procedimiento declara (`claims[].requires` o `mustMatch` con
    `slot:`). Un dato de una lectura (`estado_pago = pendiente`) no respalda cierres en los turnos
    siguientes, y una tarea que el motor ya no gobierna (pack deshabilitado) tampoco (el cierre por
    evento entre turnos: "tarea cerrada" del design §7.3). Así el pedido de la compra anterior del
    episodio no respalda "tu pedido quedó creado" de la siguiente (el patrón del incidente de
    ADR-0130). `Afirmacion` no cambia.
- **Referencias que bloquean:** solo las que el borrador afirma (no preguntas, negaciones ni
  condicionales) y de una **familia** (`OC` de `OC-0042`) que el servidor fijó en alguna tarea.
  Si no están en la evidencia, falla con `referencia_sin_evidencia`: reintento correctivo y luego
  `guardrail_fail_action` (ADR-0092), como los demás motivos. Un SKU u otra familia, o una
  conversación sin ese dato en sus tareas, siguen en sombra.
- **Instrucción de reintento:** sección `verificador_referencia_sin_evidencia` del playbook del
  kernel `verificador-estado`. No va en `respuestas` del core porque ese playbook entra al prompt
  de las evals grabadas; un negocio sí puede reemplazarla con esa sección en su `respuestas`.
- **Sin interruptor por ahora:** el interruptor es `aos_procedures`, que solo maneja el master. No
  se reusa `verificador_cierre` (mezclaría semánticas). `verificador_referencias` se suma a
  `TOGGLES` en la HU que encienda procedimientos en un negocio real, antes de encenderlos.
- **Los comandos del hilo no son evidencia:** su resultado trae la tarjeta con datos que escribió
  el modelo. El loop no los suma a los resultados del turno y su auditoría (`core:comando`) no es
  `tool:*`, así que tampoco vuelven desde `audit_log`.
- **Medición (2026-10-11):** el set etiquetado suma 11 casos con estado explícito
  (`origen: sintetico-estado`, `estado` en el JSONL) y `python evals/verificador.py --con-estado`
  mide las respuestas reales con un estado derivado de sus resultados. Sin estado: 225 correctas,
  1 falso positivo (0,4 %, el mismo de antes) y 0 falsos negativos en 25 falsas. Con estado: los
  mismos números; ningún falso positivo nuevo. Los conteos de `referencias_sin_evidencia` en
  `turn_outcomes.verifier` de producción se miran antes de encender un negocio.

## Adenda (2026-10-11, HU-AOS-09): respaldo en el reintento

Con el motor de procedimientos activo, el reintento tras un rechazo del verificador usa el respaldo
del agente (`model.fallback`) cuando es de la misma familia que el modelo del turno. Reglas y límites
en ADR-0132. Los reintentos y la acción ante el fallo siguen siendo los de ADR-0092.

## Adenda (2026-10-20): un traspaso a una persona exige `escalate_to_human` del turno

**Estado:** propuesta; requiere la aprobación del dueño antes del merge.

Origen: smoke del dueño en test (chat web, negocio `solvershub`, conversación `e3c24103…`). La
escalación de las 05:07 quedó `resuelta` a las 05:09 y el bot volvió; a las 05:11 el modelo dijo «Ya
le pasé tu caso a una persona del equipo… En cuanto se conecte, te escribe por acá» sin llamar
`escalate_to_human`: nadie lo iba a atender.

- **Dónde:** se amplía la afirmación que ya existía, `asesor_humano` de pack-commerce (respaldada por
  `escalate_to_human`), en vez de crear otra en el core: un solo detector del mismo hecho. Suma «le pasé
  tu caso / solicitud», «te comunico / paso / transfiero con una persona o el equipo», «escalé tu caso»,
  «quedó escalado» y «una persona del equipo te va a contactar / escribir».
- **Respaldo:** solo la tool del turno. Se descartó aceptar «una escalación vigente»: `escalate()` pausa
  el bot, así que con una escalación vigente el bot no responde y ese respaldo no cambia ningún veredicto
  real (revisión del `arquitecto`).
- **Polaridad:** «si quieres, te comunico con…» no afirma: el condicional suma `quier` (antes solo
  `quer`, que no cubre «quieres»).
- **Medición** (`evals/verificador.py`, con y sin `--con-estado`): 7 casos nuevos del smoke en el set
  etiquetado; 230 correctas y 27 falsas, falsos positivos 1 (0,4 %, el mismo de antes) y ningún falso
  negativo. Gate en replay sin regrabar.
- **Instrucción del reintento:** la de siempre (`verificador_status_sin_evidencia`): llamar la tool que
  hace la acción o decir con claridad que todavía no está hecha.
- **Sin cambio:** la respuesta neutral de ADR-0092 («Dejame verificar eso y te confirmo») sigue igual;
  en el smoke salió por un falso positivo de `cob_suscripcion_check`, corregido en la adenda 2026-10-20
  de ADR-0113.

## Adenda propuesta (2026-10-25, HU-AOS-21): k = 4 en el gate de release

Decisión del dueño del 2026-10-25: el gate de promoción de releases (ADR-0117, adenda HU-AOS-21) usa **k = 4**, no k = 8, por costo. El umbral sigue en pass^4 ≥ 0,85 por procedimiento tocado y sin regresión frente a la release activa. Se revisa con la primera medición del gate.
