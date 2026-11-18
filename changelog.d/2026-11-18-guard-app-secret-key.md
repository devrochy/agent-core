## Guard de arranque de APP_SECRET_KEY (2026-11-18, salto a producción)

- La API, el worker y el beat no arrancan en producción (`APP_ENV=production`, también test) si `APP_SECRET_KEY` es la de ejemplo (`change-me`), está vacía o tiene menos de 32 caracteres. Se genera con `openssl rand -hex 32`. Prod y test ya tienen claves de 64 caracteres (medido 2026-11-18).
