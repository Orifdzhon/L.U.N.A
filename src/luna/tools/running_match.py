"""Resolve a spoken app name to a *running* app (ADR 0011, feature A).

Pure logic over data (no Win32). Two steps, window titles are never used (a browser tab titled
"Telegram" must not make "закрой телеграм" close the browser):

1. spoken name -> catalog entry (the ``open_app`` matcher with ``verbs="close"``) -> the running
   app whose identity equals the entry (``identity_matches``: AUMID / exe path / known folder);
2. otherwise the same strict matcher over the running apps themselves, named by their catalog
   name (if mapped), their exe description and their exe stem.
"""

from __future__ import annotations

import ntpath
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from luna.platform.base import AppEntry, RunningApp
from luna.tools.app_matching import (
    DEFAULT_MARGIN,
    DEFAULT_THRESHOLD,
    MatchStatus,
    match_app,
)

Identity = Callable[[RunningApp, AppEntry], bool]


@dataclass(frozen=True)
class RunningMatch:
    status: MatchStatus
    app: RunningApp | None = None
    candidates: tuple[RunningApp, ...] = ()  # for AMBIGUOUS
    entry: AppEntry | None = None  # catalog entry that was recognised (maybe not running)
    names: Mapping[str, str] | None = None  # RunningApp.key -> display name


def display_names(
    running: Sequence[RunningApp], catalog: Sequence[AppEntry], identity: Identity
) -> dict[str, str]:
    """Catalog name if the running app maps to a catalog entry, else its own name."""
    names: dict[str, str] = {}
    for app in running:
        names[app.key] = next((e.name for e in catalog if identity(app, e)), app.name)
    return names


def _stem(app: RunningApp) -> str:
    return ntpath.splitext(ntpath.basename(app.exe_path))[0] if app.exe_path else ""


def match_running(
    name: str,
    running: Sequence[RunningApp],
    catalog: Sequence[AppEntry],
    aliases: Mapping[str, tuple[str, ...]] | None,
    identity: Identity,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    margin: int = DEFAULT_MARGIN,
) -> RunningMatch:
    names = display_names(running, catalog, identity)
    by_key = {a.key: a for a in running}

    def done(status: MatchStatus, **kw: object) -> RunningMatch:
        return RunningMatch(status, names=names, **kw)  # type: ignore[arg-type]

    first = match_app(name, catalog, aliases, threshold=threshold, margin=margin, verbs="close")
    if first.status is MatchStatus.UNSUPPORTED:
        return done(MatchStatus.UNSUPPORTED)

    # Step 1: catalog entries (one, or the ambiguous candidates) that are running.
    entries = first.candidates if first.status is not MatchStatus.NONE else ()
    hits: list[RunningApp] = []
    for entry in entries:
        hits.extend(a for a in running if identity(a, entry) and a not in hits)
    if len(hits) == 1:
        return done(MatchStatus.MATCH, app=hits[0], entry=first.entry)
    if len(hits) > 1:
        return done(MatchStatus.AMBIGUOUS, candidates=tuple(hits[:5]))

    # Step 2: the running apps by their own names. A recognised catalog entry is searched by
    # its display name (so "хром" -> "Google Chrome" finds chrome.exe by description).
    query = first.entry.name if first.entry is not None else name
    synthetic = [
        AppEntry(names[a.key], a.key, tuple(sorted(x for x in {a.name, _stem(a)} if x)))
        for a in running
    ]
    second = match_app(query, synthetic, aliases, threshold=threshold, margin=margin, verbs="close")
    if second.status is MatchStatus.MATCH and second.entry is not None:
        return done(MatchStatus.MATCH, app=by_key[second.entry.app_id], entry=first.entry)
    if second.status is MatchStatus.AMBIGUOUS:
        return done(
            MatchStatus.AMBIGUOUS,
            candidates=tuple(by_key[e.app_id] for e in second.candidates),
        )
    return done(MatchStatus.NONE, entry=first.entry)
