## Arranque de la API sin reinstalar los packs (2026-11-20)

- La imagen deja el sello `/app/.packs-instalados` (sha256 del `pyproject.toml` de cada pack) y el
  entrypoint se salta los packs que ya trae. Solo instala un pack nuevo o con el `pyproject`
  cambiado en un bind-mount de compose. El arranque de la API baja de ~41 s a ~18 s medido en
  local; en producción eran 48 s. Es el tiempo de 502 en cada despliegue.
- El entrypoint escribe la duración de cada etapa (`[entrypoint] etapa …: N s`).
- `docs/runbooks/despliegue-api-sin-corte.md`: propuesta, sin ejecutar, de rolling update de la
  API con Traefik de Coolify y health check en lugar del puerto fijo `3003`.
