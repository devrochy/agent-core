## Paquetes stub borrados (2026-11-22, HU-AOS-91, parte A)

- Se borran los stubs sin código `apps/{agentos,gateway,worker}` y `packages/{core-crm,core-runtime,channel-whatsapp}` (ADR-0206 §10). Nada los importaba. El glob `packages/channel-*/` del `Dockerfile` y de `install_plugins.sh` se queda: sigue instalando telegram, twilio, web y waweb.
