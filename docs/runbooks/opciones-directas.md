# Opciones directas: prueba del sandbox de Meta y encendido de la bandera (runbook)

SPEC-40, ADR-0172. Las plantillas con botones que ejecutan una tool sin LLM están construidas (API
`/api/v1/plantillas-opciones`, envío, resolver del toque), pero la bandera `opciones_directas.habilitada` está
**apagada por defecto** y NO se enciende en ningún negocio hasta cerrar el paso 1. Todo lo de abajo lo hace el dueño o
el master a mano: ninguna sesión de Claude envía a Meta ni usa credenciales.

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

Solo con el paso 1 registrado y el OK del dueño. La bandera la fija **únicamente el master** y es por negocio
(`tenants.settings["opciones_directas"] = {"habilitada": true}`). El escritor único es
`core/kernel/opciones/bandera.py::fijar` (atómico con `jsonb_set`, audita `opciones_directas_bandera`). **Todavía no
hay ruta de plataforma que lo llame**: se difiere al PR de publicación a Meta (ADR-0172, adenda del PR 6). Mientras no
exista, el encendido es una operación del master/DBA sobre el negocio de test, nunca con SQL suelto en producción.

Con la bandera encendida, para el negocio:

- el panel (PR 8) muestra el constructor; la API responde en vez de `404 opciones_directas_no_habilitadas`;
- la tool `enviar_plantilla_opciones` se ofrece a los agentes del negocio;
- el consumer atiende los payloads `opt:`, `optc:` y `optn:`.

Apagarla es el rollback inmediato: lo enviado queda sin efecto (el toque ya no se resuelve) y no hay estado que limpiar.

## 3. Qué mirar tras encenderla en test

- `GET /api/v1/admin/settings/message-templates/missing`: los envíos que no salieron por falta de plantilla de Meta
  aprobada vienen con `tipo: "opcion"` y su `clave` (no son del mapeo de eventos: el remedio es publicar la versión en
  Meta, no editar el mapeo).
- Logs `option_send` y `option_tap`, métricas `option_sends_total`, `option_taps_total{outcome}` y
  `option_tool_errors_total{tool}`; alerta si crece `bad_signature`.
- `audit_log`: `option_template_*` (quién creó, publicó o retiró), `option_send` (con el actor `admin:<id>` de un envío
  de prueba) y `option_tap`.
