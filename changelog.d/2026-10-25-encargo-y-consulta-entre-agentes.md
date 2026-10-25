## Encargo y consulta entre agentes (HU-AOS-19, parte 2)

Cierra HU-AOS-19 con la parte 2. El diseño está en la adenda HU-AOS-19 de ADR-0116, §4 y §5, y el contrato en la adenda 3.2.0 de ADR-0113.

- **`agentcore-contracts` 3.2.0:** `ExposedQuery.action`, opcional. Es la acción de lectura que responde la consulta. El linter exige dos cosas:
  - que la acción esté entre las tools del agente (`consulta_sin_accion_del_agente`);
  - que el `outputSchema` use solo el subconjunto que valida el kernel (`schema_de_salida_no_soportado`, en `core/kernel/esquema_salida.py`).
- **Comando `encargar(agente, procedimiento, entradas)`**, con tarea en foco.
  - Solo acepta procedimientos que el destino expone, publica y que se completan con datos (`core/kernel/colaboracion.py`). Hoy ninguno de los packs lo es.
  - Crea una sub-tarea del destino, valida los datos igual que `actualizar_tarea` y avanza las etapas sin LLM, todo en una sola transacción. Si falta un dato, no queda nada.
  - Vuelve un resultado tipado; los datos `pii` solo si el permiso los deja pasar.
- **Comando `consultar(agente, consulta, parametros)`.**
  - La acción corre con el spec y la identidad del destino, sin escribir memoria, con auditoría y una clave idempotente.
  - La salida se valida contra el schema y entra como dato.
  - No se puede consultar una acción que aparece en alguna etapa de la release ni una que pide confirmación.
- **Solo lectura, fail-closed:** una consulta solo puede usar una acción marcada `MCPTool.solo_lectura` que no crea registros. Hoy la única marcada es `sub_consultar`; el linter avisa con `consulta_con_accion_que_escribe`.
- **Límites del resultado de una consulta:**
  - solo vuelven las propiedades que declara el schema;
  - un schema abierto exige que el enlace permita `pii`;
  - la acción tiene el tope de tiempo del enlace;
  - se audita como `query:` y no cuenta como evidencia de que el agente del turno llamó la tool.
- **`_execute_tool_safely`** acepta `identidad` y `solo_lectura`.
