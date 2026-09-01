"""CZSC structure adapter for the independent Chan training extension.

All structure and signal calculations live in ``chan_czsc_core``.  This
module only keeps the training service API stable and attaches an optional
lower-level CZSC snapshot.  The referenced CZSC source does not expose an
independent segment (线段) calculator, so no local approximation is added.
"""
from __future__ import annotations

from typing import Any

from app.services.chan_czsc_core import IncrementalStructureAnalyzer, analyze_structure
from app.services.chan_market_data import ChanBarGenerator


def _complete_structure(structure: dict[str, Any], level: str) -> dict[str, Any]:
    fractals = structure["fractals"]
    strokes = structure["strokes"]
    centers = structure["centers"]
    points = structure["points"]
    return {
        **structure,
        "segments_source": "unsupported-by-upstream-czsc",
        "level": level,
        "segments": [],
        "summary": {
            "fractals": len(fractals),
            "strokes": len(strokes),
            "segments": 0,
            "centers": len(centers),
            "confirmed_points": sum(
                1 for point in points if point["status"] == "confirmed"
            ),
        },
    }


def _analyze_core(rows: list[dict[str, Any]], level: str) -> dict[str, Any]:
    return _complete_structure(analyze_structure(rows, level=level), level)


def analyze_chan(
    rows: list[dict[str, Any]],
    lower_rows: list[dict[str, Any]] | None = None,
    *,
    level: str = "1d",
    lower_level: str = "30m",
) -> dict[str, Any]:
    """Analyze only the supplied prefix with the ported CZSC implementation."""
    primary = _analyze_core(rows, level)
    primary["lower_level"] = (
        _analyze_core(lower_rows, lower_level) if lower_rows else None
    )
    return primary


class IncrementalChanAnalyzer:
    """Incremental daily/lower-level adapter for one training session."""

    def __init__(self, *, level: str = "1d", lower_level: str = "30m") -> None:
        self.level = level
        self.lower_level = lower_level
        self.primary = IncrementalStructureAnalyzer(level=level)
        self.lower = IncrementalStructureAnalyzer(level=lower_level)

    def update_primary(self, row: dict[str, Any], source_index: int | None = None) -> bool:
        return self.primary.update(row, source_index)

    def update_lower(self, row: dict[str, Any], source_index: int | None = None) -> bool:
        return self.lower.update(row, source_index)

    def snapshot(self, *, include_replay: bool = False) -> dict[str, Any]:
        primary = _complete_structure(
            self.primary.snapshot(include_replay=include_replay),
            self.level,
        )
        primary["lower_level"] = (
            _complete_structure(
                self.lower.snapshot(include_replay=include_replay),
                self.lower_level,
            )
            if self.lower.analyzer.raw_bars
            else None
        )
        return primary


class IncrementalMinuteChanAnalyzer:
    """Build and update strict CZSC 30m/daily analyzers from one 1m stream."""

    def __init__(self) -> None:
        self.generator = ChanBarGenerator((30, "1d"))
        self.analyzers = {
            "30": IncrementalStructureAnalyzer(level="30m"),
            "1d": IncrementalStructureAnalyzer(level="1d"),
        }

    def update_minute(self, row: dict[str, Any]) -> bool:
        updated = self.generator.update(row)
        accepted = False
        for key, analyzer in self.analyzers.items():
            if key not in updated or not self.generator.bars[key]:
                continue
            accepted = analyzer.update(self.generator.bars[key][-1]) or accepted
        return accepted

    def snapshot(self, *, include_replay: bool = False) -> dict[str, dict[str, Any] | None]:
        result: dict[str, dict[str, Any] | None] = {}
        for key, analyzer in self.analyzers.items():
            level = "30m" if key == "30" else key
            result[key] = (
                _complete_structure(
                    analyzer.snapshot(include_replay=include_replay),
                    level,
                )
                if analyzer.analyzer.raw_bars
                else None
            )
        return result
