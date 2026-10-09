## Las tools declarativas ya no llegan a la red interna (2026-10-08)

- Seguridad (SSRF, severidad alta, hallazgo del `auditor-multitenant`): un admin del tenant podía apuntar una
  tool declarativa (ADR-0052) a hosts internos que el chequeo de `ScopedHttp` dejaba pasar, y la respuesta volvía
  al chat: nombres de Docker de una etiqueta (`redis`, `postgres`, `api`), formas numéricas que glibc resuelve
  (`2130706433`, `127.1`, `0x7f.1`) y dominios que resuelven a IPs privadas o de metadata
  (`169.254.169.254.nip.io`).
- `ScopedHttp.request` ahora rechaza esos hosts sin resolver, resuelve el DNS y rechaza si alguna IP es privada,
  loopback, link-local, reservada, multicast, sin especificar, CGNAT (`100.64/10`) o IPv4 embebida en IPv6, y
  conecta a la IP validada con `Host` y SNI del dominio (el certificado se sigue verificando contra el dominio;
  sin DNS rebinding). Sin redirects ni proxies del entorno. Un dominio que no resuelve da el mismo error que uno
  interno (no deja enumerar nombres de Docker/Coolify), y si una IP no conecta se prueba la siguiente (IPv4 primero,
  connect de 5 s por intento).
- Las tools declarativas solo guardan `allowed_domains` que sean FQDN públicos; el admin avisa cuáles rechazó.
  Las ya guardadas con dominios internos quedan sin allowlist (fail-closed). Los packs (`plugin_http`, p. ej.
  pack-calendar hacia `www.googleapis.com`) siguen igual.
