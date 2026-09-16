"""数据源 → 系统的统一中间格式。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class RawAnnouncement:
    ann_id: str                  # 唯一 ID，幂等去重的键
    code: str
    name: str
    title: str
    date: str                    # YYYY-MM-DD
    board: str = ""
    market_value: float = 0.0    # 亿元
    url: str = ""
    summary: str = ""


@dataclass(slots=True)
class RawKline:
    code: str
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    change_pct: float | None = None
    source: str = "unknown"
    adjustment: str = "qfq"


@dataclass(slots=True)
class DayResult:
    date: str
    source: str
    announcements: list[RawAnnouncement] = field(default_factory=list)
    fetched: int = 0
    expected: int | None = None
    complete: bool = False
    error: str = ""


class SourceError(RuntimeError):
    pass
