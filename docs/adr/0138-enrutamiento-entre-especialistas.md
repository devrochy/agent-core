# ADR-0138 — Enrutamiento entre especialistas: tarea en foco, reglas, disparadores léxicos y modo sombra

- **Estado:** propuesto (HU-AOS-17, 2026-10-18; decisiones revisables por el dueño marcadas abajo)
- **Fecha:** 2026-10-18
- **Contexto:** SPEC-26 (D-AOS-8, design §5.2 y §7.1, R3), HU-AOS-17 (#378). **Usa:** ADR-0112 (banderas y
  ledger del Coordinador), ADR-0113 (KernelSpec `routing`, `procedure/1` `triggers`), ADR-0117 (adendas HU-AOS-15
  y HU-AOS-16: `elegir_foco` y dueño de la conversación), ADR-0118 (procedimientos por agente).

## Contexto

Hasta HU-AOS-16 la release de una conexión podía traer varios especialistas, pero el turno siempre lo atendía
el de `elegir_foco` (dueño vigente → `is_default` → `routing.defaultAgent` → único) y `core_decisions.route_source`
era siempre `default`. El KernelSpec ya traía `routing` (reglas sin regex, umbrales de disparadores, coprocesador,
estabilidad del dueño, `shadowMode`) y cada `procedure/1` sus `triggers.examples`, pero nadie los leía. No hay
embeddings en producción (P-19, R-AOS-11) y la tanda no permite gastar LLM.

## Decisión

1. **Router del kernel** (`core/kernel/router.py`), sin LLM, llamado por `release_del_turno.asignar` bajo el lock
   de la conversación. Orden:
   1. **Tarea en foco** (activa, posición 0): atiende su `owner_instance_id` (o quien declara su procedimiento),
      sin puntuar el texto (`task`). Solo una regla con `when.flowStage` sobre esa etapa la adelanta.
      *Decisión revisable:* se desvía del pseudo `prepare` de design §7.1 (que evalúa reglas aunque haya tarea);
      así una regla como «cliente en mora → cobranza» no le quita una venta en curso a Ventas. La digresión
      con dos tareas es de HU-AOS-20.
   2. **Reglas** (`routing.rules`), la primera que cumple todas sus condiciones (`rule`, `rule_id`, confianza 1):
      `interactivePayloadPrefix`, `keywordAny` (frase normalizada completa), `customerFact` (hecho vigente de
      `memory_facts` escrito por el sistema: se excluye `source = 'agent'`, lo que el modelo extrajo de lo que
      dijo el cliente, invariante 7), `flowStage`, `channel`, `withinHours` (zona del negocio). `agent` → ese especialista;
      `start` → el especialista que declara el procedimiento, y **el motor** lo inicia con `_iniciar` al empezar
      el turno (`ContextoMotor.al_empezar_turno`), después de las políticas, con sus guardas de salud del pack y
      de tarea en curso. El router nunca escribe tareas. El payload del botón llega por Redis
      (`aos:ruteo:payload:{negocio}:{conversación}`, 2 min, GETDEL): lo guarda `entrada_sin_llm` solo con el
      ruteo encendido. El motor solo inicia un procedimiento que el agente del turno declara. El `started` de esa tarea queda con `turn_id` NULL (como las confirmaciones al empezar:
      la cabecera del turno se escribe después; ADR-0112 lo admite). Reaplicar el payload a la tarea nueva
      (llenar el dato del botón) queda fuera de alcance.
   3. **Disparadores** (`trigger`): con un candidato ≥ `strongScore` y `margin` sobre el segundo, atiende el
      especialista que declara ese procedimiento. No inicia nada: el hilo elige con `iniciar_procedimiento`.
   4. **Por defecto** (`default`): `elegir_foco` de siempre.
   - **Estabilidad:** una regla o disparador que cambiaría al dueño vigente no lo cambia si en los últimos 6
     turnos enrutados ya hubo `maxOwnerChangesPer6Turns` cambios (se consulta `core_decisions` solo en ese caso;
     el ledger es best-effort y puede contar de menos).
   - Cambiar quién atiende **no es un handoff** (HU-AOS-19): se escribe `conversations.active_agent_instance_id`
     y `owner_since` como en HU-AOS-15. Un episodio nuevo suelta al dueño.
2. **Disparadores léxicos, no embeddings** (`core/kernel/disparadores.py`). *Decisión revisable (respuesta
   provisional a P-19):* puntaje determinista sobre los ejemplos y la descripción de cada procedimiento más los
   `procedures[].overrides.examples` del agente: normalización, equivalencias y vacías del playbook del kernel
   `disparadores` (invariante 3), raíz de 5 letras, pesos IDF y media geométrica 0,6/0,4 de cuánto del ejemplo
   dice el texto y cuánto del texto explica el ejemplo. `triggers.embeddings = true` del contrato se ignora y
   `trigger_embeddings` sigue vacía. El índice se arma una vez por release (inmutable, en la caché de
   `ReleaseCongelada`). Golden `tests/fixtures/ruteo/golden.jsonl` (80 casos, 11 escritos después del ajuste):
   precisión 1,00 en los cinco procedimientos; recall 0,85–1,00 (contratar_plan 11/13 = 0,846, bajo la meta de
   0,85 de R3 por nombres de plan que ningún ejemplo trae). Se sube con ejemplos en el pack o en el agente.
3. **Coprocesador no implementado**: exige LLM; con los valores por defecto (`whenCandidatesAbove` 8 >
   `maxCandidates` 6) nunca aplicaría. Queda pendiente con P-16.
4. **Bandera `tenants.aos_routing`** (`off` | `shadow` | `active`, por defecto `off`; migración 0094), del master,
   con `CHECK (aos_routing = 'off' OR aos_runtime = 'v3')`. Orden de encendido de ADR-0112 ampliado:
   `AOS_CONNECTION_ROUTING` → `aos_runtime = v3` → (`aos_procedures`) → `aos_routing`; `migrar-a-v3 --volver`
   la apaga en el mismo UPDATE. `routing.shadowMode = true` de un KernelSpec solo baja `active` a `shadow`.
   CLI: `agent-core db ruteo --tenant X --modo …` (rol dueño). Con `off`: ni una consulta ni un Redis más; el
   gate en replay no cambia.
5. **Modo sombra en `core_decisions.shadow_route`** (jsonb, migración 0094; `CHECK` gemelo de
   `ck_core_decisions_routed`, `NOT VALID` hasta el contract). `route_source` es siempre lo que pasó de verdad
   (en sombra, `default`); `shadow_route` guarda la decisión sin texto del cliente (`agent_key`, `instance_id`,
   `route_source`, `rule_id`, `procedure_id`, `start`, `confidence`); `candidates` (lo que vio el router) va en
   los dos modos. Las columnas del router solo se escriben en turnos `routed`. Reporte:
   `agent-core db reporte-ruteo --tenant X --dias 7`: precisión y recall por procedimiento frente al primer
   procedimiento iniciado entre ese turno y el siguiente. **Precondiciones:** hay verdad solo con
   `aos_procedures`; y como `iniciar_procedimiento` ofrece solo los del agente en foco, la medida es justa si el
   agente por defecto los declara todos (el General).
6. **Dueño de la tarea:** `ContextoMotor._iniciar` escribe `owner_instance_id` = el especialista del turno.
   Efecto aceptado: en un negocio v3 con procedimientos cambia también `memory_facts.written_by_instance_id` de lo
   que consolida esa tarea, aunque el ruteo esté en `off` (dato, no prompt).
7. **El build valida el ruteo** (`ErrorDelBuild('ruteo_invalido')`): `rules[].agent` es especialista de la
   conexión y `rules[].start` lo declara algún especialista. `defaultAgent` no se exige.

## Contrato de `routing` para los presets (HU-AOS-46)

Fuente única: el **KernelSpec del Coordinador** (`kernelspec/1`, `routing`), que la release congela por su
`agent_version` (componente `core` del manifiesto `release/2`). No hay un campo nuevo en el manifiesto.

```json
"routing": {
  "defaultAgent": "ventas",
  "rules": [
    { "id": "boton_plan", "when": { "interactivePayloadPrefix": "plan:" }, "start": "pack_commerce.contratar_plan" },
    { "id": "cliente_en_mora", "when": { "customerFact": { "namespace": "cobranza", "key": "en_mora", "equals": true } }, "agent": "cobranza" }
  ],
  "triggers": { "minScore": 0.45, "strongScore": 0.80, "margin": 0.15, "maxCandidates": 6 },
  "owner": { "maxOwnerChangesPer6Turns": 2 },
  "shadowMode": false
}
```

- `rules[].agent` = `agents.key` de un especialista instanciado en la conexión; `rules[].start` = id de un
  procedimiento que declara (en `procedures`) algún especialista de la conexión. Si no, el build falla.
- El **dueño de un procedimiento** es el especialista que lo declara en `procedures`. Si lo declaran varios
  (con los presets v2 el General, por defecto, declara también los de Ventas, Agenda y Suscripciones), gana el
  **más específico**: el que declara menos procedimientos; entre empatados, el dueño vigente, luego el que no es
  el agente por defecto, luego por key. *Decisión revisable, conservadora:* no se quita nada al General (sigue
  atendiendo lo que solo él declara y los saludos); solo deja de «ganar» los procedimientos que un especialista
  también declara.
- Ejemplos propios de un agente: `procedures[].overrides.examples` del AgentSpec v3 (suman a los del pack).
- `is_default` de la instancia y `defaultAgent` siguen siendo dos fuentes; gana `is_default` (adenda HU-AOS-16 de
  ADR-0117). Un preset coherente pone los dos iguales. **No se unifican en esta HU** (revisable): hacerlo exige
  que el build rechace la diferencia (rompería a un dueño que cambió `is_default` por la API sin publicar el
  Coordinador) o reescribir el KernelSpec al cambiar la instancia (otra versión del Coordinador por cada
  cambio). Lo conservador es que `is_default` sea la fuente por conexión y `defaultAgent` solo el respaldo de una
  `release/1`; retirarlo del contrato va con el próximo major de `kernelspec`.
- Verificado de punta a punta con el preset `suscripciones` v2 (`presets-v2 --aplicar`): las reglas `preset_*`
  (`keywordAny`) pasan el build y «quiero agendar una demo» la atiende Agenda (`rule`); sin palabra de regla,
  «¿tienen espacio el jueves?» también (`trigger`), aunque el General declare `agendar_cita`.
- Encender: `agent-core db ruteo --tenant X --modo shadow`; una semana y `reporte-ruteo` antes de `active`.

## Consecuencias

- `route_source ≠ default` y `active_agent_instance_id` del especialista elegido con `aos_routing = active`;
  `focus_task_id` con la tarea de una regla `start`. Con `off`, idéntico a HU-AOS-16.
- Costo: sin LLM; 1 lectura de la tarea en foco por turno con el ruteo encendido, más hechos u hora local solo si
  alguna regla los usa y el conteo de estabilidad solo si cambiaría el dueño.
- **Pendientes:** embeddings y `trigger_embeddings` por release (P-19, cuando el golden lo justifique); coprocesador
  (P-16); la ventana de 7 días en sombra (operación); el DDL de design §4.3 (`shadow_route`, `aos_routing`) en
  solvershub-docs; panel del master para la bandera.
