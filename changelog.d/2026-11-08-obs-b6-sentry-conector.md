## Observabilidad B6: Sentry en el conector de WhatsApp Web (2026-11-08)

- `core/waweb/connector.py:main()` llama a `configure_sentry()` (no-op sin `SENTRY_ENABLED`/`SENTRY_DSN`), con el mismo scrubber que la API y el worker, y marca los eventos con `role=connector`. Antes, los errores del conector solo quedaban en el log del contenedor.
- El scrubber de Sentry también tapa las variables locales del conector que llevan datos del cliente o el código de vinculación: `event`, `qr`, `job`, `text`, `sender`, `jid`, `to`, `raw`.
