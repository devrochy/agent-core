## Foto de producto subida al almacenamiento propio (HU-CRM-05)

- `pack_commerce_products.image_key` (migración `crm_0013`): la foto del producto se sube
  desde el panel o la APK al **controlador único** (`core/almacenamiento`, ADR-0085
  enmendado). `image_url` sigue siendo el link externo (HU-CRM-01); si hay `image_key`, gana
  y la API, la APK y el agente devuelven una **URL firmada** (60 min, suficiente para que Meta
  la descargue al enviar).
- `POST /pack/commerce/products/image` (multipart) sube la imagen y devuelve `image_key` + URL.
  El alta/edición acepta `image_key` (`""` = quita la subida).
