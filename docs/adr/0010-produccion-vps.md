# ADR-0010 — Producción en VPS

- **Estado:** aceptado
- **Fecha:** 2026-08-26

## Contexto

Un cliente que paga no puede correr en la máquina de desarrollo de quien construye el
framework (invariante #9). Coolify en local es solo para desarrollo; producción necesita un
entorno estable, monitoreado y con respaldo probado.

## Decisión

Desde el primer cliente que paga, producción corre en un VPS con backups probados. Restaurar
un backup al menos una vez y documentarlo.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Producción en la máquina de desarrollo | Mezcla entornos; un cambio de dev tumba a clientes |
| Producción en un PaaS gestionado | Costo y dependencia de plataforma; el despliegue multi-tenant lo complica |
| VPS sin backup probado | Un backup que nunca se restauró no es un backup |

## Consecuencias

**Positivas:** entorno estable y aislado; la restauración documentada convierte el backup en
una garantía vendible; la operación es asequible (un VPS de 2 GB corre todo).

**Negativas / lo que aceptamos pagar:** operar un VPS (seguridad, updates, monitoreo);
la restauración de prueba consume tiempo antes de cada entrega grande.

**Qué haría que revisáramos esto:** que dos clientes seguidos requieran infraestructura que un
VPS no cubre — señal para evaluar topología `dedicado` o un proveedor gestionado.

## Impacto en el código

`docker-compose.yml` (variante de producción), scripts de backup/restauración, `docs/security.md`,
y la documentación de despliegue (`docs/INSTALLATION.md`, SDD).