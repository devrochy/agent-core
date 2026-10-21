# ADR-0149 — Estado e interés del lead editables desde la ficha, y el perfil como fuente de los datos de contacto

- **Estado:** propuesto (falta que lo acepte el dueño).
- **Fecha:** 2026-10-21
- **Contexto:** SPEC-33 «CRM». **Enmienda:** ADR-0056 (el `LeadProvider` suma `edicion`). **Precisa:**
  ADR-0022 (el estado del lead vuelve a editarse a mano, en la tabla que ya existe). **Usa:** ADR-0114
  (la memoria del cliente se escribe solo por `core/memory/facts.py`), ADR-0147 (formato de enmienda).

## Contexto

Los datos de un cliente viven en tres sitios: el perfil (`memory_facts` y su proyección
`customers.attributes`, editable desde la ficha), el lead de venta de pack_commerce
(`pack_commerce_leads`: nombre, teléfono, correo, interés, puntaje y estado) y el pedido. Del lead la
ficha solo mostraba columnas de solo lectura. El dueño quiere mover el lead de etapa desde el panel y
la APK (marcarlo «contactado» o «perdido») y corregir el interés, como en agent-sale, y que el
nombre, el teléfono y el correo no aparezcan dos veces con valores distintos.

ADR-0022 decía, en «Qué haría que revisáramos esto», que si un admin necesita reordenar a mano las
etapas de un pipeline de ventas «conviene una tabla real de nuevo, con este ADR reemplazado». Este es
ese caso, pero la tabla ya existe: `pack_commerce_leads.status` lo escriben hoy las tools
(`actualizar_lead`, `calificar_lead`) y el seguimiento de `jobs.py`. No hace falta una tabla nueva ni
reemplazar ADR-0022: el cliente sigue siendo del core y la clasificación (nuevo, ocasional, frecuente,
fiel, inactivo) se sigue calculando en vivo. Lo que se edita a mano es el estado del lead de venta.

## Decisión

1. **Hook `edicion` del `LeadProvider`** (`core/admin/lead_registry.py`), opcional:
   `LeadEdicion(campos, leer, escribir)`. Cada `LeadCampoEditable` declara clave, etiqueta, tipo
   (`opciones` con valores cerrados, o `texto` con largo máximo). `leer(customer_id)` devuelve los
   valores actuales (códigos, no etiquetas) y `escribir(customer_id, valores)` los guarda. El core no
   conoce las tablas del pack (invariante 5). No se agregan tipos de campo hasta que un segundo pack los
   necesite.
2. **API.** `GET /admin/leads/{id}` suma `editables` (pack, clave, etiqueta, tipo, opciones, máximo y
   valor); si `leer` falla, ese pack se omite y se loguea, como con los resolvers. Los cambios van por
   una ruta aparte, `PATCH /admin/leads/{id}/pack/{plugin_id}` (sección `leads`, escritura), para que
   el `PATCH /{id}` del perfil siga escribiendo solo por `facts.py`. El core valida antes de llamar al
   pack: pack habilitado y con `edicion` (si no, **404**, sin decir cuál de las dos cosas falla), claves
   declaradas y valores dentro de las opciones o del largo (si no, 400 con `fields`). Valida el cliente
   en su sesión, la cierra y después llama a `escribir`.
3. **pack_commerce** expone `estado_lead` (las cinco etapas) e `interes` (texto, 500) del lead que la
   ficha ya mostraba: el de actividad más reciente del cliente. `escribir` bloquea la fila del cliente
   (`FOR NO KEY UPDATE`, el mismo orden que `facts.py`) antes de buscar el lead, filtra por tenant y, si
   el cliente no tiene lead, lo crea con `idempotency_key = manual:<cliente>` (como el formulario del
   panel SSR) **sin** copiar datos de contacto. Solo un cambio de estado mueve `last_activity_at`.
4. **Las tools siguen siendo lo que escribe el asistente.** `crear_lead`, `actualizar_lead` y
   `calificar_lead` no cambian su descripción, sus parámetros ni su salida.
5. **Fuente de los datos de contacto en la ficha: el perfil.**
   - Nombre: `customers.name`, que proyecta el hecho `perfil.name`.
   - Teléfono: `customers.contact_phone` es la identidad del canal, de solo lectura. Si el perfil tiene
     `phone` (una sola clave, como `name` y `email`) y es distinto, se muestra como dato del perfil,
     editable.
   - Correo: `perfil.email`.
   - `pack_commerce_leads.name/phone/email` quedan como captura interna del pack (intake de la landing,
     seguimiento) y no se muestran ni se editan en la ficha. Para que un dato que el cliente dicta en el
     chat no quede escondido ahí, `crear_lead` y `actualizar_lead` también lo escriben en el perfil
     (`entity_memory.upsert_attributes`): nombre, correo y teléfono. Ningún escenario del gate de evals
     llama esas tools, así que el replay no cambia.
   - Ese reflejo limpia espacios y recorta como `completar_lead` (200, 254 y 40 caracteres).
   - El saludo del seguimiento (`jobs.py`) y la vista SSR de leads prefieren `customers.name` al
     nombre del lead.

## Riesgos asumidos

- **Varios leads por cliente.** No hay índice único `(tenant_id, customer_id)`: el intake de la landing
  crea un lead por cada clave de idempotencia, y el borrador automático, el agente y la ficha pueden
  crear otro a la vez (`crear_lead` no toma el bloqueo del cliente). Desde este ADR las tools de lead,
  `completar_lead` y la ficha eligen el mismo lead (el de actividad más reciente, desempatado por id)
  en vez de fallar con dos filas. Un índice único o la fusión de duplicados quedan para otra HU.
- **Gana el último que escribe el estado.** Lo cambian el dueño, el asistente (`calificar_lead` fuerza
  «calificado») y el seguimiento (`calificado → contactado`). El lead no guarda quién escribió; el log
  `lead_pack_editado` registra los cambios desde la ficha.
- **Seguimiento por una edición manual.** Marcar «calificado» a mano mueve `last_activity_at` y deja al
  lead en el seguimiento automático, que puede escribirle al cliente si su conversación sigue abierta en
  la ventana de 24 horas. Es lo que significa «calificado» en ese flujo. Corregir solo el interés no
  mueve la actividad.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Ruta propia del pack (`/pack/commerce/leads/{id}`) | Panel y APK tendrían que conocer rutas de pack; ADR-0056 existe para evitarlo |
| Extender el `PATCH /{id}` del perfil con valores de packs | Rompe su regla de escribir solo por `facts.py` y mezcla dos fuentes en una ruta |
| Tabla nueva de pipeline (reemplazar ADR-0022) | La columna `status` ya existe y la usan las tools; una tabla nueva duplicaría el estado |
| Mostrar el contacto del lead junto al del perfil | Es el «tres sitios» que se quiere cerrar: dos valores para el mismo dato |

## Consecuencias

**Positivas:** el dueño mueve el lead de etapa desde la ficha del panel y de la APK sin que el core
conozca el pack; un solo valor por dato de contacto, con la fuente escrita.

**Negativas:** un hook más en el contrato de ADR-0056; los riesgos de arriba.

**Qué haría que revisáramos esto:** que se necesite historial de etapas o quién movió el lead (tabla de
eventos del lead), o que un segundo pack pida tipos de campo nuevos.
