## Registro de opciones: el Coordinador y las banderas del Agent OS (HU-CFG-07, 2026-10-25)

- `GET /api/v1/admin/settings/catalog` suma 19 opciones del Coordinador (`KernelSpec`, grupo `coordinador`; 3 topes de
  costo solo master) y las 3 banderas del Agent OS (grupo `agent_os`, solo master). Rangos y defectos salen del
  contrato `kernelspec/1`; el valor sigue donde vivía y solo se describe. Cada opción trae `ambito`
  (`negocio` | `conexion`), derivado del almacén. Los clientes actuales no cambian (ignoran grupos que no conocen).
- Prueba de consistencia: toda hoja del `KernelSpec` está registrada o excluida con motivo (voz, ids de modelo,
  playbooks, reglas de enrutamiento…). Adenda en ADR-0166.
