## Clientes: ficha, pausa del agente por cliente, filtros y CSV (HU-UX-16, parte backend)

- `POST /api/v1/admin/leads/{id}/toggle-bot` pausa o reanuda el agente para un cliente en todas sus
  conversaciones (`Customer.bot_paused`, que el kernel ya respetaba y no tenía cómo cambiarse).
- `GET /api/v1/admin/leads/{id}`: ficha con los datos del canal, sus conexiones, la última
  conversación y lo que se sabe del cliente (`memory_facts` vigentes con origen y fecha).
- `PATCH /api/v1/admin/leads/{id}` corrige datos del perfil. Escribe solo por `core/memory/facts.py`
  con `source="human"` (el agente los ve desde la siguiente conversación); las claves del sistema
  (`crm_lead_id`, `optin`, `source`) no se pueden editar.
- La lista filtra por `channel`, `connection_id` y por los filtros que declaran los packs
  (`filtro=clave:valor`), devuelve `telefono`, `bot_paused` y `filters`, y busca también por teléfono.
- `GET /api/v1/admin/leads/export.csv`: la lista filtrada en CSV (hasta 5000 filas), con las celdas que
  empiezan por `=`, `+`, `-` o `@` neutralizadas.
- `LeadRegistry` acepta `filters` + `filter_query` (ADR-0056): el pack devuelve un `SELECT` de
  `customer_id` y el core lo aplica sin conocer sus tablas.
- pack_commerce registra por fin su `LeadProvider`: clasificación del cliente (nuevo, ocasional,
  frecuente, fiel, inactivo; la regla de `classify_customer`, calculada en SQL para toda la página) y
  estado, puntaje e interés de su último lead, con filtros por clasificación y por estado del lead.
