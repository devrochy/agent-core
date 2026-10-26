# ADR-0169 — Depurador del turno, sección de observabilidad y redacción

- **Estado:** propuesto (se acepta al mergear la parte 1 de HU-AOS-28).
- **Fecha:** 2026-10-26
- **Contexto:** HU-AOS-28 (SPEC-26, REQ-AOS-04 y REQ-AOS-08), P-17 («solo `observabilidad:ver`, siempre redactadas»).
- **Enmienda:**
  - ADR-0047 §3 y ADR-0136 (una sección que el colaborador no hereda);
  - ADR-0114 (la toma humana queda en la tarea).
- **Usa:**
  - ADR-0112 (`core_decisions` / `turn_outcomes`);
  - ADR-0114 (`task_events`);
  - ADR-0119 (verificador);
  - ADR-0145 y ADR-0053 (retención);
  - ADR-0117 (releases congeladas).

## Contexto

El kernel ya deja, por cada turno y con índice `(tenant_id, turn_id)`, casi todo lo que hace falta para entenderlo:
- **`core_decisions`:** decisión, candidatos, versión y carril;
- **`turn_outcomes`:** resultado, verificador, tokens, costo y latencia;
- **`task_events`:** cambios de estado de las tareas;
- **`audit_log`:** comandos del hilo, decisiones del motor, `tool:*` y escalación;
- **`messages`:** lo enviado;
- **`usage_ledger`:** costo por tipo de llamado;
- **`a2a_conversations`:** operaciones entre agentes.

Pero ninguna API lo junta, y el panel no puede pasar de un mensaje a su turno.

Faltan además tres piezas:
- **Permiso:** no hay un permiso para ver el detalle técnico.
- **Redacción:** `audit_log` guarda crudas las entradas y salidas de las `tool:*`, con datos personales.
- **Relevo humano:** cuando una persona toma una conversación escalada, nada le dice qué faltaba ni queda rastro en la tarea.

## Decisión

### 1. Sección `observability`, que el colaborador no hereda (enmienda ADR-0047 §3 y ADR-0136)

Se agrega `observability` («Observabilidad») a `CORE_SECTIONS` y a un conjunto nuevo `OPT_IN_SECTIONS`. En esas secciones:
- el **owner** hereda `write`;
- el **colaborador** no hereda nada: solo tiene acceso con una fila explícita en `admin_permissions`;
- el **master** (y el tester habilitado) entra por bypass.

Es la alternativa 1 de ADR-0136 («sin acceso por defecto para el colaborador»), acotada a una sección nueva. Allí se descartó porque dejaba sin menú a los colaboradores existentes, y con una sección que nace así no pasa.

Detalles de la implementación:
- La regla vive en `acceso_por_rol` (función pura). `revoke_permission` y `set_permissions` (`collaborators_service.py`) dejan de calcular la herencia por su cuenta y usan `acceso_por_rol(...) is not None`. Si no, escribirían filas `none` innecesarias.
- `observability` no va en `GLOBAL_ONLY_SECTIONS`, ni en `OWNER_ONLY_SECTIONS`, ni en `OWNER_GRANTABLE_SECTIONS` (pese al nombre, es lo que el owner **no** puede otorgar). El dueño la otorga desde Colaboradores. Allí el colaborador la ve como «sin acceso por defecto»: `acceso_y_origen` devuelve `none` heredado.
- `notification_recipients` no cambia (sin fila, solo el owner).
- Si aparece otra sección con otra regla, se fusionan los conjuntos en vez de abrir un cuarto.

### 2. Qué protege la sección

Sin `observability` se ve la línea de tiempo del turno:
- la decisión;
- los pasos: qué comando o acción, y su resultado **sin valores** (ok o error y los códigos de error, nunca el contenido de la salida);
- los cambios de estado, las comprobaciones del verificador, el envío y la escalación.

Con `observability` se ven además la entrada y la salida de cada paso (`audit_log.input` y `.output`), siempre redactadas (§3).

Además:
- La línea de tiempo exige `conversations` en lectura: es parte de una conversación que esa persona ya puede leer.
- El costo exige `costs`, como el tablero de fiabilidad (HU-AOS-29).

### 3. Redacción al leer: forma visible, datos personales enmascarados (P-17)

Un redactor puro (`core/shared/redaccion.py`, `enmascarar(valor, clave=None, data_class=None)`) recorre el JSON y enmascara:
- **Por la clave:** correo/email, teléfono/phone/celular, documento/cédula/nit/dni, dirección/address, titular, tarjeta/card/cvv/iban/account, token, password y secret. La comparación es sin tildes ni mayúsculas, sobre segmentos de la clave (`customer_email`, `datos.telefono`, `cardNumber`). No cubre `nombre`/`name` ni `cuenta`/`clave`: chocan con productos y códigos («pan», «cuenta de cobro», «clave_producto»).
- **Por la clase** del dato del procedimiento, en los eventos de tarea: `payment` siempre oculto.
- **Por el patrón** del valor de cualquier texto: correos (`j***@gmail.com`) y secuencias de 7 dígitos o más (`•••4821`).

Se dejan tal cual los nombres de los campos, los tipos, los booleanos, los números que no salen de una clave sensible, los códigos y los estados, incluida la clave interna `_tool_name`.

En los payloads de `audit_log` no hay `data_class`: «pagos siempre enmascarados» vale para los datos `payment` de las tareas. En `audit_log` cuentan solo la clave y el patrón, así que una referencia como `WOMPI-abc12` se ve. No se cruzan las claves de los argumentos de las tools con los datos del procedimiento.

**Por qué al leer y no al escribir:**
- `_historical_tool_results` lee `AuditLog.output` crudo para el guardrail `required_tool` (`loop.py`).
- El verificador usa `audit_log` como evidencia (ADR-0119).
- Redactar al escribir rompería las dos cosas, y además `audit_log` es inmutable (ADR-0008), así que no cubriría el histórico.

**Frontera:** minimizar al escribir (`resumen_de_auditoria`, ADR-0145) es control de **retención**; redactar al leer es control de **visualización**. Este ADR no cambia la retención ni la purga por cliente (ADR-0053). Los textos de `messages` y los datos `pii` de las tareas se muestran a quien tiene `conversations`, porque ya los ve en el hilo; los `payment`, nunca. `OCULTO` es uno solo, el del redactor; lo usan también las tareas (`conversation_tasks`).

### 4. API del turno

`GET /api/v1/admin/turns/{turn_id}` (`require("conversations")`). El servicio `core/observability/turno.py` exige el `tenant_scope` y filtra `tenant_id` explícito en cada consulta, además de la RLS.

Devuelve en una respuesta:
- el turno;
- la decisión;
- los pasos de `audit_log`, en orden;
- los eventos de `task_events`;
- el verificador;
- las operaciones A2A (con `payload_redacted`, que ya viene redactado);
- los mensajes salientes;
- el costo por tipo;
- los flags `can_see_detail` y `can_see_costs`.

Son unas ocho consultas por los índices existentes, con un tope de 200 filas por bloque; objetivo ≤ 2 s.

Casos especiales:
- **Otro negocio o playground:** un turno de otro negocio da 404. También da 404 uno de una conversación del playground (`TEST_CHANNEL`): allí las acciones corren de verdad y no es tráfico de clientes. Depurar el playground sería una decisión aparte.
- **Enlace desde el chat:** `MessageItem` gana `turn_id` (aditivo), solo en los mensajes del agente; los del cliente no tienen turno propio (se enlazan por `core_decisions.inbound_message_ids`, que la línea de tiempo devuelve). Solo los mensajes del agente abren el depurador desde el chat.
- **Trabajo en Celery:** lo que corre en Celery sin `turn_id` (ADR-0112) no aparece en la línea de tiempo.

### 5. «En curso» y la toma humana (enmienda ADR-0114)

`GET /api/v1/admin/conversations/{id}/in-progress` (`require("conversations")`, 404 en el playground) devuelve, con la tarea en foco primero:
- las tareas **abiertas**;
- también, en solo lectura, las **cerradas como `escalated`** desde que se creó la escalación activa. El error de acción con relevo humano y el cambio de versión con un efecto irreversible cierran la tarea como `escalated` justo antes de escalar: sin ellas, la tarjeta saldría vacía en el caso en que más se necesita.

Por cada tarea:
- el procedimiento, la etapa, el estado y el `status_reason`;
- los datos ya dados (pagos enmascarados) y la confirmación pendiente;
- **lo que falta**: la lista estructurada de `motor.faltas_de_salida` (clase y nombre), más el rótulo humano del dato que declara su habilidad, y el panel pone el texto. No se reusan los textos de `tarjeta.describir_faltas`: salen del playbook del kernel y son instrucciones para el modelo, la audiencia equivocada;
- la última acción y su resultado, sin los `candidates` de `action_succeeded`.

**Fuente de la definición:** se busca la clave exacta `(procedure_id, procedure_version)` en los congelados de la release de la conversación (`conversations.release_id`, cargada con `cargar_release` en el `tenant_scope` de la request) y, si no está, en el binario (`registro.definicion`, que recibe la release como parámetro en vez de depender de `release_vigente`). Nunca `procedimiento_para`, que resuelve por major. Los motivos van como códigos:
- sin release (legacy), se usa el binario;
- sin definición exacta, faltas vacías con `definicion_no_disponible`;
- sin tareas, `sin_procedimientos`.

**La toma humana:** tomar una conversación escalada (`POST …/take`) escribe un evento **`taken`** (actor `human`, `data = {admin_id, escalation_id}`) en cada tarea **abierta**. Lo escribe solo si la toma es nueva (`recien_tomada`; `take` ya es idempotente). Devolverla al agente o resolverla escribe **`released`**, solo si cerró una escalación que tenía a alguien asignado. Los dos:
- son eventos de anotación, con una rama propia en `aplicar`;
- no cambian el estado, la etapa ni los datos;
- solo los emite `human`;
- no se aceptan sobre una tarea terminal: la regla de lo terminal no se relaja.

Se respeta el orden de bloqueos (conversación → tareas, `state.registrar`): `take` bloquea primero la conversación. Una tarea que se cerró entre la lectura y la escritura se salta sin tumbar la toma. `owner_changed` no sirve para esto: exige una instancia de agente y cambia el dueño.

**Consecuencia aceptada:** como todo evento, `taken` y `released` actualizan `updated_at`. Eso reinicia el reloj de expiración por episodio y el del vencimiento de las esperas de pago de esa tarea: una persona que toma la conversación la «mantiene viva», que es lo esperado. Los consumidores de `task_events` filtran por tipo y los ignoran. Los tipos no están en `agentcore_contracts`: no hay cambio de contrato.

**Migración 0111:** reemplaza el CHECK inline de `task_events.type` (Postgres lo llama `task_events_type_check`; se verifica en `pg_constraint`) por `ck_task_events_type`, creado `NOT VALID` y luego `VALIDATE`, como en la 0106. En la práctica es irreversible: con filas `taken`, el CHECK viejo no valida y `prevent_mutation()` no deja borrarlas. El número se confirma contra develop al mergear.

### 6. Reproducir el turno

Queda fuera de este ADR; tendrá su adenda propia. Va sobre la infraestructura aislada del gate de pass^k (base `agentcore_eval`, negocio efímero, release exacta, pasarela y correo simulados), nunca en el playground.

## Alternativas consideradas

- **Reusar la sección `agents` para el detalle:** mezcla editar agentes con ver datos de clientes. El dueño la descartó.
- **Redactar al escribir:** rompe `required_tool` y la evidencia del verificador, y no cubre el histórico de un log inmutable.
- **Mostrar solo la forma (sin valores):** ayuda poco a depurar (un código de pedido o un monto son la pista). El dueño eligió enmascarar solo lo personal.
- **Resumen de «En curso» generado por LLM:** cuesta un llamado por toma y puede equivocarse. Lo determinista sale del mismo motor que decide el turno.
- **Anotar la toma en `audit_log`:** no aparece en la línea de eventos de la tarea, que es lo que lee el siguiente que la retoma.

## Consecuencias

- **Dueño:** depura un turno desde el chat sin acceso a la base.
- **Colaborador:** solo ve las entradas y salidas de las acciones si se las da el dueño.
- **Redactor:** es heurístico. Un dato personal bajo una clave neutra y sin formato de correo ni de número largo puede verse, por ejemplo un nombre en `nota`. Se acepta, porque solo lo ve quien tiene `observability` y `conversations`. Las tools pueden seguir declarando `resumen_de_auditoria` para guardar menos.
- **`task_events`:** gana dos tipos de anotación. La migración 0111 no se revierte en la práctica.
- **Rendimiento:** si la API del turno pasara de 2 s con turnos grandes, el siguiente paso sería paginar los pasos, no cachear.

## Impacto en el código

- **`core/admin/permissions.py`:** `observability`, más `OPT_IN_SECTIONS` en `acceso_por_rol`.
- **`core/admin/collaborators_service.py`:** la herencia sale de `acceso_por_rol`.
- **`core/shared/redaccion.py`:** redactor; `conversation_tasks` usa su `OCULTO`.
- **`core/observability/turno.py`** (servicio: lee y redacta) y **`core/api/routers/turnos.py`**; `turn_id` en `MessageItem` (`conversations.py`).
- **Parte 2:**
  - `/in-progress` en `conversation_tasks.py`;
  - `registro.definicion` con la release como parámetro;
  - `taken` y `released` en `core/kernel/state.py`;
  - `take`, `return-to-bot` y `resolve` en `conversations.py`;
  - migración 0111.
- **`docs/openapi.json`**; panel (línea de tiempo, tarjeta «En curso» y sección en Colaboradores).
