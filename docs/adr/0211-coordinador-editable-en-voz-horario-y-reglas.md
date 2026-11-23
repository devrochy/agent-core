# ADR-0211 — El dueño edita las opciones propias del Coordinador; sus tools y habilidades siguen cerradas

- **Estado:** aceptado (2026-11-23, por decisión del dueño; revisado por el subagente `arquitecto`). El código va en HU-AOS-98.
- **Usa:** invariantes 3 (cero prompts en código), 4 (contratos solo por ADR) y 7; ADR-0112 §2 (políticas deterministas del kernel), ADR-0142 (el Coordinador no cuenta en el plan), ADR-0206 (corazón V3 puro) y ADR-0210 (nivel de IA).

## Contexto
ADR-0205 §5 fijó que el Coordinador no es un agente editable: cuando atiende lo hace con una «vista de recepción»
armada en memoria por el core. Su voz es `KernelSpec.voice`, su identidad son los `CAMPOS_DE_PROMPT` de `AppSettings`,
su modelo es `AppSettings.llm_model`, sus instrucciones son el playbook `recepcion-coordinador.md` y sus tools son un set
cerrado de 7. Por eso el panel no abre su editor y HU-AOS-97 le responde 409.

El dueño quiere entrar a las opciones del Coordinador como en los demás agentes. Lo que hoy se puede cambiar de él está
repartido en Configuración (voz e identidad del negocio) y no tiene un lugar propio. Además, como el Coordinador solo
conversa cuando el negocio no tiene especialista, lo que se le configure solo se nota en ese caso; con especialistas
solo enruta.

## Decisión
El Coordinador gana una pantalla de opciones propias en el panel, con las mismas secciones y el mismo aspecto que el
editor de agentes, pero **no es un `AgentSpecV3` editable**: cada sección escribe en el dato que ya la gobierna.

1. **Se pueden editar:**
   - voz y tono, y los datos de identidad del negocio que ya usa la vista de recepción (los `CAMPOS_DE_PROMPT` de `AppSettings`);
   - horario y mensaje fuera de horario;
   - nivel de IA de la recepción (ADR-0210), sin mostrar el modelo;
   - mensaje de escalamiento a una persona.
   Configuración deja de mostrar voz e identidad: se mueven a la pantalla del Coordinador (decisión del dueño del 2026-11-23). Pueden editar el dueño y el master, como en los demás agentes.
   Los cambios entran con la siguiente release de la conexión, como hoy los de Configuración (ADR-0117).
2. **Reglas de respuesta (guardrails) del Coordinador:** entran en la primera versión (decisión del dueño del 2026-11-23). Valen solo
   en la vista de recepción (no en los especialistas), con el mismo contrato `GuardrailRule`, y se guardan en
   `AppSettings` (no en un agente). Exigen cambiar el contrato de la release para que las lleve: invariante 4, se
   aprueba aquí.
3. **Siguen cerradas:** tools declarativas, secretos, habilidades, packs y procedimientos del Coordinador. La API
   mantiene 409 `coordinador_inmutable` en `guardrails` y `declarative-tools` del agente Coordinador y en la
   instalación de packs.
4. **Instrucciones:** el playbook `recepcion-coordinador.md` sigue siendo código versionado (invariante 3); el dueño
   no lo edita. Lo que personaliza va por la voz y la identidad.
5. **Sin cambio de plan ni de cupo** (ADR-0142): editar al Coordinador no cuenta como agente.

## Alternativas consideradas
| Opción | Por qué no |
|---|---|
| Hacer del Coordinador un `AgentSpecV3` editable completo | Reabre lo que ADR-0205 §5 cerró: tools con secretos en el punto de entrada de todas las conversaciones y un agente más con borrador y versiones |
| Dejar todo como está (inmutable) | El dueño no tiene dónde ajustar la recepción; hoy tiene que saber que está en Configuración |
| Permitir tools declarativas en el Coordinador | Ensancha la superficie de seguridad del único agente que ve todos los mensajes |

## Consecuencias
**Positivas:** el dueño configura la recepción en un solo lugar, con el mismo recorrido que sus agentes.
**Negativas / lo que aceptamos pagar:**
- Configuración pierde la edición de voz e identidad: hay que quitarla y avisar en el panel a quien la buscaba ahí.
- La release lleva un campo nuevo y hay que migrar las release vigentes (un campo opcional con valor por defecto vacío).
- Con especialistas, la mayoría de estas opciones no se notan en la conversación; la pantalla debe decirlo.

**Qué haría que revisáramos esto:** que los dueños pidan tools propias del Coordinador, o que las reglas del Coordinador se usen para compensar especialistas mal configurados.

## Decisiones del dueño (2026-11-23)
1. Los guardrails del Coordinador entran en la primera versión.
2. Voz e identidad se mueven de Configuración a la pantalla del Coordinador.
3. Editan el dueño y el master, como en los demás agentes.

## Impacto en el código
- `core/api/routers/agent_editor.py` y `core/agents/instalaciones_agente.py`: el 409 se acota a tools, secretos, habilidades y packs.
- Rutas nuevas del Coordinador sobre `AppSettings` (voz, identidad, horario, nivel de IA, escalamiento) y, `guardrails`.
- `core/shared/release_vigente.py`: la vista de recepción lee las reglas.
- Panel: pantalla «Coordinador» en el editor de agentes (HU-AOS-98).

## Adenda de implementación (2026-11-23, HU-AOS-98)
Decisiones del diseño revisado por el subagente `arquitecto`.

1. **Dónde viaja.** La fuente editable son `behavior_config["recepcion_guardrails"]` y `["recepcion_nivel_ia"]` de
   `AppSettings` (sin migración). La release las congela en una clave de primer nivel del manifiesto, `recepcion`
   (`{"guardrails": [...], "nivel_ia": "avanzado"}`), que se **omite** cuando está vacía y el nivel es Estándar: el
   hash de las releases existentes no cambia. No va en `app_settings`: ese tipo es `dict[str, str | None]` y un cambio
   ahí vuelve a medir los procedimientos de todos los agentes (`diferencias.py`), cuando la recepción no tiene
   procedimientos. `diff_legible` la muestra como el ajuste `recepcion`.
2. **El turno lee la release**, nunca la fila en caliente: `vista_de_recepcion(foco, recepcion=release.recepcion)`.
   Las reglas no se compilan en bloques del prompt (solo las aplica el loop), así que la clave de caché por release
   ya es correcta.
3. **Reglas válidas o el build falla.** Se validan al escribir (422 por campo) y otra vez al construir
   (`reglas_recepcion_invalidas`): el turno nunca lanza por un dato malo. `required_tool` acepta solo las 7 tools
   de la recepción.
4. **Nivel de IA.** No usa `agents.nivel_ia` (el Coordinador no se publica). `recepcion.nivel_ia` fija
   `model.primary` y `model.router` con el mapa de ADR-0210; Estándar deja el modelo de la plataforma. Avanzado pasa
   por el mismo gate de plan (409 `plan_nivel_ia_not_included`). El modelo nunca se muestra.
5. **Ruta.** `GET` y `PUT /api/v1/admin/coordinator`, con escritura **parcial** (solo lo enviado) bajo `FOR UPDATE` y
   una fila en `audit_log` por cambio. Permiso: `settings` (lectura y escritura), como la configuración del negocio.
   No reutiliza `PUT /admin/settings/identity`, que sobrescribe todos sus campos. Las rutas del agente Coordinador
   (`guardrails`, `declarative-tools`, habilidades, packs, flujo y horario por agente) siguen en 409.
6. **Horario.** Es el del negocio entero y rige en caliente: no se mueve; la pantalla del Coordinador lo edita como
   la misma fuente. Voz e identidad entran con la siguiente release; el horario no.
7. **Fuera de v1:** el mensaje de escalamiento propio. El texto sale de `respuestas.md` en siete sitios del loop y de
   la entrada; hacerlo editable pide un helper único y es un cambio del loop, no de la recepción. Va en una HU aparte.
