# HU-009 · Importar datos de una BD existente

**Como** developer, **quiero** que al instalar un plugin pueda mapear las tablas de
una BD PostgreSQL existente a las entidades del plugin, **para** darle vida al módulo
con datos reales del negocio.

## Criterios de aceptación

- [ ] Durante `plugin install`, opción "Importar desde una BD PostgreSQL existente".
- [ ] Pide la URL de conexión, introspecciona el schema (SQLAlchemy inspector) y
      lista las tablas encontradas.
- [ ] Por cada entidad del plugin (declaradas en `[tool.agent_core.plugin.entities]`
      del pyproject.toml), pregunta qué tabla origen corresponde y mapea columna
      por columna (con sugerencia automática por nombre igual).
- [ ] Genera un script SQL (`INSERT INTO ... SELECT`) en
      `plugins/<mod>/import_<fecha>.sql` — NUNCA ejecuta la importación
      automáticamente: el usuario revisa y corre el script.
- [ ] Conexión fallida o BD vacía → mensaje claro, la instalación continúa con
      esquema base.
