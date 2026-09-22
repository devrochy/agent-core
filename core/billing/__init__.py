"""Planes, cuotas y facturación de tenants (ADR-0045 / ADR-0046).

Importar submódulos explícitamente (`core.billing.plans`, `core.billing.metrics`,
`core.billing.enforcement`) — este `__init__` no reexporta para no arrastrar
imports pesados de BD a cualquier import accidental.
"""
