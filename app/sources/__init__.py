"""
数据源选择。

优先级：环境变量 BOARD_FETCHER > rules.yaml 的 fetch.source > eastmoney

  eastmoney  东方财富公告（主力，生产在用）
  cninfo     巨潮资讯公告（备选）
  mock       离线假数据（本地开发 / 验证流水线，不碰网络）
"""
from __future__ import annotations

import logging
import os

from .base import AnnouncementSource
from .types import RawAnnouncement, RawKline  # noqa: F401  （对外导出）

log = logging.getLogger("sources")


def source_name() -> str:
    name = os.getenv("BOARD_FETCHER", "").strip().lower()
    if name:
        return "eastmoney" if name == "em" else name
    from ..config import get_fetch_config
    name = str(get_fetch_config().get("source", "eastmoney")).strip().lower()
    return "eastmoney" if name == "em" else name


def get_source(name: str = "") -> AnnouncementSource:
    name = (name or source_name()).lower()

    if name == "mock":
        from .mock import MockSource
        from ..config import get_fetch_config
        m = get_fetch_config().get("mock") or {}
        return MockSource(seed=int(m.get("seed", 20260828)), pool_size=int(m.get("pool_size", 800)),
                          ann_per_day=int(m.get("ann_per_day", 150)), kline_days=int(m.get("kline_days", 160)))

    if name == "cninfo":
        from .cninfo import CninfoSource
        return CninfoSource()

    if name in ("eastmoney", "em"):
        from .eastmoney import EastmoneySource
        return EastmoneySource()

    raise ValueError("未知的数据源：%s（可选 mock / cninfo / eastmoney）" % name)
