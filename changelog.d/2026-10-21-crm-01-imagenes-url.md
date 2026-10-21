## Comercio: foto del producto por URL (HU-CRM-01)

- `GET /api/v1/pack/commerce/products` devuelve `image_url` y `POST` la acepta: solo un link
  `https://` de hasta 2048 caracteres (otro esquema da 422). `null` no la cambia y `""` la quita.
- El import CSV de productos lee la columna opcional `image_url`; una URL que no sea https se ignora
  y el producto se crea sin foto.
- `consultar_inventario` le pasa la foto al core (`_image_url`, que el LLM no ve) solo con un único
  resultado pedido por SKU o por el nombre exacto del producto, como en agent-sale. Una lista, una
  búsqueda por texto o un producto sin foto no la llevan. Por WhatsApp sale como imagen en la última
  burbuja. Cada resultado trae `tiene_foto`.
