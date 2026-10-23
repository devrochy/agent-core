## Clientes: perfil base y agentes en la ficha (tanda 1A, ADR-0153)

- `core/memory/perfil.py` define el perfil canónico del cliente (`name`, `apellido`, `alias`,
  teléfono en E.164, correo, documento, dirección, municipio, departamento, país, coordenadas y
  clasificación), lee los datos viejos por alias (`full_name`, `contact_phone`, `cedula`, `city`…) y
  valida lo que llega del panel sin migración.
- `PATCH /api/v1/admin/leads/{id}/perfil` escribe por `core/memory/facts.py` con `source="human"`;
  `otros` conserva la posibilidad de guardar un dato libre con las mismas reglas. El `PATCH /{id}`
  actual sigue disponible.
- `LeadProvider` suma `agent_key`, `agent_name` e `icon` (enmienda de ADR-0056). La API devuelve
  `agents`, `agentes` y `campos_perfil` de forma aditiva; los packs de Ventas, Cobranza y Agenda ya
  declaran su área.
- El CSV de Clientes suma nombre, apellido, alias, clasificación, teléfono con indicativo, correo,
  documento, dirección, ubicación, coordenadas y agentes antes de las columnas de los packs.
- El consumidor guarda el `profile.name` de WhatsApp como alias cuando no hay uno; el nombre real
  sigue mandando. `crear_pedido` escribe el perfil con las claves canónicas.
- Panel: tabla con avatar/iniciales, nombre o alias, icono de canal, clasificación, teléfono,
  ubicación, iconos por agente y acciones; ficha con pestañas General y de cada agente, edición
  estructurada y «otros datos».
- Tests: unitarios de perfil/teléfono, integración de API (perfil, agentes, CSV, aislamiento) y
  helpers del panel. ADR-0153.

## Geocodificación de la dirección (ADR-0154)

- `core/geocoding` con LocationIQ detrás de `GEOCODING_PROVIDER`; la API key es de la plataforma.
- `POST /api/v1/admin/leads/{id}/geocodificar` guarda latitud, longitud, dirección normalizada,
  precisión, proveedor y fecha como hechos `perfil` con `source="human"`.
- Solo se envía la dirección compuesta al proveedor; los logs no llevan dirección. Topes por negocio
  y global en Redis, con 503 si no se puede verificar la cuota.
- El panel geocodifica al guardar una dirección cambiada (best-effort) y ofrece «Geocodificar
  dirección» / «Recalcular ubicación».

## Ventas, Cobranza y Agenda en la ficha (tandas 2-4)

- pack_commerce suma pedidos, total comprado, ticket promedio, último pedido y cotizaciones abiertas.
- pack_cobranza suma cartera, saldo, mora, riesgo, estatus, cuotas vencidas, próxima cuota, promesa y
  último pago; riesgo, tope de deuda y estatus se editan desde la ficha con auditoría.
- pack_calendar suma próxima/última cita, citas, canceladas, no asistió y tipo frecuente.
- La vista previa de plantilla acepta `lead_id` y el panel muestra el icono de enviar plantilla en las
  acciones del cliente (solo dueño/master). La promoción por segmento pasa a Comercio › Promociones.
