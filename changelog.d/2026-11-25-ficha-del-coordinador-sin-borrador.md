## La ficha del Coordinador abre aunque no tenga borrador (HU-AOS-98) (2026-11-25)

- `GET /api/v1/admin/agents/{id}` respondía 404 para un Coordinador sin borrador (8 de 11 en test), y el panel lo mostraba
  como página inexistente: no se podía entrar a Personalizar. Para un agente `core` sin borrador devuelve ahora un borrador
  vacío (`content: {}`, `lock_version: 0`), sin escribir nada. Un especialista sin borrador sigue dando 404. El contrato
  no cambia.
