"""Unit tests for pagination utilities."""
from __future__ import annotations

import pytest

from pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Page,
    PageMeta,
    paginate,
    paginate_queryset,
)

RECORDS = [{"id": i, "name": f"item-{i}"} for i in range(1, 51)]


def test_paginate_first_page() -> None:
    page = paginate(RECORDS, page=1, page_size=10)
    assert len(page.items) == 10
    assert page.items[0]["id"] == 1
    assert page.meta.page == 1
    assert page.meta.total_count == 50
    assert page.meta.total_pages == 5
    assert page.meta.has_next is True
    assert page.meta.has_previous is False


def test_paginate_last_page() -> None:
    page = paginate(RECORDS, page=5, page_size=10)
    assert len(page.items) == 10
    assert page.items[-1]["id"] == 50
    assert page.meta.has_next is False
    assert page.meta.has_previous is True


def test_paginate_partial_last_page() -> None:
    page = paginate(RECORDS, page=3, page_size=21)
    assert len(page.items) == 8  # 50 - 2*21 = 8
    assert page.meta.total_pages == 3


def test_paginate_empty_sequence() -> None:
    page = paginate([], page=1, page_size=10)
    assert page.items == []
    assert page.meta.total_count == 0
    assert page.meta.total_pages == 1
    assert page.meta.has_next is False
    assert page.meta.has_previous is False


def test_paginate_caps_page_size_at_max() -> None:
    page = paginate(RECORDS, page=1, page_size=9999)
    assert page.meta.page_size == MAX_PAGE_SIZE
    assert len(page.items) == min(MAX_PAGE_SIZE, len(RECORDS))


def test_paginate_raises_on_invalid_page() -> None:
    with pytest.raises(ValueError, match="page must be >= 1"):
        paginate(RECORDS, page=0)


def test_paginate_queryset_ordered_asc() -> None:
    qs = [{"id": 3}, {"id": 1}, {"id": 2}]
    page = paginate_queryset(qs, page=1, page_size=10, order_by="id")
    assert [r["id"] for r in page.items] == [1, 2, 3]


def test_paginate_queryset_ordered_desc() -> None:
    qs = [{"id": 3}, {"id": 1}, {"id": 2}]
    page = paginate_queryset(qs, page=1, page_size=10, order_by="-id")
    assert [r["id"] for r in page.items] == [3, 2, 1]


def test_paginate_returns_page_meta_type() -> None:
    page = paginate(RECORDS, page=1)
    assert isinstance(page, Page)
    assert isinstance(page.meta, PageMeta)
