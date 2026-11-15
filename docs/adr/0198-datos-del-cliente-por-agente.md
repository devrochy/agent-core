# ADR-0198 — Los datos que pide el cliente son de cada agente especialista

- **Estado:** propuesto (se acepta al mergear la HU-CRM-28).
- **Fecha:** 2026-11-14
- **Contexto:** [SPEC-33](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-33-crm/spec.md) (HU-CRM-13 y HU-CRM-28), D-56, ADR-0179 y ADR-0190.
- **Enmienda:** ADR-0179 decisiones 1, 2 y 3 (la política sale de `AppSettings.behavior_config` y pasa a cada agente; el aviso de tratamiento de datos sigue siendo del negocio).

## Contexto

HU-CRM-13 puso una política de datos del cliente por módulo y acción. El dueño ajusta, por dato,
`requerido`, `recomendado` o `no_pedir`, y la política vive en
`AppSettings.behavior_config["datos_cliente"]["politica"]`. El panel la muestra en Configuración ›
Datos del cliente y el runtime la resuelve desde el negocio.

Con ADR-0190 las habilidades se instalan y configuran por agente. El agente especialista ya tiene
sus propias acciones (`tools`) y sus packs, y el runtime conoce el agente que ejecuta cada tool
(`ToolContext.agent_id` y `release_vigente.agente_del_turno()`). La política del negocio quedó en el
lugar equivocado: un agente de Comercio ve operaciones de otros agentes, dos especialistas del mismo
tipo no pueden pedir datos distintos, y el dueño no encuentra la configuración al editar el agente.

## Decisión

1. **La política es de cada agente especialista.** Vive en `agents.data_policy` como
   `{politica: {accion_id: {clave: nivel}}, herramientas: bool, metodos_entrega: [...]}`.
   `metodos_entrega` (HU-CRM-28, adenda de 2026-11-14) guarda qué métodos de entrega de Comercio
   puede ofrecer el agente (`domicilio`, `recoger_en_tienda`); sin el dato, ambos. Solo se guarda
   lo que difiere del valor por defecto del módulo. No hay política global.
2. **Precedencia:** ajuste del agente > configuración del módulo (`base`) > defecto de la acción.
   Los pisos técnicos (`ajustable=False`) siguen sin poder bajarse.
3. **Cada acción de datos declara sus tools.** `AccionDatos.tools` relaciona una operación
   (`ventas.pedido_domicilio`) con las acciones del agente (`crear_pedido`). El panel muestra solo
   las operaciones que ese agente puede ejecutar; una acción sin `tools` se filtra por pack.
4. **Runtime por agente.** `evaluar_accion()`, `requeridos_efectivos()`, `guardar()`,
   `herramientas_activas()` y los métodos de entrega reciben el `agent_id` del `ToolContext`; sin
   él usan el especialista en foco de la release. Fuera de una conversación se usan los defaults
   del módulo. `crear_pedido` y el resolver `pack_commerce.metodo_de_entrega` rechazan un método
   que el agente no tenga habilitado y devuelven los disponibles; el procedimiento ofrece solo esos
   como candidatos.
5. **API y panel.** `GET/PUT /admin/agents/{id}/datos-cliente` con permiso `settings` (escritura) y
   auditoría `agent.datos_cliente`. El editor del agente gana el paso «Datos del cliente», después
   de Acciones, con la matriz editable, el interruptor de herramientas y los datos obligatorios de
   sus superpoderes en solo lectura. Se retiran `GET/PUT /admin/datos-cliente/politica` y la matriz
   de Configuración.
6. **Migración.** `scripts/migrar_datos_cliente_agentes.py` (`--dry-run` por defecto) copia la
   política global a cada especialista solo para las operaciones que ejecuta y quita `politica` y
   `herramientas` de `behavior_config`. Las operaciones sin agente se reportan y se descartan; un
   agente nuevo arranca con los defaults del módulo.
7. **El aviso de tratamiento de datos no cambia.** Sigue siendo del negocio en
   `behavior_config["datos_cliente"]["aviso"]`, junto con el consentimiento y la baja.

## Consecuencias

- **Positivas.** D-35 se cumple también en los datos: cada agente pide lo suyo; dos especialistas
  del mismo tipo pueden pedir distinto; el dueño configura donde edita al agente; el runtime no
  necesita releer una política global.
- **Negativas.** Una columna JSONB nueva en `agents` y una migración de datos; el panel debe
  regenerar tipos y mover la pantalla; las acciones de datos tienen que declarar sus tools y
  mantenerse sincronizadas.
- **Riesgos.** Un agente sin `tools` mapeadas no muestra la operación (se documenta y se prueba);
  los turnos sin agente usan defaults; la migración descarta ajustes de operaciones huérfanas.
- **Qué haría que revisáramos esto.** Que el dueño necesite una política común para varios agentes
  (pediría una plantilla de política, no volver al negocio); que una operación se ejecute por un
  agente distinto del foco sin `ToolContext` (pediría un contextvar explícito).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Dejar la política en el negocio y solo mover la pantalla | Dos agentes del mismo tipo seguirían sin poder pedir datos distintos; el alcance seguiría mintiendo |
| Guardar la política en el `AgentSpec` versionado | La configuración operativa por agente de ADR-0190 no se versiona; el cambio exigiría publicar y tocar el contrato `agentspec/3` |
| Una tabla nueva de políticas | Es un JSONB pequeño por agente; `agents.data_policy` ya tiene RLS y dueño natural |
| Derivar todo de los `slots` de los procedimientos | Los packs validan por acción antes de ejecutar; los slots no cubren los pedidos ni la inscripción |
| Copiar la política global a todos los agentes | Les daría operaciones que no ejecutan y confundiría al dueño |

## Impacto en el código

- `core/db/models.py` (`AgentRecord.data_policy`) y migración `0134`.
- `core/datos_cliente/` (`modelo.py` con `AccionDatos.tools`, `ajustes.py` con los métodos de
  entrega, `servicio.py`, `legado.py`, `guardar.py`, `tools.py`) y los `datos.py` de commerce,
  calendar y subscriptions. `pack_commerce` sube a 3.3.0: `crear_pedido` valida los métodos y el
  procedimiento `tomar_pedido` 1.2.0 usa el resolver y los candidatos dinámicos.
- `core/orchestrator/loop.py`, `core/memory/historial_tool.py`, `core/tools/core_skill.py`.
- `core/api/routers/datos_cliente.py` y `core/api/__init__.py`.
- `scripts/migrar_datos_cliente_agentes.py`.
- Panel: paso «Datos del cliente» del editor, BFF `/api/agent-editor/{id}/datos-cliente`, retiro de
  la matriz de Configuración y tipos regenerados.
- `docs/openapi.json` regenerado.
