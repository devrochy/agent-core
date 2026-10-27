## Opciones directas: publicar una versión como plantilla de Meta (SPEC-40, HU-OPC-04, 2026-10-27)

- **Pendiente de verificar con Meta (sin cambio):** el payload del QUICK_REPLY al enviar y los límites de 25, 10 y 256
  siguen sin verificarse contra el sandbox real; la bandera `opciones_directas.habilitada` sigue apagada y es la
  puerta (ADR-0172, adenda del PR 5). Este cambio **no envía nada a ningún cliente ni llama al sandbox**.
- Servicio nuevo `core/gateway/opciones_meta.py::publicar_en_meta(negocio, versión, conexión, categoría)`: crea la
  plantilla `<clave>_v<n>` (BODY con parámetros NAMED, FOOTER y botones QUICK_REPLY) en la WABA de la conexión.
  Valida ANTES de reservar o llamar (categoría solo `UTILITY` o `MARKETING`, topes de Meta, `validate_template_components`
  y `compatibilidad_plantilla`, la misma prueba que hace el envío del PR 5). Errores como resultado con un `outcome`
  cerrado, nunca excepción.
- **Nunca se edita una aprobada:** la versión `n + 1` es la plantilla `_v<n+1>` y la anterior sigue ligada y válida
  para los envíos que la usan; no hay llamadas de edición y no se gasta ninguna de las 10 ediciones mensuales.
- **Reintento seguro sin migración:** se reserva la fila de `message_templates` en `DRAFT` (con la categoría y los
  componentes) y se liga `option_template_versions.message_template_id` ANTES del POST; el
  `UNIQUE (waba, nombre, idioma)` existente es la clave de idempotencia. Un reintento tras un corte de red consulta
  a Meta por ese nombre antes de crear: si la plantilla ya existe la adopta (sin otro POST ni otro cupo del límite de
  creación). Candado Redis por (negocio, versión) con token y liberación atómica; sin Redis no se publica. Los `ejemplos` de los parámetros se acotan a 60 caracteres sin saltos de línea.
- El estado (aprobada, rechazada, pausada) es el de `message_templates`, que ya escribe el webhook de estado de
  plantillas; `estado_de_publicacion(negocio, versiones)` lo lee en una consulta para el panel. Meta no recategoriza
  en silencio (`allow_category_change=False`).
- `core/gateway/meta_templates.py`: `create_template` se parte en `post_template` (valida, límite de creación y POST,
  sin tocar la base) y `create_template` (sin cambio de comportamiento para el panel); nueva `find_template_by_name`.
- Observabilidad: log `option_meta_publish`, métrica `option_meta_publications_total{outcome}` y auditoría
  `option_meta_publish` con ids y resultado, sin el texto de Meta.
- ADR-0172: adenda del PR 7. **Limitación conocida:** una versión se liga a UNA plantilla de Meta (una WABA); un
  negocio con dos WABAs envía por la de la versión (`plantilla_de_otra_cuenta` en la otra).
- **API:** `POST /plantillas-opciones/{clave}/publicar` acepta un cuerpo opcional `meta` (conexión, categoría, idioma, ejemplos) y
  publica la versión en Meta tras congelarla; si Meta falla la versión sigue publicada y el reintento es
  `POST …/versiones/{n}/publicar-en-meta`. El detalle de cada versión muestra su estado en Meta. Sin `meta` el comportamiento es el de antes.
  Mismas puertas que el resto de la API (bandera apagada → 404, `connections` con escritura y dueño o master).
