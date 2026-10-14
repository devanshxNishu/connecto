"""Where people come from: the YC directory, CSV files, web pages and LinkedIn's own search.

    reports = fetch_sources(home, cfg, store)          # run every enabled source, write people into the store
    wanted = make_filter(cfg)                          # later: is this stored person still wanted by the current config?

A *source* (``base.Source``) finds people and hands them to a ``Sink``; ``accepts(row)`` answers who may be sent to, so
changing a filter in the config takes effect on people already stored, without fetching anything again.
"""
from __future__ import annotations

from typing import Callable

from ..browser import DEFAULT_BASE_URL, LoggedOut, NeedsAttention
from ..config import enabled_sources
from ..limits import limits_from_config
from .base import FetchReport, Sink, Source, SourceContext
from .csv_file import CSVSource, read_csv_rows
from .linkedin_search import LinkedInSearchSource
from .webpage import WebpageSource, extract_people, safe_http_url
from .yc import YCSource

REGISTRY: dict[str, type[Source]] = {
    "yc": YCSource,
    "csv": CSVSource,
    "webpage": WebpageSource,
    "linkedin_search": LinkedInSearchSource,
}

__all__ = [
    "REGISTRY", "FetchReport", "Sink", "Source", "SourceContext", "build_source", "make_filter", "fetch_sources",
    "read_csv_rows", "extract_people", "safe_http_url",
]


def build_source(spec: dict, ctx: SourceContext) -> Source:
    """Instantiate the source a config entry describes (cheap: no network, no disk). ValueError for an unknown type."""
    typ = str((spec or {}).get("type") or "").strip().lower()
    cls = REGISTRY.get(typ)
    if cls is None:
        raise ValueError(f"unknown source type {typ!r} (expected one of {', '.join(REGISTRY)})")
    return cls(spec, ctx)


def make_filter(cfg: dict) -> Callable[[dict], bool]:
    """Build once, call per person: True when the stored person is still wanted.

    Wanted = added by hand (``source_type == "manual"``), or accepted by an ENABLED source of the person's own type that
    still owns the row. People found by a source that was since removed or disabled are not wanted.
    """
    ctx = SourceContext(home=None, cfg=cfg, store=None, limits=None)       # type: ignore[arg-type]  # sources never touch these when built
    by_type: dict[str, list[Source]] = {}
    for spec in enabled_sources(cfg):
        try:
            src = build_source(spec, ctx)
        except Exception:  # noqa: BLE001 - a malformed entry must not make everyone unwanted... or wanted
            continue
        by_type.setdefault(src.type, []).append(src)

    def wanted(row: dict) -> bool:
        source_type = str(row.get("source_type") or "")
        if source_type == "manual":
            return True
        return any(src.accepts(row) for src in by_type.get(source_type, ()))

    return wanted


def fetch_sources(home, cfg: dict, store, *, only: str | None = None, limit: int = 0, refresh: bool = False,
                  skip_browser: bool = False, log: Callable[[str], None] = print, base_url: str = DEFAULT_BASE_URL,
                  headless: bool | None = None, stop: Callable[[], bool] = lambda: False, page=None,
                  reports_out: list | None = None) -> list[FetchReport]:
    """Run every enabled source (or only those of type ``only``) and return one report each.

    A source that fails is recorded in its report and the next one still runs. ``NeedsAttention`` / ``LoggedOut`` are
    recorded and then re-raised - LinkedIn wants the person, so the caller must stop - with the reports so far attached
    as ``exc.reports`` (and appended to ``reports_out`` if one is given).
    """
    reports: list[FetchReport] = reports_out if reports_out is not None else []
    ctx = SourceContext(home=home, cfg=cfg, store=store, limits=limits_from_config(cfg), log=log, limit=limit,
                        refresh=refresh, base_url=base_url, headless=headless, page=page, stop=stop)
    for spec in enabled_sources(cfg):
        typ = str(spec.get("type") or "").strip().lower()
        if only and typ != only:
            continue
        if stop():
            break
        try:
            source = build_source(spec, ctx)
        except Exception as exc:  # noqa: BLE001
            rep = FetchReport(source=typ or "?")
            rep.errors.append(str(exc))
            reports.append(rep)
            continue
        if skip_browser and source.needs_browser:
            log(f"{source.source_id}: skipped (needs a browser)")
            continue
        report = FetchReport(source=source.source_id)
        reports.append(report)
        sink = Sink(store, source.type, source.source_id, report, log)
        try:
            source.fetch(sink)
        except (NeedsAttention, LoggedOut) as exc:
            report.errors.append(f"{type(exc).__name__}: {exc}")
            exc.reports = list(reports)                                    # type: ignore[attr-defined]
            raise
        except Exception as exc:  # noqa: BLE001 - one broken source must not stop the others
            first = (str(exc).splitlines() or [""])[0]
            report.errors.append(f"{type(exc).__name__}: {first}" if first else type(exc).__name__)
            log(f"{source.source_id}: failed - {report.errors[-1]}")
    return reports
