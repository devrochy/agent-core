## Conocimiento: un original que el worker no encuentra falla como `interno`

- Si el worker no encuentra el archivo subido (detalle `archivo_ausente`), el documento queda `fallido`
  con el código `interno` y ya no con `formato`. El panel dejaba de decir «El archivo está dañado o no
  es del tipo que dice ser», que era falso: el archivo del dueño estaba bien y el fallo era nuestro
  (en test, el worker y el beat no montaban el volumen de la API).
- `error_detalle` y el log `knowledge_documento_fallido` siguen diciendo `archivo_ausente`.
