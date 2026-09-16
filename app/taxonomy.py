"""
规则引擎 —— 分类 / 噪声过滤 / 关键数字提取。

全部口径来自 config/rules.yaml 这一份 taxonomy，前后端共用，
所以首页卡片和详情页的分类天然一致（老站是两套标签各写各的，互相对不上）。

★ 为什么没有热重载：
  老实现有一套 `_auto_reload_if_changed()` —— 每次 classify 都去 stat 一下
  rules.yaml，mtime 变了就重读。那是给常驻进程准备的（改配置不重启就生效）。
  本版在固定 worker 中每个 CLI 任务启动新进程；进程内只编译一次规则。
  修改规则后应启动新任务，并对历史公告显式执行 reclassify。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

import yaml

from .paths import rules_path
from .config import validate_config

# 金额统一换算到「元」
_AMOUNT_UNIT = {"亿元": 1e8, "万元": 1e4, "元": 1.0}


class Taxonomy:
    """rules.yaml 的运行时视图。进程内只加载一次。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or rules_path()
        self._load()

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            cfg = validate_config(yaml.safe_load(f))

        self.version = cfg.get("version", 1)
        self._raw: List[dict] = cfg.get("taxonomy") or []

        classify_cfg = cfg.get("classify") or {}
        self.multi_label: bool = bool(classify_cfg.get("multi_label", False))
        self.default_category: str = classify_cfg.get("default_category", "other")
        self.match_fields = classify_cfg.get("match_fields", ["title", "summary"])
        if not self.match_fields or any(f not in ("title", "summary") for f in self.match_fields):
            raise ValueError("classify.match_fields 无效")

        # 规则按 priority 降序 —— 单标签时"第一个命中"就是最高优先级
        rules = []
        for item in self._raw:
            if not item.get("enabled", True):
                continue
            rules.append({
                "id": item["id"],
                "keywords": tuple(k for k in (item.get("keywords") or []) if k),
                "patterns": tuple(re.compile(p) for p in (item.get("patterns") or [])),
                "priority": int(item.get("priority", 0)),
                "min_amount": item.get("min_amount"),
            })
        rules.sort(key=lambda r: r["priority"], reverse=True)
        self.rules = rules

        nx = cfg.get("numeric_extraction") or {}
        self._num_max = int(nx.get("max_items", 4))
        self._num_patterns = tuple(
            re.compile(p)
            for key in ("money", "percent", "share")
            for p in (nx.get(key) or [])
        )
        self._amount_patterns = tuple(re.compile(p) for p in (nx.get("money") or []))

        noise = cfg.get("noise") or {}
        self.noise_on = bool(noise.get("enabled", True))
        self._noise_titles = tuple(noise.get("titles") or [])
        self._pledge_white = tuple(noise.get("pledge_whitelist") or [])
        self._risk_ids = {x["id"] for x in self._raw if x.get("risk")}

    # ---------------- 分类 ----------------
    def classify(self, title: str, summary: str = "") -> str:
        """
        返回分类 id。命中多类时取 priority 最高者；未命中返回 default_category。

        带 min_amount 的规则（目前只有质押需要大额）金额不够则跳过该类。
        金额是**惰性**解析的：只有真走到那条规则才去跑金额正则。
        老实现是循环之前无条件算一次，而 118 个关键词里只有 1 条配了 min_amount，
        绝大多数公告根本走不到 —— 白跑。实测 6.5 万条：
        _max_amount 调用 65577 次 → 910 次，热路径 1.76s → 1.14s。
        """
        text = " ".join({"title": title or "", "summary": summary or ""}[f] for f in self.match_fields).strip()
        if not text:
            return self.default_category

        amount: Optional[float] = None
        first_hit: Optional[str] = None

        for rule in self.rules:
            matched = any(kw in text for kw in rule["keywords"])
            if not matched:
                matched = any(p.search(text) for p in rule["patterns"])
            if not matched:
                continue
            if rule["min_amount"]:
                if amount is None:
                    amount = self._max_amount(text)
                if amount is not None and amount < float(rule["min_amount"]):
                    continue
            if not self.multi_label:
                return rule["id"]
            if first_hit is None:
                first_hit = rule["id"]

        return first_hit or self.default_category

    def label_of(self, category_id: str) -> str:
        for item in self._raw:
            if item.get("id") == category_id:
                return item.get("label", category_id)
        return category_id

    def taxonomy(self) -> List[dict]:
        """给前端的分类清单（按 order 排序）。"""
        out = [{
            "id": item["id"],
            "label": item.get("label", item["id"]),
            "short": item.get("short") or item.get("label", item["id"]),
            "icon": item.get("icon", ""),
            "order": int(item.get("order", 99)),
            "risk": bool(item.get("risk", False)),
            "color": item.get("display_color", "#6b7280"),
        } for item in self._raw if item.get("enabled", True)]
        out.sort(key=lambda x: x["order"])
        return out

    # ---------------- 噪声 ----------------
    def is_noise(self, title: str) -> bool:
        """
        标记不展示的例行公告；采集层仍保留原始标题和来源以便重分类。

        两条规则：
          1. 标题含噪声词（章程 / 议事规则 / 回购进展 / 债券 / 审计报告…）
          2. 质押类特例：只保留「控股股东 / 第一大股东 / 5%以上股东 / 解除质押」，
             其余千篇一律的"关于股份质押的公告"全部过滤
        """
        if not self.noise_on:
            return False
        t = title or ""
        if self.classify(t) in self._risk_ids:
            return False
        if any(kw in t for kw in self._noise_titles):
            return True
        if "质押" in t and not any(k in t for k in self._pledge_white):
            return True
        return False

    # ---------------- 关键数字 ----------------
    def extract_numbers(self, text: str) -> List[str]:
        """提取金额/比例/股数，去重保序，最多 max_items 个。"""
        if not text:
            return []
        found: List[str] = []
        for pat in self._num_patterns:
            for m in pat.finditer(text):
                val = m.group(0).strip()
                if val not in found:
                    found.append(val)
                    if len(found) >= self._num_max:
                        return found
        return found

    def _max_amount(self, text: str) -> Optional[float]:
        """文本里的最大金额（换算成元），用于 min_amount 阈值判断。"""
        best: Optional[float] = None
        for pat in self._amount_patterns:
            for m in pat.finditer(text):
                try:
                    num = float(m.group(1).replace(",", ""))
                except (ValueError, IndexError):
                    continue
                seg = m.group(0)
                unit = 1.0
                for u, mul in _AMOUNT_UNIT.items():
                    if u in seg:
                        unit = mul
                        break
                val = num * unit
                if best is None or val > best:
                    best = val
        return best


_engine: Optional[Taxonomy] = None


def get_engine() -> Taxonomy:
    """进程内单例。"""
    global _engine
    if _engine is None:
        _engine = Taxonomy()
    return _engine
