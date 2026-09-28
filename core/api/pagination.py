"""Paginación uniforme de la API `/api/v1` (ADR-0077)."""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

# `Generic[T]` en vez de sintaxis PEP 695 (`Page[T]`): mypy 1.11 aún no la soporta
# (ruff UP046/UP047 quedan exceptuados en pyproject para este archivo).
T = TypeVar("T")

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class PageParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def page_params(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> PageParams:
    return PageParams(page=page, page_size=page_size)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    has_more: bool


def build_page(items: list[T], total: int, params: PageParams) -> Page[T]:
    return Page[T](
        items=items,
        total=total,
        page=params.page,
        page_size=params.page_size,
        has_more=params.page * params.page_size < total,
    )
