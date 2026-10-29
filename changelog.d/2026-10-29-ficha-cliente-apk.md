## Ficha del cliente en la APK (2026-10-29)

- Clasificación general: nueva opción `Nuevo` en el perfil del cliente.
- Avatar del cliente: `POST/DELETE /admin/leads/{lead_id}/avatar`, con almacenamiento por negocio y URL firmada en la ficha.
- Geocodificación inversa: `POST /admin/leads/{lead_id}/geocodificar/reverso` propone dirección, municipio, departamento y país desde un punto del mapa, sin escribir el perfil.
