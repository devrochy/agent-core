## Guardar un borrador con algo retirado avisa en palabras simples (2026-11-04)

- `PUT /api/v1/agents/{id}/draft`: si el borrador nombra una acción o un procedimiento retirado, el 422 trae el mismo aviso en palabras simples que publicar (qué ya no existe y qué hacer) en lugar del mensaje técnico del linter (HU-AOS-59).
