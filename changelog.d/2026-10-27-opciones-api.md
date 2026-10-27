## Opciones directas: API admin de plantillas con opciones (SPEC-40, HU-OPC-05, 2026-10-27)

- `core/api/routers/plantillas_opciones.py` bajo `/api/v1/plantillas-opciones` (reglas en
  `core/admin/plantillas_opciones_service.py`): CRUD de borradores y retiro, `POST /validar` (hallazgos
  `{path, code, severity, mensaje}` sin guardar), `POST /vista-previa` (interactivo, componentes de Meta, cuerpo
  renderizado y texto de respaldo, sin enviar), `POST /{clave}/publicar` (congela la versión `n + 1`, inmutable; con
  errores del lint 422 sin cambiar nada; `sin_cambios` 409), `POST /{clave}/retirar`, `DELETE /{clave}` (borrado
  lógico de lo no publicado o retirado, libera la clave), `POST /{clave}/enviar-prueba` (solo a una conversación del
  negocio, con `clave_idempotencia`, por `sender.send_options_to_conversation`), `GET /catalogo-acciones` (solo las
  tools elegibles del negocio, con su JSON Schema, marca de sensible y confirmación obligatoria) y
  `GET /catalogo-variables`.
- Autorización como `meta_ops.py`: lectura con `connections`; escritura con `require("connections", write=True)` más
  `require_owner_or_master`. **Sin la bandera `opciones_directas.habilitada` (apagada por defecto) toda ruta responde
  404 `opciones_directas_no_habilitadas`** y no toca datos; la clave de otro negocio es un 404 igual que una inexistente.
- La publicación a Meta de la versión (HU-OPC-04) NO está aquí: `gancho_tras_publicar` es el punto de enganche del
  PR siguiente y `message_template_id` sale siempre nulo.
- Lista permitida de rutas para `datos` (pendiente del PR 5): `contexto.RUTAS_DE_DATOS`, cerrada y vacía; el catálogo
  de variables del lint y de la API es `contexto.catalogo_de_variables()` (base + esa lista). Una ruta fuera de ella es
  `DatosInvalidos` al enviar y `variable_fuera_de_catalogo` en el lint.
- `GET /admin/settings/message-templates/missing` distingue el `sin_plantilla` de una plantilla con opciones del de un
  aviso por evento: campos nuevos `tipo` (`evento` | `opcion`), `clave`, `total_eventos_7d` y `total_opciones_7d`
  (aditivos; el valor por defecto es `evento`).
- La clase `Opcion` de la definición pasa a llamarse `OpcionDeLaPlantilla` (el esquema OpenAPI `Opcion` ya era del
  registro de configuración y el nombre repetido habría renombrado ese esquema para el panel).
- `docs/runbooks/opciones-directas.md`: la prueba real del payload del QUICK_REPLY contra el sandbox de Meta como
  paso previo a encender la bandera, y cómo encenderla en un negocio de test.
