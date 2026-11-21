# ADR-0205 — El Coordinador atiende la conexión sin especialista y asegura siempre el lead base

- **Estado:** propuesto (2026-11-20). Se acepta con el OK del dueño al mergear este documento; el código va en HU-AOS-88 y HU-AOS-89, cuando [HU-AOS-70](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-44-habilidades-por-agente/hu/HU-AOS-70-leads-sin-comercio.md) esté en test.
- **Fecha:** 2026-11-20
- **Contexto:** [D-78](https://github.com/devrochy/solvershub-docs/blob/main/02-vision-y-decisiones/decisiones.md#d-78--corazón-v3-puro-coordinador-receptor-con-captura-de-lead-base-y-manifiesto-pack--habilidad--tools), [SPEC-50](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-50-corazon-v3-coordinador-y-matriz/spec.md) (REQ-AOS-29), [HU-AOS-87](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-50-corazon-v3-coordinador-y-matriz/hu/HU-AOS-87-adr-corazon-v3-coordinador-y-manifiesto.md). Va con [ADR-0206](0206-corazon-v3-puro-retiro-de-agent-specs.md) y [ADR-0207](0207-manifiesto-del-tipo-de-agente-y-tools-por-habilidad.md).
- **Enmienda:** [ADR-0112](0112-agent-os-de-procedimientos.md) §1 (el Coordinador ya no solo decide: atiende cuando no hay especialista), [ADR-0192](0192-sin-general-especialista-por-defecto.md) §1 (el primer especialista ya no nace solo con la conexión), §2 (la conexión tiene release sin especialista) y §3 (el ruteo puede quedar en el Coordinador), y [ADR-0193](0193-leads-en-el-core.md) §2 (el lead base ya no depende de que el agente en foco tenga Captar leads).
- **Usa:** invariantes 1 (multi-tenant con RLS), 3 (cero prompts en código), 4 (contratos solo por ADR: este no cambia ninguno), 7 (lo de fuera es dato) y 8 (sin borrado físico); ADR-0117 (releases, episodios y caché por versión), ADR-0122 (pausa), ADR-0142 (el Coordinador no cuenta en el plan), ADR-0198 (datos del cliente por agente).

## Contexto
En `origin/develop` 0f9ab7f3 una conexión solo tiene release si tiene un especialista `is_default` publicado:
`core/releases/build.py` lanza `ErrorDelBuild("sin_especialista")` y `elegir_foco` (`core/releases/cargar.py`)
descarta todo componente que no sea `role == "specialist"` («el Coordinador nunca atiende»). Para que eso no deje
mudas las conexiones, `on_connection_release_1` aplica el preset y crea el primer especialista solo
(`backfill_presets_v2`). El lead se asegura en `loop.py`, después del ruteo, únicamente si el agente en foco tiene
`crear_lead` en su oferta. El dueño decidió el 2026-11-20 (D-78) que la conexión nazca atendida por el Coordinador,
que el dueño cree su primer especialista con el recorrido de 5 estaciones (SPEC-49) y que ningún prospecto se pierda.

## Decisión
Toda conexión conversacional activa tiene release y atiende desde que nace: si no hay especialista que tome el
turno, lo atiende el Coordinador con un conjunto cerrado de habilidades del core; y en toda conversación real el
core asegura el `Customer` y la fila base del lead.

Detalle:
1. **Sin alta automática de especialista.** Crear o activar una conexión conversacional construye su release 1
   aunque el negocio no tenga especialistas. `on_connection_release_1` deja de aplicar los agentes del preset:
   el dueño crea su primer especialista con el recorrido de SPEC-49 (decisión del dueño del 2026-11-20). El preset
   sigue aportando settings del negocio, y su lista `agentes` pasa a ser la sugerencia de tipos que ofrece ese
   recorrido. `backfill_presets_v2` queda solo como comando explícito (journeys, evals y soporte). Las reglas que
   cada plantilla agrega al Coordinador (`agregar_reglas`) se aplican cuando se crea el especialista de ese tipo,
   por cualquier camino.
2. **Release solo con el Coordinador.** La invariante de ADR-0192 §2 pasa a ser: una conexión conversacional
   tiene release activa si tiene su Coordinador vivo; los especialistas son opcionales. El build ya no falla con
   `sin_especialista` cuando la conexión no tiene ningún especialista; sí sigue fallando con `sin_coordinador`, y
   con `sin_especialista_por_defecto` si tiene especialistas pero ninguno `is_default` (con especialistas, siempre
   hay uno por defecto). La release congela la versión del `KernelSpec`
   como hoy (ADR-0117), así que la caché por versión no cambia.
3. **Ruteo.** El orden de ADR-0192 §3 no cambia (tarea en foco → reglas → disparadores → especialista
   `is_default`). Si el resultado es vacío porque la release no tiene especialistas, el foco es el Coordinador
   con `source = "coordinador_receptor"`; `elegir_foco` deja de lanzar `ErrorDeRelease` en ese caso. Una release
   con especialistas nunca enruta al Coordinador por residuo: el especialista por defecto sigue recibiendo los
   saludos y lo ambiguo (ADR-0192 sigue rigiendo ahí). Si el único especialista está pausado, el turno sale en
   silencio como hoy (HU-ARCH-38); el Coordinador no lo reemplaza.
4. **Transición al primer especialista.** Cuando el dueño publica el primer especialista asignado a una conexión,
   ese especialista queda `is_default` y la conexión recibe una release nueva que lo incluye. Una promoción
   normal no corta el episodio en curso (ADR-0117, hasta `episode.idleMinutes`, 12 h por defecto), pero esta sí:
   una release **sin especialistas** que deja de ser la activa se trata como retirada, y la conversación pasa a
   la activa en su siguiente turno. Así el dueño ve a su especialista en cuanto lo publica.
5. **Habilidades fijas del Coordinador cuando atiende.** Cuando el foco es el Coordinador, el turno corre con
   una **vista de recepción**: un `AgenteCongelado` armado en memoria por el core (no se persiste ni se edita),
   cuyo `content` ya tiene forma `agentspec/3`. Así corre con el adaptador actual mientras exista y HU-AOS-90 la
   lee directo:
   - voz: la del `KernelSpec.voice` congelado; identidad del negocio: los `CAMPOS_DE_PROMPT` de `AppSettings`
     congelados en la release;
   - modelo: `AppSettings.llm_model` (con el respaldo de la plataforma si es nulo), como un ajuste en caliente
     igual que el proveedor. `KernelSpec` no tiene modelo y no se le agrega: sin cambio de contrato;
   - instrucciones: el playbook versionado `core/playbooks/recepcion-coordinador.md` (invariante 3);
   - memoria: `writeNamespaces = ["perfil"]`;
   - tools, set cerrado de 7 (decisión del dueño del 2026-11-20): `crear_lead`, `get_customer_profile`,
     `update_customer_profile` (solo `name`, `phone` y `email`, con `writeNamespaces = {"perfil"}`),
     `pedir_telefono`, `responder_aviso_datos`, `escalate_to_human` y `set_conversation_note`, con sus bindings
   concedidos en la vista (el fail-closed de HU-ARCH-34 sigue rigiendo). El límite de atributos de
   `update_customer_profile` lo hace cumplir el host con la lista de la vista antes de llamar a la tool, no una
   rama dentro de la tool compartida.
   - caché y costos: la clave de caché es la versión del `KernelSpec`, la versión del playbook del binario y el modelo en uso (los bloques compilados dependen del modelo), y
     `usage_ledger.agent_spec_version` registra `coordinador@recepcion:{versión del KernelSpec}`.
   Nunca recibe tools ni procedimientos de un pack de especialidad o transversal: no cotiza, no agenda, no cobra
   ni inscribe. El set es código del core, no configuración: la API responde 409 `coordinador_inmutable` a
   cualquier intento de instalarle un pack o de editarle habilidades. El Coordinador como foco se pausa con la
   conexión (ADR-0122) y con el interruptor global; no tiene pausa propia.
6. **El Coordinador sigue siendo el kernel.** Sus políticas (horario, pausa, cuota, ruteo, procedimientos y
   A2A) siguen siendo deterministas (ADR-0112 §2). Lo nuevo es acotado: cuando no hay especialista, el kernel
   le da el turno a la vista de recepción en vez de no responder. No conversa cuando hay un especialista.
7. **Lead base siempre.** En toda conversación real (fuera de la conversación de prueba del panel) el core
   asegura el `Customer` y la fila de `leads` (`status = 'nuevo'`, `origen` según el canal), idempotente por
   `(tenant_id, customer_id)` como define ADR-0193. Sigue en el mismo punto del turno que hoy (`loop.py`, después
   de fijar la release y el foco, que se deciden juntos bajo el lock de la conversación), pero sin la condición de
   que el foco tenga Captar leads. Esto reemplaza la condición de ADR-0193 §2 («al empezar una conversación con
   un agente que tiene Captar leads»). `asegurar` devuelve si creó la fila, y el aviso del punto 9 se decide con
   ese dato y el foco.
8. **Enriquecer el lead sigue siendo una habilidad.** `calificar_lead` y `actualizar_lead` los recibe solo el
   especialista que tiene Captar leads encendido (ADR-0193 §2 sigue rigiendo para eso; decisión del dueño del
   2026-11-20). Cada especialista crea y edita los datos del cliente de su módulo según su `data_policy`
   (ADR-0198) y los registros de su módulo; no vuelve a crear la identidad base.
9. **Aviso de lead nuevo.** El aviso al dueño (`avisar_lead_nuevo`) sale cuando se crea la fila y el foco es el
   Coordinador o un agente que tiene Captar leads. Un cliente que escribe a un agente sin Captar
   leads (por ejemplo Cobranza) obtiene su fila base sin aviso.
10. **Plan.** El Coordinador sigue sin contar en el cupo de agentes (ADR-0142); un negocio puede operar solo con
    el Coordinador.

## Alternativas consideradas
| Opción | Por qué no |
|---|---|
| Mantener el alta automática del especialista y que el Coordinador cubra solo los huecos (especialista archivado o sin publicar) | El dueño eligió que el primer especialista lo cree él con el recorrido de SPEC-49; el alta automática crea un agente que el dueño no configuró |
| Volver al agente `general` editable o crear una plantilla «Recepción» como especialista | Ya descartado en D-69 y ADR-0192: otro agente que mantener, que cuenta en el plan y mezcla recepción con módulos |
| Que el Coordinador conteste también el residuo cuando hay especialistas | Dos formas de atender en la misma conexión; el especialista por defecto ya cubre ese caso (ADR-0192 §3) |
| Asegurar el lead solo cuando el agente tiene Captar leads (ADR-0193 §2 tal cual) | Las conexiones atendidas por el Coordinador y los agentes sin la habilidad perderían el prospecto |
| Forzar Captar leads (calificar y actualizar) en todo especialista | El dueño prefirió que enriquecer siga siendo activable; la fila base ya garantiza que no se pierda el prospecto |

## Consecuencias
**Positivas:**
- Ninguna conexión activa queda muda ni pierde un prospecto, aunque el dueño no haya creado su especialista.
- El dueño crea su primer especialista a conciencia, con el recorrido de SPEC-49, en vez de heredar uno del preset.
- Todo cliente que escribe queda en Clientes con su origen, sea cual sea el agente que lo atendió.

**Negativas / lo que aceptamos pagar:**
- Un negocio en el plan free puede tener un bot que conversa con 0 agentes en el cupo; el costo LLM de la
  recepción lo frenan los créditos del plan y `budgetUsdPerConversation`. Es una decisión comercial que se acepta.
- `conversations.active_agent_instance_id` apunta a la instancia `core` en esos turnos: En curso, métricas y el
  depurador deben mostrarlo como «Coordinador», no suponer un especialista.
- El Coordinador gana un camino conversacional (con LLM) que hay que probar y medir aparte (golden de HU-AOS-96).
- Cambian el alta y la bienvenida: los recorridos, journeys y evals que suponían un especialista nacido con la
  conexión tienen que crearlo de forma explícita.
- Se crean filas de lead también para clientes de Cobranza o Agenda sin Captar leads; el embudo las muestra
  como `nuevo`.
- `build.py`, `cargar.py` y `core/shared/release_vigente.py` los cambian también ADR-0206; el orden lo fija la sección siguiente.

**Qué haría que revisáramos esto:** que en el primer mes de producción más del 20 % de las conversaciones
atendidas por el Coordinador terminen en escalamiento sin que el dueño cree un especialista en esa conexión, o que
el golden del Coordinador receptor no llegue a `pass^4 = 100 %`.

## Orden de los PR
HU-AOS-88 (release y lead base) y HU-AOS-89 (habilidades fijas) van primero, sobre el contrato v2 actual; HU-AOS-90
(ADR-0206) se apoya en ese resultado. Es el cambio más chico que entrega valor y no espera el refactor del loop.

## Impacto en el código
- `core/releases/build.py` (`sin_especialista`), `core/releases/cargar.py` (`elegir_foco`),
  `core/shared/release_vigente.py` (vista de recepción), `core/kernel/release_del_turno.py` (foco
  `coordinador_receptor` y release sin especialistas tratada como retirada).
- Mientras exista el adaptador v2: `core/agents/spec_de_release.py`, `core/agents/compiler.py` y las concesiones
  del loop (`_solo_las_concedidas`) aceptan la vista de recepción.
- `core/events/core_handlers.py` (`on_connection_release_1` sin `backfill_presets_v2`), `core/releases/release_uno.py`
  y `core/presets/agentes.py`.
- `core/leads/asegurar.py` (devuelve si creó; el aviso se separa) y su llamada en `core/orchestrator/loop.py`;
  `core/leads/oferta.py`.
- `core/presets/agentes.py` (`agregar_reglas` al crear el especialista) y los lectores de
  `active_agent_instance_id` (En curso, métricas, depurador).
- Nuevo `core/playbooks/recepcion-coordinador.md`; oferta de tools de la vista de recepción.
- Guardas 409 `coordinador_inmutable` en `core/agents/instalaciones_agente.py` y `core/api/routers/agent_editor.py`.
- Panel y APK: estado «Atendiendo con el Coordinador» de la conexión (HU-AOS-95).
- Journeys y golden: siembra explícita del especialista donde hoy se supone.
