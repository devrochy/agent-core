## Tests del catálogo de opciones sin conteos fijos (2026-10-28)

- `test_cfg02_catalogo_api` y `test_cfg02_registro_opciones` calculan cuántas opciones esperan a partir de `OPCIONES` en vez de literales (57/43, 27+16, 32+19+6). Cada opción nueva del registro los rompía y dejó `develop` en rojo tres veces, con una vuelta de CI de ~20 min cada una.
