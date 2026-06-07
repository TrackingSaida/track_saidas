"""Logging estruturado para busca de endereços."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ProviderSearchStats:
    provider: str
    results: int = 0
    best_score: Optional[int] = None
    error: Optional[str] = None
    skipped: bool = False


@dataclass
class GooglePlacesSearchStats:
    called: bool = False
    reason: str = ""
    http_status: Optional[int] = None
    results: int = 0
    first_result: Optional[str] = None
    error: Optional[str] = None
    cost_guard_hit: bool = False


@dataclass
class AddressSearchReport:
    query: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    cached: bool = False
    providers: List[ProviderSearchStats] = field(default_factory=list)
    google: GooglePlacesSearchStats = field(default_factory=GooglePlacesSearchStats)
    final_provider: Optional[str] = None
    final_results: int = 0
    best_score: int = 0
    providers_timed_out: bool = False
    skip_external_providers: bool = False


def _quote(value: str) -> str:
    escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def format_address_search_log(report: AddressSearchReport) -> str:
    lines = ["[address-search]", f"query={_quote(report.query)}"]

    if report.latitude is not None:
        lines.append(f"latitude={report.latitude}")
    if report.longitude is not None:
        lines.append(f"longitude={report.longitude}")
    if report.cached:
        lines.append("cached=true")
    if report.providers_timed_out:
        lines.append("providers_timed_out=true")
    if report.skip_external_providers:
        lines.append("skip_external_providers=true")

    lines.append("")
    for p in report.providers:
        lines.append(f"provider={p.provider}")
        if p.skipped:
            lines.append("skipped=true")
        else:
            lines.append(f"results={p.results}")
            if p.best_score is not None:
                lines.append(f"best_score={p.best_score}")
            if p.error:
                lines.append(f"error={_quote(p.error)}")
        lines.append("")

    g = report.google
    lines.append(f"google_places_called={'true' if g.called else 'false'}")
    if g.reason:
        lines.append(f"reason={_quote(g.reason)}")
    if g.cost_guard_hit:
        lines.append("google_places_cost_guard_hit=true")
    if g.called or g.http_status is not None:
        if g.http_status is not None:
            lines.append(f"google_places_status={g.http_status}")
        lines.append(f"google_places_results={g.results}")
        if g.first_result:
            lines.append(f"google_first_result={_quote(g.first_result)}")
        if g.error:
            lines.append(f"google_places_error={_quote(g.error)}")

    lines.append("")
    if report.final_provider:
        lines.append(f"final_provider={report.final_provider}")
    lines.append(f"final_results={report.final_results}")
    lines.append(f"best_score={report.best_score}")

    return "\n".join(lines)


def emit_address_search_log(report: AddressSearchReport) -> None:
    logger.info("%s", format_address_search_log(report))


def best_score_by_source(scored: List[dict]) -> dict[str, int]:
    by_source: dict[str, int] = {}
    for item in scored:
        src = (item.get("source") or "unknown").strip()
        score = int(item.get("score") or 0)
        by_source[src] = max(by_source.get(src, 0), score)
    return by_source


def build_provider_stats_list(
    provider_order: List[str],
    raw_counts: dict[str, int],
    best_scores: dict[str, int],
    errors: dict[str, str],
    skipped: Optional[set[str]] = None,
) -> List[ProviderSearchStats]:
    skipped = skipped or set()
    stats: List[ProviderSearchStats] = []
    seen: set[str] = set()
    for name in provider_order:
        if name in seen:
            continue
        seen.add(name)
        stats.append(
            ProviderSearchStats(
                provider=name,
                results=raw_counts.get(name, 0),
                best_score=best_scores.get(name),
                error=errors.get(name),
                skipped=name in skipped,
            )
        )
    for name in sorted(raw_counts.keys()):
        if name in seen:
            continue
        stats.append(
            ProviderSearchStats(
                provider=name,
                results=raw_counts.get(name, 0),
                best_score=best_scores.get(name),
                error=errors.get(name),
            )
        )
    return stats
