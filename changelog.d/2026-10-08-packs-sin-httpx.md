## Comercio y pagos salen a red solo por el cliente acotado del core (2026-10-08)

- `pack-payments` dejó de usar `httpx` directo (ADR-0052). `WompiProvider` recibe un `ScopedHttp`. La tool
  `generar_link_pago` le pasa su `context.http`. "Probar y guardar" del panel y la capacidad de pagos que usa cobranza
  no tienen `ToolContext`, así que usan `plugin_http` con la allowlist del pack: `production.wompi.co` y
  `sandbox.wompi.co`. `checkout.wompi.co` sigue en el manifest, pero solo lo abre el navegador del pagador.
- `pack-commerce` valida Turnstile del intake de leads por `plugin_http` (`challenges.cloudflare.com`, ahora también
  en `network` del manifest). Mismo timeout de 8 s, y sigue fail-closed: un fallo de red o una respuesta
  inesperada rechaza el lead.
- Sin cambios de comportamiento en Wompi: timeout de 15 s; un 4xx/5xx sigue siendo `PaymentRequestRejected` (recuperable,
  sin `auto_disable`); un fallo de red es `PluginError`, igual que antes la excepción de `httpx` envuelta por el host. El
  mensaje de rechazo ahora muestra el cuerpo como JSON re-serializado (antes era el texto crudo). Un 2xx sin JSON ahora es
  `PaymentRequestRejected("sin id")`, antes era una excepción de parseo. Si la red falla cuando comercio pide un link
  (capacidad de pagos), el error ahora es de `pack_payments` y el auto-disable apaga los pagos del tenant, no todo
  `pack_commerce`.
- El test AST que impedía importar `httpx` en la agenda ahora cubre todos los `packages/pack-*/**/*.py` y también
  `requests`, `aiohttp`, `urllib3`, `urllib.request` y `http.client`. Además verifica en cada pack que la
  `network_allowlist` esté declarada en `network` del manifest, y que cada `plugin_http(X)` reciba una constante igual
  a esa allowlist (`tests/test_packs_salida_a_red.py`).
- `pack-payments` ya no declara `httpx` como dependencia.
