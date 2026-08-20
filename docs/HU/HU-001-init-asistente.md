# HU-001 · Inicializar un asistente nuevo

**Como** developer, **quiero** clonar el core y correr un solo comando interactivo,
**para** tener un asistente configurado con documentación generada en minutos.

## Criterios de aceptación

- [ ] `agent-core init` pregunta: nombre, negocio, idioma, tono, LLM (DeepSeek default),
      API key, embeddings, velocidad de respuesta, BD/Redis, admin.
- [ ] Genera `.env` con `MASTER_ENCRYPTION_KEY` (Fernet), `APP_SECRET_KEY` y
      `WHATSAPP_VERIFY_TOKEN` auto-generados — nunca placeholders inseguros.
- [ ] Genera `sdd/*.md` (project, architecture, entities, skills, channels,
      skills-config, changelog) poblados con las respuestas.
- [ ] Aplica migraciones y crea el admin + fila `app_settings` (idempotente).
- [ ] Si la BD no está disponible: no falla; indica los comandos de recuperación
      (`agent-core db migrate && agent-core db seed`).
- [ ] Al terminar imprime URL del panel y credenciales (contraseña generada visible
      UNA sola vez).

## Flujo técnico

`cli/commands/init.py` → wizard questionary → `.env` → `cli/sdd_generator.py`
(estado en `sdd/.state.json` + render de plantillas) → alembic + seed.
