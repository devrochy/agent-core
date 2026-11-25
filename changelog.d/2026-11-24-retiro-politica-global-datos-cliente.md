## Retiro de la política global de datos del cliente (HU-CRM-28, ADR-0198 §5) (2026-11-24)

- `GET` y `PUT /api/v1/admin/datos-cliente/politica` responden **410** `politica_por_agente`: la política de datos del
  cliente es de cada agente (`/admin/agents/{id}/datos-cliente`). Ni el panel ni la APK las llamaban, y en test no
  hubo llamadas. El aviso de tratamiento de datos (`/admin/datos-cliente/aviso`) sigue igual.
- `docs/openapi.json` regenerado: además del retiro, recoge rutas que ya estaban en el código y faltaban en el archivo.
