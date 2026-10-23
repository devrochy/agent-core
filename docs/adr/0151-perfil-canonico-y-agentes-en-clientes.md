# ADR-0151 — Perfil canónico del cliente y agentes en la vista de Clientes

- **Estado:** propuesto (falta que lo acepte el dueño).
- **Fecha:** 2026-10-22
- **Contexto:** SPEC-33 «CRM», tanda 1A. **Enmienda:** ADR-0056 (el `LeadProvider` declara a qué
  agente pertenece) y ADR-0114 (el perfil se lee por un esquema canónico; sigue escribiéndose solo
  por `core/memory/facts.py`). **Usa:** ADR-0022 (el cliente es `Customer`) y ADR-0149 (la ficha
  muestra el contacto del perfil).

## Contexto

La lista de Clientes mostraba una columna plana por cada pack y una ficha genérica de clave/valor.
El dueño pidió ver primero los datos base del cliente (avatar, nombre o alias, canal, clasificación,
teléfono, ubicación) y, después, la data que cada agente del negocio construyó en su área: Ventas,
Cobranza, Agenda, Suscripciones o agentes personalizados. También pidió editar esos datos desde el
panel sin que el cliente se duplique entre agentes.

Hoy el `LeadProvider` (ADR-0056) agrupa columnas por **pack**, no por agente, y los datos de contacto
se escriben con claves distintas según el camino: `name`/`email`/`phone` en las tools de lead,
`full_name`/`contact_phone` en el checkout de pack_commerce, `cedula`/`direccion`/`city` en datos
viejos. La ficha los mostraba como una lista sin estructura.

## Decisión

1. **Perfil canónico en `core/memory/perfil.py`.** Claves únicas: `name`, `apellido`, `alias`,
   `avatar_url`, `phone`, `pais_telefono`, `email`, `tipo_documento`, `id_document`, `address`,
   `municipio`, `departamento`, `pais`, `latitud`, `longitud` y `clasificacion`. El módulo:
   - lee los datos viejos por alias (`full_name` → `name`, `contact_phone` → `phone`, `cedula` →
     `id_document`, `city` → `municipio`, `lat`/`lng`…), sin migración;
   - separa el teléfono en E.164 con un catálogo pequeño de países (`core/shared/telefono.py`);
   - calcula el nombre visible (nombre + apellido; si no hay, alias; si no, identificador del canal);
   - valida lo que llega del panel (correo, teléfono, tipo/número de documento, coordenadas, opciones)
     y genera la URL de OpenStreetMap con coordenadas o con la dirección.
2. **La fuente sigue siendo `memory_facts`.** No hay tabla ni migración nueva. `PATCH
   /admin/leads/{id}/perfil` valida y escribe por `facts.escribir_hechos(..., source="human")`; el
   `PATCH /{id}` viejo se mantiene para compatibilidad. `otros` permite seguir guardando un dato libre
   con las mismas reglas de `facts.py` (nada del sistema, nada sensible).
3. **El `LeadProvider` declara su agente.** `register(..., agent_key, agent_name, icon)` añade metadatos
   sin romper a los packs que no los pasan (la clave cae al `plugin_id`). La API agrupa las columnas
   por agente y devuelve `agents` (metadatos) y, por fila, `agentes` (claves con data). El panel dibuja
   un icono por agente y abre la ficha en esa pestaña.
4. **Los agentes son los publicados por el negocio.** La API lee `agents` (`AgentRecord`) y usa el
   nombre/`key` reales; si el negocio es legacy o no tiene filas, cae a los `agent_key` de los packs.
   El General se muestra siempre y es la pestaña de los datos base; no se cambia el runtime: el
   Coordinador sigue orquestando (D-35).
5. **La API es aditiva.** `LeadRow` y `LeadDetail` suman `perfil`, `agentes` y `campos_perfil`; se
   conservan `cliente`, `canal`, `telefono`, `extras` y `columns` para la APK y los clientes viejos.
   El CSV suma las columnas base (nombre, apellido, alias, clasificación, teléfono, documento,
   dirección, ubicación, agentes) antes de las columnas de los packs.
6. **El alias del canal se conserva.** Cuando WhatsApp manda el `profile.name`, el consumidor lo
   guarda como hecho `perfil.alias` si todavía no hay alias; si después el agente aprende el nombre
   real, `name` cambia y el alias queda.

## Riesgos asumidos

- **La data de un pack es del área, no de una instancia concreta.** Si dos agentes comparten un pack
  (dos agentes de Ventas), la vista los agrupa por área y ambos ven la misma data. La atribución fina
  ya existe en `agent_record_origins`; usarla es una tanda posterior.
- **Datos viejos con varias claves.** La lectura prefiere la clave canónica; si un cliente tiene
  `name` y `full_name` a la vez, gana `name`. No se reescriben los hechos viejos.
- **PII.** Documento, dirección y geolocalización son datos personales: la API los devuelve solo con
  la sección `leads`, el CSV los incluye y la purga del cliente los borra. La geocodificación (tanda
  1B) enviará solo la dirección al proveedor.
- **El panel es la única escritura nueva del perfil.** Las tools y el checkout siguen escribiendo por
  `facts.py`; las claves canónicas se normalizan también en `update_customer_profile`.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Columnas nuevas en `customers` para cada dato | Duplica la fuente de `memory_facts` y rompe la regla del único escritor |
| Una tabla de perfiles por agente | Duplica nombre/teléfono/dirección y crea valores contradictorios |
| Dejar el `LeadProvider` solo por pack | El panel no puede agrupar por agente ni mostrar los iconos que pidió el dueño |
| Cambiar el runtime para que el General orqueste | Es una decisión del Agent OS (SPEC-26), no de la vista de Clientes |

## Consecuencias

**Positivas:** una sola fuente de contacto, una ficha legible por área y una API aditiva que no
rompe la APK. El panel puede crecer por tandas (Ventas, Cobranza, Agenda) sin rehacer el contrato.

**Negativas:** un módulo de perfil más que mantener; la agrupación por pack no distingue dos agentes
del mismo pack; el CSV crece.

**Qué haría que revisáramos esto:** que el dueño necesite atribución exacta por instancia de agente,
que un dato de contacto tenga que vivir en el pack por requisito legal, o que se agregue un tipo de
campo que no quepa en el esquema canónico.
