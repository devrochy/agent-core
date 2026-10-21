## Centro de pruebas: subsecciones y evolución por versión (HU-QA-08, catálogo v15)

- `qa_items.subseccion` (migración 0101): tercer nivel dentro del área; las pruebas del catálogo v15
  dejan de traer `grupo` (sigue obligatorio en `test`, `prod` y `datos`). El parser acepta catálogos
  anteriores sin cambios.
- Informe de ejecución en formato 2: `secciones` con las pruebas agrupadas por entorno, área y
  subsección y el conteo de cada nivel (OK, Falla, Bloqueada, Sin correr); el Markdown descargable
  incluye la tabla «Resultados por sección».
- `GET /api/v1/admin/qa/evolution`: línea de tiempo de versiones del catálogo con el resultado
  consolidado por sección del ciclo más reciente que usó cada versión.
