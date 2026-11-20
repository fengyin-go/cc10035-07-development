"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS


INTERNAL_TABLES = (
    "_review_receipts",
    "_publish_candidates",
    "_site_rule_datasets",
    "_collection_batches",
)


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 发布交接的内部流转表：不登记成业务模块，避免污染运营概览的模块统计。
        for name in INTERNAL_TABLES:
            self._tables.setdefault(name, [])

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if not name.startswith("_"))

    def reset(self) -> None:
        """把内存仓库恢复到示例数据，供测试和演示重放同一条发布链路。"""
        self._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in INTERNAL_TABLES:
            self._tables.setdefault(name, [])

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
