"""发布交接的共享约定：内部流转表名、字段分隔与选择器规范化。

这些表不登记成业务模块，避免出现在运营概览的模块统计里；
但跨模块链路（工坊 → 模板规则 → 采集批次）都需要按同一套名字读写。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

REVIEW_RECEIPTS = "_review_receipts"
PUBLISH_CANDIDATES = "_publish_candidates"
SITE_DATASETS = "_site_rule_datasets"
COLLECTION_BATCHES = "_collection_batches"

HELD_STATUS = "暂挂"
PUBLISHED_STATUS = "已发布"
INVALIDATED_STATUS = "已失效"

_SEPARATOR = re.compile(r"[,，;；]")

# 标注区域里可能直接写裸类名；带 CSS 语法或本身是标签名的值保持原样。
HTML_TAGS = {
    "a", "article", "aside", "body", "button", "div", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "img", "input", "li", "main", "nav", "ol",
    "p", "section", "span", "table", "tbody", "td", "th", "thead", "tr", "ul",
}
_CSS_SYNTAX = re.compile(r"[.#\[\]>:+~*()/\\@\s]")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def split_values(value: Any) -> list[str]:
    return [item.strip() for item in _SEPARATOR.split(str(value or "")) if item.strip()]


def normalize_selector(selector: str) -> str:
    """把标注区域里的裸类名补全成 CSS 选择器，保留已写好的选择器原样。"""
    selector = selector.strip()
    if not selector:
        return ""
    if _CSS_SYNTAX.search(selector):
        return selector
    if selector.casefold() in HTML_TAGS:
        return selector
    return f".{selector}"
