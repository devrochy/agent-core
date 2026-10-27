# Opciones directas: prueba del sandbox de Meta y encendido de la bandera (runbook)

SPEC-40, ADR-0172. Las plantillas con botones que ejecutan una tool sin LLM están construidas (API
`/api/v1/plantillas-opciones`, envío, resolver del toque), pero la bandera `opciones_directas.habilitada` está
**apagada por defecto** y NO se enciende en ningún negocio hasta cerrar el paso 1. El encendido lo hace el master con la ruta de
plataforma (§2) y la prueba de Meta la hace el dueño a mano: ninguna sesión de Claude envía a Meta ni usa credenciales.

## 1. Prueba real del payload del QUICK_REPLY (puerta para encender la bandera)

**Por qué.** El diseño supone que el payload de un botón de plantilla se manda **al enviar** (componente `button` con
`sub_type: "quick_reply"`, `index` como texto y un parámetro `payload`) y que vuelve en el webhook como
`type: "button"` con `button.payload`. No está confirmado contra la documentación oficial. Si Meta lo fija al crear la
plantilla, hay que reabrir ADR-0172 §3 (payload `optv:<version_id>:<option_id>`, firma sin `send_id`); no basta con
cambiar una función, porque el `send_id` dentro del payload y el orden «alta antes de encolar» dependen de ello.

**Qué se necesita (todo del número de PRUEBA, nunca producción):**

- Una plantilla `APPROVED` en `es` con 1 a 3 botones QUICK_REPLY y sin variables, en la WABA del sandbox.
- El celular del dueño agregado como destinatario del número de prueba (la ventana de 24 h no hace falta: es plantilla).
- Las variables, en la terminal de quien corre la prueba (no se commitean):

```bash
export OPCIONES_META_SANDBOX=1
export OPCIONES_META_PHONE_NUMBER_ID=<id del número de prueba>
export OPCIONES_META_ACCESS_TOKEN=<token del sandbox>
export OPCIONES_META_TO=<celular del dueño, con indicativo>
export OPCIONES_META_TEMPLATE=<nombre de la plantilla aprobada>
export OPCIONES_META_BOTONES=1        # cuántos botones tiene esa plantilla (hasta 10)
python -m pytest tests/test_opciones_envio_meta_sandbox.py -q
```

**Cómo se lee el resultado.**

1. La prueba pasa si Meta **acepta** el envío (200 con el id del mensaje). Un 400 de Graph significa que la forma de
   `envio.componentes_con_payload` está mal.
2. Paso manual, que la prueba no puede hacer: en el celular, tocar el botón y mirar el log del webhook
   (`inbound.py`): debe llegar `type: "button"` con `button.payload` **idéntico** al enviado (hasta 67 caracteres).
3. Repetir con el máximo de botones que se quiera ofrecer, y con el texto del botón de 20 y 25 caracteres, para
   confirmar los topes que el repo asume sin haberlos verificado contra la documentación primaria (25 caracteres del
   texto del botón, 10 botones, 256 del payload).

**Registrar el resultado** en el checklist del PR de publicación a Meta (PR 7, HU-OPC-04) y en la adenda del ADR. Si
algo difiere, no se enciende la bandera: se reabre el diseño.

## 2. Encender la bandera en UN negocio de test

Solo con el paso 1 registrado, el ADR-0172 aceptado y el OK del dueño (la ruta no verifica nada de esto: la puerta es
humana). La bandera la fija **únicamente el master** y es por negocio
(`tenants.settings["opciones_directas"] = {"habilitada": true}`). El escritor único es
`core/kernel/opciones/bandera.py::fijar` (atómico con `jsonb_set`, no pisa el resto de `settings`, audita
`opciones_directas_bandera`) y su única llamada es la ruta de plataforma de abajo (PR 10a). El interruptor en el panel
master llega en el PR 10b y usa esta misma ruta.

**Cómo se enciende (ruta master).** El negocio se elige por su id (el listado `GET /api/v1/admin/platform/tenants`
lo trae). Con la sesión del master (no vale el tester ni el dueño: 403):

```bash
# estado actual: {"habilitada": false} de fábrica
curl -s -H "Authorization: Bearer $TOKEN" "$API/api/v1/admin/platform/tenants/$TENANT_ID/opciones-directas"
# encender (idempotente) / apagar
curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"habilitada": true}' "$API/api/v1/admin/platform/tenants/$TENANT_ID/opciones-directas"
```

`404` si el negocio no existe o está borrado; `422` si el cuerpo trae algo que no sea `habilitada` booleana. Nunca se
usa SQL suelto.

**Cómo se verifica.**

1. `GET …/opciones-directas` devuelve `{"habilitada": true}` y el `audit_log` tiene la fila `opciones_directas_bandera`
   con `actor = admin:<email del master>` y el valor.
2. Como dueño de ESE negocio, `GET /api/v1/plantillas-opciones` responde 200 (con la bandera apagada responde
   `404 opciones_directas_no_habilitadas`). Otro negocio sigue en 404: la bandera es por negocio.
3. `GET /api/v1/plantillas-opciones/catalogo-acciones` lista las acciones disponibles (ver §2.1). Vacío es normal si el
   negocio no tiene el binding de ninguna.

**Cómo se apaga (rollback inmediato).** `PUT {"habilitada": false}` (no hay `DELETE`: apagar es fijar `false`).
Lo enviado queda sin efecto (el toque ya no se resuelve) y no hay estado que limpiar. Nadie más la repone: los caminos
que reescriben `tenants.settings` entero (suscripciones, presets, alta) leen la fila con `FOR UPDATE` y el apagado
espera ese candado (ADR-0172, adenda del PR 10a).

Con la bandera encendida, para el negocio:

- el panel (PR 8) muestra el constructor; la API responde en vez de `404 opciones_directas_no_habilitadas`;
- la tool `enviar_plantilla_opciones` se ofrece a los agentes del negocio;
- el consumer atiende los payloads `opt:`, `optc:` y `optn:`.

### 2.1 Qué acciones hay y qué hace falta para verlas

Una tool es acción de una opción solo si se cumplen **las cinco**: la marcó su autor (`apta_para_opcion`), su pack está
activo en el negocio, tiene un **binding explícito** y habilitado en el asistente, ningún procedimiento de los packs
del negocio la nombra, y la bandera del negocio está encendida. Hoy hay una marcada:

| Tool | Pack | Qué hace | Por qué es segura | Qué hace falta |
|---|---|---|---|---|
| `list_client_appointments` | `pack_calendar` | Cuenta/lista las citas agendadas del cliente de la conversación (`total`, `proximas`) | Solo lee; siempre acotada al cliente y a la conversación del toque (no puede traer citas de otro); sin argumentos obligatorios; no envía nada; no es `sensible` | Pack de agenda activo y su binding habilitado (Configuración → Habilidades, `PUT /api/v1/admin/settings/skills`) |

La respuesta de una opción es un texto fijo que solo cita valores escalares de primer nivel del resultado (`{result.k}`):
para esta tool, `proximas` y `total` (la lista `appointments` no se cita). Ejemplo de respuesta de una opción
«Ver mis citas»: `Tienes {result.proximas} cita(s) próxima(s). Escríbenos si quieres moverlas.`

**Tools seguras que NO llegan al catálogo (y por qué).** `find_next_available`, `list_appointment_types`,
`list_calendars` (las nombra el procedimiento `agendar_cita`) y `sub_consultar` (la nombra `cambiar_plan`) son de solo
lectura pero las gobierna un procedimiento de su propio pack: mientras el pack esté activo, el lint las rechaza con
`tool_en_procedimiento`. Cambiar esa regla para las de solo lectura es una decisión de ADR aparte. `escalate_to_human`
(el botón «Hablar con un asesor») no se marcó: no es idempotente (cada toque crea otra escalación y otro aviso).

**Tools declarativas.** Un dueño puede marcar una tool declarativa suya con `"option_safe": true` (solo el literal
`true`; si hace POST nace `sensible`) por `PUT /api/v1/admin/settings/declarative-tools` o por el editor crudo del
AgentSpec (`PUT /api/v1/admin/settings/advanced/spec`); el panel no tiene un editor de tools declarativas. Esas rutas
exigen el permiso `settings` con escritura, **no** `require_owner_or_master` (que sí exige definir una plantilla): un
colaborador con escritura en Configuración puede marcar una tool apta. Con un AgentSpec v3 no es posible
(`declarativeTools` está congelado).

## 3. Qué mirar tras encenderla en test

- `GET /api/v1/admin/settings/message-templates/missing`: los envíos que no salieron por falta de plantilla de Meta
  aprobada vienen con `tipo: "opcion"` y su `clave` (no son del mapeo de eventos: el remedio es publicar la versión en
  Meta, no editar el mapeo).
- Logs `option_send` y `option_tap`, métricas `option_sends_total`, `option_taps_total{outcome}` y
  `option_tool_errors_total{tool}`; alerta si crece `bad_signature`.
- `audit_log`: `option_template_*` (quién creó, publicó o retiró), `option_send` (con el actor `admin:<id>` de un envío
  de prueba) y `option_tap`.
