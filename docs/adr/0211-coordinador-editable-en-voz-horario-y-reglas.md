# ADR-0211 — El dueño edita las opciones propias del Coordinador; sus tools y habilidades siguen cerradas

- **Estado:** propuesto (2026-11-23, a petición del dueño; falta su decisión sobre las preguntas abiertas de abajo). Sin código todavía; el código va en HU-AOS-98.
- **Fecha:** 2026-11-23
- **Contexto:** [D-78](https://github.com/devrochy/solvershub-docs/blob/main/02-vision-y-decisiones/decisiones.md#d-78--corazón-v3-puro-coordinador-receptor-con-captura-de-lead-base-y-manifiesto-pack--habilidad--tools), [SPEC-50](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-50-corazon-v3-coordinador-y-matriz/spec.md), HU-AOS-98.
- **Enmienda:** [ADR-0205](0205-coordinador-receptor-y-lead-base.md) §5 (la API responde 409 `coordinador_inmutable` a cualquier edición del Coordinador: pasa a valer solo para sus tools y habilidades).
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
   Los cambios entran con la siguiente release de la conexión, como hoy los de Configuración (ADR-0117).
2. **Reglas de respuesta (guardrails) del Coordinador:** ❓ pendiente de decisión del dueño. Si se aceptan, valen solo
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
- Dos formas de editar la voz (la pantalla del Coordinador y Configuración) hasta que Configuración deje de mostrarla: se decide en la HU.
- Si se aceptan las reglas, la release lleva un campo nuevo y hay que migrar las release vigentes (un campo opcional con valor por defecto vacío).
- Con especialistas, la mayoría de estas opciones no se notan en la conversación; la pantalla debe decirlo.

**Qué haría que revisáramos esto:** que los dueños pidan tools propias del Coordinador, o que las reglas del Coordinador se usen para compensar especialistas mal configurados.

## Preguntas abiertas para el dueño
1. ¿Entran los guardrails del Coordinador en la primera versión o van en una segunda?
2. ¿Configuración deja de mostrar voz e identidad y se mueven a la pantalla del Coordinador, o conviven?
3. ¿Quién puede editar: dueño y master, como los demás agentes?

## Impacto en el código
- `core/api/routers/agent_editor.py` y `core/agents/instalaciones_agente.py`: el 409 se acota a tools, secretos, habilidades y packs.
- Rutas nuevas del Coordinador sobre `AppSettings` (voz, identidad, horario, nivel de IA, escalamiento) y, si se aceptan, `guardrails`.
- `core/shared/release_vigente.py`: la vista de recepción lee las reglas.
- Panel: pantalla «Coordinador» en el editor de agentes (HU-AOS-98).
