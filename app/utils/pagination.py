"""Offset-based pagination utilities for API responses."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Sequence, TypeVar

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass
class PageMeta:
    """Metadata for a paginated response."""

    page: int
    page_size: int
    total_count: int
    total_pages: int
    has_next: bool
    has_previous: bool


@dataclass
class Page(Generic[T]):
    """A single page of results with metadata."""

    items: list[T]
    meta: PageMeta


def paginate(
    items: Sequence[T],
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Page[T]:
    """Slice a sequence into a paginated Page.

    Args:
        items: The full ordered sequence to paginate.
        page: 1-based page number.
        page_size: Number of items per page (capped at MAX_PAGE_SIZE).

    Returns:
        Page containing the requested slice and full metadata.

    Raises:
        ValueError: If page < 1 or page_size < 1.
    """
    if page < 1:
        raise ValueError(f"page must be >= 1, got {page}")
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)

    total_count = len(items)
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    offset = (page - 1) * page_size
    slice_ = list(items[offset : offset + page_size])

    meta = PageMeta(
        page=page,
        page_size=page_size,
        total_count=total_count,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )
    return Page(items=slice_, meta=meta)


def paginate_queryset(
    queryset: list[dict],
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    order_by: str = "id",
) -> Page[dict]:
    """Paginate a list of dicts with optional ordering.

    Args:
        queryset: List of dict records (simulates DB queryset).
        page: 1-based page number.
        page_size: Number of items per page.
        order_by: Key to sort by (prefix with '-' for descending).

    Returns:
        Page of dict records.
    """
    reverse = order_by.startswith("-")
    key = order_by.lstrip("-")
    sorted_qs = sorted(queryset, key=lambda r: r.get(key, ""), reverse=reverse)
    return paginate(sorted_qs, page=page, page_size=page_size)
