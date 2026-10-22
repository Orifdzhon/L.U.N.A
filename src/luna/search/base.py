"""Web search provider protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    content: str | None = None  # extracted page text (truncated), filled by the fetch step


@runtime_checkable
class SearchProvider(Protocol):
    name: str

    def search(
        self, query: str, *, lang: str, max_results: int = 5, timeout_s: float = 5.0
    ) -> list[SearchResult]: ...
