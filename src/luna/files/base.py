"""OS-neutral types and protocols of the file search (ADR 0011, Feature B)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RootDir:
    """A folder the search may look into. ``label`` is what the LLM sees instead of its path."""

    path: Path
    label: str  # "Documents", "OneDrive", "D:"
    user: bool = False  # a user folder (searched before the drives)


@dataclass(frozen=True)
class FileHit:
    path: Path  # absolute; never sent to the LLM
    is_dir: bool
    size: int | None
    modified: datetime | None
    root_label: str


@dataclass(frozen=True)
class FileQuery:
    tokens: tuple[tuple[str, ...], ...]  # per token: its variants (word, stem, translit, alias)
    extensions: frozenset[str] | None = None  # kind filter (".pdf"); None = any
    dirs_only: bool = False


@dataclass(frozen=True)
class SearchOutcome:
    hits: tuple[FileHit, ...]
    backend: str  # "walk", "index" or "index+walk"
    timed_out: bool  # some folders were not checked (time or entry budget)
    scanned: int = 0
    elapsed_s: float = 0.0
    roots: tuple[RootDir, ...] = ()  # the roots this search was allowed to use


@runtime_checkable
class FileIndex(Protocol):
    """The OS search index (optional; the Windows backend is a later Issue)."""

    def available(self) -> bool:
        """Cached probe. Never raises."""
        ...

    def query(
        self, q: FileQuery, roots: Sequence[Path], *, limit: int, timeout_s: float
    ) -> list[FileHit]:
        """Raises ``ProviderUnavailable`` on failure."""
        ...


@runtime_checkable
class FileOpener(Protocol):
    def open(self, path: Path) -> None:
        """Open with the default app (ShellExecute, no arguments); ``ToolError`` on failure."""
        ...

    def reveal(self, path: Path) -> None:
        """Show the item selected in the file manager; ``ToolError`` on failure."""
        ...
