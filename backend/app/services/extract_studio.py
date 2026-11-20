"""抽取工坊领域服务：管理交互式抽取规则的标注、审核与发布交接。

发布链路：
已审核任务 -> 审核回执（审核版本） -> 发布候选 -> 站点采集规则数据集
-> 模板规则（同审核版本的字段与选择器） -> 采集批次（固定选择器快照）。

同一站点上，不同布局的已审核任务可能为同一字段给出不同选择器：
布局不同时按页面范围并存，布局相同但选择器冲突时发布暂挂，并在冲突报告中
逐条说明涉及哪些页面、哪些字段。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from typing import Any

from app.services.base import BaseModuleService
from app.services.handoff import (
    COLLECTION_BATCHES,
    HELD_STATUS,
    INVALIDATED_STATUS,
    PUBLISH_CANDIDATES,
    PUBLISHED_STATUS,
    REVIEW_RECEIPTS,
    SITE_DATASETS,
    normalize_selector,
    now_iso as _now,
    split_values as _split,
)
from app.store import store


# 内存仓库不是线程安全的；发布与提交审核必须串行，保证同一审核版本只生效一次。
_HANDOFF_LOCK = threading.RLock()


def _split_pair(item: str) -> tuple[str, str]:
    separator = "=" if "=" in item else ("：" if "：" in item else ":")
    if separator not in item:
        return "", ""
    field, selector = item.split(separator, 1)
    return field.strip(), selector.strip()


def parse_field_pairs(task: dict[str, Any]) -> list[dict[str, str]]:
    """解析任务里的“字段=选择器”配对；未显式给出时按字段列表与标注区域顺序对齐。"""
    pairs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in _split(task.get("字段配对")):
        field, selector = _split_pair(item)
        selector = normalize_selector(selector)
        if field and selector and field not in seen:
            seen.add(field)
            pairs.append({"field": field, "selector": selector})
    if pairs:
        return pairs

    fields = _split(task.get("字段列表"))
    areas = _split(task.get("标注区域"))
    for index, field in enumerate(fields):
        if index < len(areas):
            pairs.append({"field": field, "selector": normalize_selector(areas[index])})
    return pairs


def _pairs_to_text(pairs: list[dict[str, str]]) -> str:
    return ", ".join(f"{pair['field']}={pair['selector']}" for pair in pairs)


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class ExtractStudioService(BaseModuleService):
    MODULE = "extract_studio"
    ENTITY = "交互任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "任务名称", "站点", "示例页面", "页面布局", "页面范围", "标注区域",
        "字段列表", "字段配对", "预览结果", "标注人", "完成时间",
    ]
    REQUIRED_FIELDS = ["任务名称", "示例页面", "标注区域"]
    STATUS_ORDER = ["草稿", "标注中", "已审核", "已发布"]
    ACTION_RULES = {"开始标注": "草稿", "提交审核": "标注中", "发布规则": "已审核"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------ 审核回执

    def _task_site(self, task: dict[str, Any]) -> str:
        explicit = str(task.get("站点") or "").strip()
        if explicit:
            return explicit
        page = str(task.get("示例页面") or "").strip()
        match = re.match(r"^[a-z]+://([^/]+)", page, re.IGNORECASE)
        return match.group(1) if match else page

    def _task_definition(self, task: dict[str, Any]) -> dict[str, Any]:
        """把任务压缩成审核定义：发布与冲突判断只看这份快照。"""
        site = self._task_site(task)
        layout = str(task.get("页面布局") or "默认布局").strip()
        sample_page = str(task.get("示例页面") or "").strip()
        pages = _split(task.get("页面范围")) or ([sample_page] if sample_page else [])
        pairs = parse_field_pairs(task)
        definition = {
            "task_id": int(task.get("id", 0)),
            "task_name": task.get("任务名称", ""),
            "site": site,
            "layout": layout,
            "sample_page": sample_page,
            "pages": pages,
            "pairs": pairs,
        }
        definition["fingerprint"] = hashlib.sha256(
            json.dumps(
                {"site": site, "layout": layout, "pairs": pairs, "page": sample_page},
                ensure_ascii=False, sort_keys=True,
            ).encode()
        ).hexdigest()
        return definition

    def _receipts_for_task(self, task_id: int) -> list[dict[str, Any]]:
        return [
            receipt for receipt in store.rows(REVIEW_RECEIPTS)
            if int(receipt.get("task_id", 0)) == task_id
        ]

    def current_receipt(self, task_id: int) -> dict[str, Any] | None:
        task = store.find(self.MODULE, task_id)
        if task is None:
            return None
        version = str(task.get("审核版本") or "")
        receipts = self._receipts_for_task(task_id)
        if version:
            for receipt in reversed(receipts):
                if receipt.get("review_version") == version:
                    return receipt
        return receipts[-1] if receipts else None

    def _pairs_problem(self, task: dict[str, Any]) -> str | None:
        """提交审核前校验字段配对完整性，避免发布出缺字段的数据集。"""
        declared = _split(task.get("字段列表"))
        explicit = _split(task.get("字段配对"))
        if explicit:
            blank = [
                field for field, selector in map(_split_pair, explicit)
                if field and not selector
            ]
            if blank:
                return f"字段配对存在空选择器：{'、'.join(blank)}"
            paired = [pair["field"] for pair in parse_field_pairs(task)]
            if declared and set(paired) != set(declared):
                missing = sorted(set(declared) - set(paired))
                extra = sorted(set(paired) - set(declared))
                detail = []
                if missing:
                    detail.append(f"缺少字段：{'、'.join(missing)}")
                if extra:
                    detail.append(f"未登记字段：{'、'.join(extra)}")
                return "字段配对与字段列表不一致；" + "；".join(detail)
            return None
        areas = _split(task.get("标注区域"))
        if declared and len(declared) != len(areas):
            return (
                f"字段列表 {len(declared)} 个字段与标注区域 {len(areas)} 个选择器"
                "无法一一配对，请显式填写字段配对"
            )
        return None

    def submit_review(self, task_id: int) -> dict[str, Any]:
        """提交审核：同一份审核定义只生成一次回执，重复提交不产生新版本。"""
        with _HANDOFF_LOCK:
            return self._submit_review_locked(task_id)

    def _submit_review_locked(self, task_id: int) -> dict[str, Any]:
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "reason": "抽取工坊任务不存在"}

        problem = self._pairs_problem(task)
        if problem:
            return {"ok": False, "reason": problem}
        definition = self._task_definition(task)
        if not definition["pairs"]:
            return {"ok": False, "reason": "没有可提交的字段与选择器配对"}

        receipts = self._receipts_for_task(task_id)
        for receipt in receipts:
            if receipt.get("fingerprint") == definition["fingerprint"]:
                task["审核版本"] = receipt["review_version"]
                task["审核回执ID"] = receipt["id"]
                candidate = self._candidate_for_receipt(receipt["id"])
                # 已发布过的同一版本再次提交：保持已发布状态，不重复生效。
                if not receipt.get("published"):
                    task["status"] = "已审核"
                    task["pending"] = False
                return {
                    "ok": True,
                    "duplicate": True,
                    "message": (
                        f"审核版本 {receipt['review_version']} 已存在，本次提交不重复生效"
                    ),
                    "task": task,
                    "receipt": receipt,
                    "candidate": candidate,
                }

        version = f"v{len(receipts) + 1}"
        receipt = {
            "id": _next_id(store.rows(REVIEW_RECEIPTS)),
            "task_id": task_id,
            "review_version": version,
            "fingerprint": definition["fingerprint"],
            "site": definition["site"],
            "layout": definition["layout"],
            "sample_page": definition["sample_page"],
            "pages": definition["pages"],
            "pairs": definition["pairs"],
            "status": "已审核",
            "published": False,
            "dataset_id": None,
            "created_at": _now(),
        }
        store.rows(REVIEW_RECEIPTS).append(receipt)

        candidate = self._create_candidate(task, receipt)
        task["status"] = "已审核"
        task["pending"] = False
        task["审核版本"] = version
        task["审核回执ID"] = receipt["id"]
        return {
            "ok": True,
            "duplicate": False,
            "message": f"审核回执 {version} 已生成，发布候选 {candidate['id']} 待发布",
            "task": task,
            "receipt": receipt,
            "candidate": candidate,
        }

    # ------------------------------------------------------------------ 发布候选

    def _create_candidate(self, task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
        candidate = {
            "id": _next_id(store.rows(PUBLISH_CANDIDATES)),
            "task_id": int(task.get("id", 0)),
            "task_name": task.get("任务名称", ""),
            "receipt_id": receipt["id"],
            "review_version": receipt["review_version"],
            "site": receipt["site"],
            "layout": receipt["layout"],
            "status": "待发布",
            "dataset_id": None,
            "batch_ids": [],
            "created_at": _now(),
        }
        store.rows(PUBLISH_CANDIDATES).append(candidate)
        return candidate

    def _candidate_for_receipt(self, receipt_id: int) -> dict[str, Any] | None:
        for candidate in reversed(store.rows(PUBLISH_CANDIDATES)):
            if int(candidate.get("receipt_id", 0)) == receipt_id:
                return candidate
        return None

    def list_candidates(self, *, site: str | None = None) -> list[dict[str, Any]]:
        candidates = list(store.rows(PUBLISH_CANDIDATES))
        if site:
            candidates = [item for item in candidates if item.get("site") == site]
        result = []
        for candidate in candidates:
            receipt = store.find(REVIEW_RECEIPTS, int(candidate["receipt_id"]))
            dataset = store.find(SITE_DATASETS, int(candidate["dataset_id"])) if candidate.get("dataset_id") else None
            result.append({
                **candidate,
                "receipt": receipt,
                "dataset_status": dataset.get("status") if dataset else None,
                # 候选只有通过回执才能追溯到审核版本，不能凭任务当前状态臆测。
                "review_version": receipt.get("review_version") if receipt else candidate.get("review_version"),
                "traceable": receipt is not None,
            })
        return result

    # ------------------------------------------------------------------ 冲突分析

    def _receipt_definition(self, task: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
        """优先使用审核回执里的快照：审核之后任务再被编辑，也不能改变已审核版本。"""
        if receipt.get("site"):
            definition = {
                "task_id": int(task.get("id", 0)),
                "task_name": task.get("任务名称", ""),
                "site": receipt.get("site", ""),
                "layout": receipt.get("layout") or "默认布局",
                "sample_page": receipt.get("sample_page", ""),
                "pages": list(receipt.get("pages") or []),
                "pairs": [dict(pair) for pair in receipt.get("pairs") or []],
            }
            definition["fingerprint"] = receipt.get("fingerprint", "")
            return definition
        return self._task_definition(task)

    def _reviewed_definitions(self, site: str) -> list[dict[str, Any]]:
        """同一站点下所有已审核/已发布任务的审核定义（有回执时以回执为准）。"""
        definitions = []
        for task in store.rows(self.MODULE):
            if task.get("status") not in ("已审核", PUBLISHED_STATUS):
                continue
            if self._task_site(task) != site:
                continue
            receipt = self.current_receipt(int(task.get("id", 0)))
            definition = (
                self._receipt_definition(task, receipt) if receipt else self._task_definition(task)
            )
            if not definition["pairs"]:
                continue
            definition["review_version"] = (
                receipt.get("review_version") if receipt else task.get("审核版本", "")
            )
            definition["receipt_id"] = receipt.get("id") if receipt else None
            definitions.append(definition)
        return definitions

    def detect_conflicts(self, site: str) -> list[dict[str, Any]]:
        """按字段归并同站定义；选择器不同即形成冲突，并给出页面范围与严重度。"""
        by_field: dict[str, list[dict[str, Any]]] = {}
        for definition in self._reviewed_definitions(site):
            for pair in definition["pairs"]:
                by_field.setdefault(pair["field"], []).append({
                    "selector": pair["selector"],
                    "layout": definition["layout"],
                    "pages": list(definition["pages"]),
                    "task_id": definition["task_id"],
                    "task_name": definition["task_name"],
                    "review_version": definition["review_version"],
                    "receipt_id": definition["receipt_id"],
                })

        conflicts = []
        for field, items in sorted(by_field.items()):
            selectors = {item["selector"] for item in items}
            if len(selectors) <= 1:
                continue
            variants = []
            for selector in sorted(selectors):
                group = [item for item in items if item["selector"] == selector]
                layouts = sorted({item["layout"] for item in group})
                pages = sorted({page for item in group for page in item["pages"] if page})
                variants.append({
                    "selector": selector,
                    "layouts": layouts,
                    "pages": pages,
                    "tasks": [
                        {
                            "id": item["task_id"],
                            "name": item["task_name"],
                            "review_version": item["review_version"],
                            "receipt_id": item["receipt_id"],
                        }
                        for item in group
                    ],
                })
            # 同一布局出现两个不同选择器：页面归属无法判断，必须暂挂。
            layout_owners: dict[str, set[str]] = {}
            for item in items:
                layout_owners.setdefault(item["layout"], set()).add(item["selector"])
            blocking_layouts = sorted(
                layout for layout, owners in layout_owners.items() if len(owners) > 1
            )
            pages = sorted({page for item in items for page in item["pages"] if page})
            if blocking_layouts:
                severity = "阻断"
                explanation = (
                    f"站点 {site} 的字段「{field}」在布局 {'、'.join(blocking_layouts)} 内"
                    f"存在 {len(selectors)} 个互斥选择器，无法确定页面归属，发布暂挂；"
                    f"涉及页面：{'、'.join(pages)}"
                )
            else:
                severity = "提示"
                explanation = (
                    f"站点 {site} 的字段「{field}」因布局差异使用 {len(selectors)} 个选择器，"
                    f"已按页面范围分别生效；涉及页面：{'、'.join(pages)}"
                )
            conflicts.append({
                "field": field,
                "site": site,
                "severity": severity,
                "selectors": sorted(selectors),
                "variants": variants,
                "pages": pages,
                "layouts": sorted({item["layout"] for item in items}),
                "explanation": explanation,
            })
        return conflicts

    def _dataset_conflicts(self, definition: dict[str, Any]) -> list[dict[str, Any]]:
        """数据集只带出与自身字段相关的冲突，避免把同站无关字段的提示混进交付物。"""
        own_fields = {pair["field"] for pair in definition["pairs"]}
        return [
            conflict for conflict in self.detect_conflicts(definition["site"])
            if conflict["field"] in own_fields
        ]

    def conflict_report(self, task_id: int) -> dict[str, Any]:
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "reason": "抽取工坊任务不存在"}
        site = self._task_site(task)
        conflicts = self.detect_conflicts(site)
        return {
            "ok": True,
            "task_id": task_id,
            "site": site,
            "review_version": task.get("审核版本", ""),
            "publish_status": task.get("发布状态") or "未发布",
            "blocking": any(item["severity"] == "阻断" for item in conflicts),
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
            "impacted_fields": sorted({item["field"] for item in conflicts}),
            "impacted_pages": sorted({page for item in conflicts for page in item["pages"]}),
        }

    # ------------------------------------------------------------------ 数据集与发布

    def build_dataset(self, task_id: int) -> dict[str, Any]:
        """把已审核任务转换成可执行的站点采集规则数据集（只读预览，不落库）。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "reason": "抽取工坊任务不存在"}
        if task.get("status") not in ("已审核", PUBLISHED_STATUS):
            return {"ok": False, "reason": "仅已审核的任务可以生成采集规则数据集"}
        receipt = self.current_receipt(task_id)
        definition = self._receipt_definition(task, receipt) if receipt else self._task_definition(task)
        conflicts = self._dataset_conflicts(definition)
        blocking = [
            item for item in conflicts
            if item["severity"] == "阻断"
            and any(
                task_id == task_ref["id"]
                for variant in item["variants"]
                for task_ref in variant["tasks"]
            )
        ]
        review_version = (
            receipt.get("review_version") if receipt else task.get("审核版本", "v1")
        )
        dataset = {
            "site": definition["site"],
            "review_version": review_version,
            "receipt_id": receipt.get("id") if receipt else None,
            "layout": definition["layout"],
            "sample_page": definition["sample_page"],
            "pages": definition["pages"],
            "fields": [
                {
                    "field": pair["field"],
                    "selector": pair["selector"],
                    "layout": definition["layout"],
                    "pages": definition["pages"],
                }
                for pair in definition["pairs"]
            ],
            "field_names": [pair["field"] for pair in definition["pairs"]],
            "executable": not blocking,
            "status": HELD_STATUS if blocking else "可执行",
            "conflicts": conflicts,
            "conflict_count": len(conflicts),
            "impacted_fields": sorted({item["field"] for item in conflicts}),
            "impacted_pages": sorted({page for item in conflicts for page in item["pages"]}),
        }
        return {"ok": True, "task_id": task_id, "dataset": dataset}

    def publish(self, task_id: int) -> dict[str, Any]:
        """发布审核版本：生成数据集与模板规则，回写工坊清单并推进候选与回执。

        同一任务、同一审核版本重复发布只生效一次，直接返回已有结果。
        """
        with _HANDOFF_LOCK:
            return self._publish_locked(task_id)

    def _publish_locked(self, task_id: int) -> dict[str, Any]:
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "reason": "抽取工坊任务不存在"}
        if task.get("status") not in ("已审核", PUBLISHED_STATUS):
            return {"ok": False, "reason": "仅供已审核的任务发布站点采集规则"}

        receipt = self.current_receipt(task_id)
        if receipt is None:
            # 兼容旧数据：已审核但没有回执时补一次审核回执，保证发布可追溯。
            submitted = self.submit_review(task_id)
            if not submitted["ok"]:
                return submitted
            receipt = submitted["receipt"]

        definition = self._receipt_definition(task, receipt)
        site = definition["site"]
        conflicts = self._dataset_conflicts(definition)
        blocking = [
            conflict for conflict in conflicts
            if conflict["severity"] == "阻断"
            and any(
                task_id == task_ref["id"]
                for variant in conflict["variants"]
                for task_ref in variant["tasks"]
            )
        ]

        existing = [
            dataset for dataset in store.rows(SITE_DATASETS)
            if int(dataset.get("task_id", 0)) == task_id
            and dataset.get("review_version") == receipt["review_version"]
        ]
        if existing:
            dataset = existing[0]
            candidate = self._candidate_for_receipt(int(receipt["id"]))
            if dataset.get("status") == HELD_STATUS and not blocking:
                # 冲突已解除：沿用同一数据集继续发布，历史号不变。
                dataset["conflicts"] = conflicts
                dataset["impacted_fields"] = sorted(
                    {item["field"] for item in conflicts}
                )
                dataset["impacted_pages"] = sorted(
                    {page for item in conflicts for page in item["pages"]}
                )
                dataset.pop("hold_reason", None)
                dataset["status"] = PUBLISHED_STATUS
                dataset["executable"] = True
                return self._finish_publish(task, receipt, candidate, dataset)
            if dataset.get("status") == HELD_STATUS:
                # 暂挂后冲突仍未解除：保持暂挂，不重复产出数据集。
                return self._publish_result(
                    task, receipt, candidate, dataset, duplicate=True,
                    message=f"审核版本 {receipt['review_version']} 仍处于暂挂，冲突未解除",
                )
            return self._publish_result(
                task, receipt, candidate, dataset, duplicate=True,
                message=f"审核版本 {receipt['review_version']} 已发布过，本次不重复生效",
            )

        held = bool(blocking)
        dataset = {
            "id": _next_id(store.rows(SITE_DATASETS)),
            "task_id": task_id,
            "task_name": task.get("任务名称", ""),
            "receipt_id": receipt["id"],
            "review_version": receipt["review_version"],
            "site": site,
            "layout": definition["layout"],
            "sample_page": definition["sample_page"],
            "pages": definition["pages"],
            "fields": [
                {
                    "field": pair["field"],
                    "selector": pair["selector"],
                    "layout": definition["layout"],
                    "pages": definition["pages"],
                }
                for pair in definition["pairs"]
            ],
            "status": HELD_STATUS if held else PUBLISHED_STATUS,
            "executable": not held,
            "conflicts": conflicts,
            "impacted_fields": sorted({item["field"] for item in conflicts}),
            "impacted_pages": sorted({page for item in conflicts for page in item["pages"]}),
            "template_ids": [],
            "created_at": _now(),
        }
        store.rows(SITE_DATASETS).append(dataset)

        candidate = self._candidate_for_receipt(int(receipt["id"]))
        if held:
            dataset["hold_reason"] = "；".join(item["explanation"] for item in blocking)
            if candidate:
                candidate["status"] = HELD_STATUS
                candidate["dataset_id"] = dataset["id"]
                candidate["hold_reason"] = dataset["hold_reason"]
            task["发布状态"] = HELD_STATUS
            task["发布数据集ID"] = dataset["id"]
            task["最近发布审核版本"] = receipt["review_version"]
            message = f"发布暂挂：{dataset['hold_reason']}"
            return self._publish_result(task, receipt, candidate, dataset, duplicate=False, message=message)

        return self._finish_publish(task, receipt, candidate, dataset)

    def _finish_publish(
        self,
        task: dict[str, Any],
        receipt: dict[str, Any],
        candidate: dict[str, Any] | None,
        dataset: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = int(task.get("id", 0))
        template_ids = self._create_templates(dataset)
        dataset["template_ids"] = template_ids
        if candidate:
            candidate["status"] = PUBLISHED_STATUS
            candidate["dataset_id"] = dataset["id"]
            candidate.pop("hold_reason", None)
        receipt["published"] = True
        receipt["dataset_id"] = dataset["id"]

        # 发布结果回写工坊清单，供审核模块同步采集版本。
        task["status"] = PUBLISHED_STATUS
        task["pending"] = False
        task["发布状态"] = PUBLISHED_STATUS
        task["发布版本"] = receipt["review_version"]
        task["最近发布审核版本"] = receipt["review_version"]
        task["发布数据集ID"] = dataset["id"]
        task["模板ID列表"] = template_ids
        task["发布回执ID"] = receipt["id"]

        # 同一任务的旧候选被新版本取代，但历史回执与已运行批次仍可追溯。
        for other in store.rows(PUBLISH_CANDIDATES):
            if int(other.get("task_id", 0)) == task_id and other is not candidate:
                if other.get("status") == PUBLISHED_STATUS:
                    other["status"] = INVALIDATED_STATUS
        for dataset_other in store.rows(SITE_DATASETS):
            if (
                int(dataset_other.get("task_id", 0)) == task_id
                and dataset_other["id"] != dataset["id"]
                and dataset_other.get("status") == PUBLISHED_STATUS
            ):
                dataset_other["status"] = INVALIDATED_STATUS

        return self._publish_result(
            task, receipt, candidate, dataset, duplicate=False,
            message=(
                f"审核版本 {receipt['review_version']} 已发布，数据集 {dataset['id']} "
                f"交付模板 {len(template_ids)} 个"
            ),
        )

    def _publish_result(
        self,
        task: dict[str, Any],
        receipt: dict[str, Any],
        candidate: dict[str, Any] | None,
        dataset: dict[str, Any],
        *,
        duplicate: bool,
        message: str,
    ) -> dict[str, Any]:
        conflicts = dataset.get("conflicts", [])
        return {
            "ok": True,
            "duplicate": duplicate,
            "message": message,
            "publish_status": dataset.get("status"),
            "held": dataset.get("status") == HELD_STATUS,
            "task": task,
            "receipt": receipt,
            "candidate": candidate,
            "dataset": dataset,
            "template_ids": dataset.get("template_ids", []),
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
            "impacted_fields": dataset.get("impacted_fields", []),
            "impacted_pages": dataset.get("impacted_pages", []),
            "review_sync": self.collection_version(int(task.get("id", 0))),
        }

    def _create_templates(self, dataset: dict[str, Any]) -> list[int]:
        """从可执行数据集生成模板规则，字段和选择器保持同一审核版本。"""
        from app.services.template_rule import TemplateRuleService

        rows = store.rows("template_rule")
        created: list[int] = []
        # 数据集只对应一个布局，生成一条模板，避免同站模板重复。
        ordered_fields = dataset["fields"]
        template = {
            "id": _next_id(rows),
            "status": PUBLISHED_STATUS,
            "pending": False,
            "abnormal": False,
            "模板名称": f"{dataset['site']}-{dataset['layout']}-{dataset['review_version']}",
            "适用站点": dataset["site"],
            "提取字段": ", ".join(field["field"] for field in ordered_fields),
            "字段选择器": ", ".join(field["selector"] for field in ordered_fields),
            "采样页面": dataset["sample_page"],
            "校验规则": "非空校验、类型校验",
            "更新策略": "审核版本发布时同步",
            "站点": dataset["site"],
            "页面布局": dataset["layout"],
            "字段配对": _pairs_to_text(ordered_fields),
            "来源数据集": dataset["id"],
            "来源任务": dataset["task_id"],
            "审核版本": dataset["review_version"],
            "审核回执ID": dataset["receipt_id"],
        }
        rows.append(template)
        created.append(template["id"])
        # 同任务旧版本模板标记失效，采集端只应交付当前审核版本。
        for row in rows:
            if row is template:
                continue
            if (
                row.get("来源任务") == dataset["task_id"]
                and row.get("审核版本") != dataset["review_version"]
                and row.get("status") == PUBLISHED_STATUS
            ):
                row["status"] = INVALIDATED_STATUS
        TemplateRuleService().refresh_published_cache()
        return created

    # ------------------------------------------------------------------ 采集交付

    def collection_version(self, task_id: int) -> dict[str, Any]:
        """审核模块同步入口：返回审核版本与采集端应使用的数据集/模板。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "reason": "抽取工坊任务不存在"}
        receipt = self.current_receipt(task_id)
        dataset_id = task.get("发布数据集ID")
        dataset = store.find(SITE_DATASETS, int(dataset_id)) if dataset_id else None
        if dataset is None and receipt:
            dataset = next(
                (
                    item for item in store.rows(SITE_DATASETS)
                    if int(item.get("task_id", 0)) == task_id
                    and item.get("review_version") == receipt.get("review_version")
                ),
                None,
            )
        return {
            "ok": True,
            "task_id": task_id,
            "task_status": task.get("status"),
            "publish_status": task.get("发布状态") or "未发布",
            "review_version": receipt.get("review_version") if receipt else None,
            "review_receipt_id": receipt.get("id") if receipt else None,
            "review_receipt_status": receipt.get("status") if receipt else None,
            "dataset_id": dataset.get("id") if dataset else None,
            "dataset_status": dataset.get("status") if dataset else None,
            "template_ids": dataset.get("template_ids", []) if dataset else [],
            "effective": bool(dataset and dataset.get("status") == PUBLISHED_STATUS),
            "conflict_count": len(dataset.get("conflicts", [])) if dataset else 0,
            "impacted_fields": dataset.get("impacted_fields", []) if dataset else [],
            "impacted_pages": dataset.get("impacted_pages", []) if dataset else [],
            # 审核后又编辑但没有重新提交：采集端仍应用已审核版本，这里显式提示。
            "unversioned_changes": bool(
                receipt and receipt.get("fingerprint") != self._task_definition(task)["fingerprint"]
            ),
        }

    def trace(self, task_id: int) -> dict[str, Any]:
        """发布追溯：任务 -> 回执 -> 候选 -> 数据集 -> 采集批次。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "reason": "抽取工坊任务不存在"}
        receipts = self._receipts_for_task(task_id)
        candidate_ids = {candidate["id"] for candidate in store.rows(PUBLISH_CANDIDATES)
                         if int(candidate.get("task_id", 0)) == task_id}
        candidates = self.list_candidates()
        candidates = [item for item in candidates if item["id"] in candidate_ids]
        datasets = [
            dataset for dataset in store.rows(SITE_DATASETS)
            if int(dataset.get("task_id", 0)) == task_id
        ]
        batches = [
            batch for batch in store.rows(COLLECTION_BATCHES)
            if int(batch.get("task_id", 0)) == task_id
        ]
        return {
            "ok": True,
            "task": task,
            "receipts": receipts,
            "candidates": candidates,
            "datasets": datasets,
            "batches": [self.batch_summary(batch) for batch in batches],
            "current_receipt": self.current_receipt(task_id),
            "collection_version": self.collection_version(task_id),
        }

    def batch_summary(self, batch: dict[str, Any]) -> dict[str, Any]:
        """批次摘要始终返回创建时的选择器快照，不随候选状态变化。"""
        return {
            "id": batch.get("id"),
            "task_id": batch.get("task_id"),
            "site": batch.get("site"),
            "dataset_id": batch.get("dataset_id"),
            "review_version": batch.get("review_version"),
            "review_receipt_id": batch.get("review_receipt_id"),
            "status": batch.get("status"),
            "fields": [pair["field"] for pair in batch.get("selector_snapshot", [])],
            "selector_snapshot": batch.get("selector_snapshot", []),
            "selector_snapshot_hash": batch.get("selector_snapshot_hash"),
            "pages": batch.get("pages", []),
            "records": batch.get("records", []),
            "created_at": batch.get("created_at"),
        }

    # ------------------------------------------------------------------ 动作

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "提交审核":
            result = self.submit_review(entry_id)
            if not result["ok"]:
                return None, result["reason"]
            return result["task"], result["message"]
        if action == "发布规则":
            result = self.publish(entry_id)
            if not result.get("task"):
                return None, result.get("reason") or result.get("message", "发布失败")
            return result["task"], result["message"]
        return super().run_action(entry_id, action)

    # ------------------------------------------------------------------ 兼容旧链路

    def export_to_template_rule(self, studio_task_id: int) -> dict[str, Any]:
        """保留旧的导出视图，内容改为可执行的站点采集规则数据集。"""
        built = self.build_dataset(studio_task_id)
        if not built["ok"]:
            return built
        dataset = built["dataset"]
        return {
            "ok": True,
            "studio_task_id": studio_task_id,
            "review_version": dataset["review_version"],
            "dataset": dataset,
            "template": {
                "模板名称": f"{dataset['site']}-{dataset['layout']}-{dataset['review_version']}",
                "适用站点": dataset["site"],
                "提取字段": ", ".join(dataset["field_names"]),
                "字段选择器": ", ".join(field["selector"] for field in dataset["fields"]),
                "采样页面": dataset["sample_page"],
                "校验规则": "非空校验、类型校验",
                "更新策略": "审核版本发布时同步",
                "审核版本": dataset["review_version"],
            },
            "selector_count": len(dataset["fields"]),
            "field_count": len(dataset["field_names"]),
            "conflicts": dataset["conflicts"],
        }

    def preview_extraction(self, studio_task_id: int) -> dict[str, Any]:
        """预览标注区域的实际抽取效果，使用字段配对的真实选择器。"""
        task = store.find(self.MODULE, studio_task_id)
        if task is None:
            return {"ok": False, "reason": "任务不存在"}

        pairs = parse_field_pairs(task)
        sample_page = task.get("示例页面", "")

        preview = {}
        for i, pair in enumerate(pairs):
            val_hash = int(
                hashlib.md5(
                    f"{sample_page}|{pair['field']}|{pair['selector']}|{i}".encode()
                ).hexdigest()[:4],
                16,
            )
            preview[pair["field"]] = {
                "selector": pair["selector"],
                "value": f"提取值样例#{val_hash % 100}" if val_hash % 3 != 0 else "（未匹配）",
            }

        return {
            "ok": True,
            "task_name": task.get("任务名称"),
            "sample_page": sample_page,
            "fields_count": len(pairs),
            "preview": preview,
            "match_ratio": round(
                sum(1 for item in preview.values() if "未匹配" not in item["value"])
                / len(preview), 2
            ) if preview else 0.0,
        }

    def compute_annotation_progress(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        counts = {
            status: sum(1 for row in rows if row.get("status") == status)
            for status in self.STATUS_ORDER
        }
        return {
            "total_tasks": len(rows),
            "draft": counts["草稿"],
            "annotating": counts["标注中"],
            "reviewed": counts["已审核"],
            "published": counts["已发布"],
            "held": sum(1 for row in rows if row.get("发布状态") == HELD_STATUS),
            "completion_ratio": round(
                (counts["已审核"] + counts["已发布"]) / len(rows), 2
            ) if rows else 0.0,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["annotation_progress"] = self.compute_annotation_progress()
        base["publish_handoff"] = {
            "receipts": len(store.rows(REVIEW_RECEIPTS)),
            "candidates": len(store.rows(PUBLISH_CANDIDATES)),
            "datasets": len(store.rows(SITE_DATASETS)),
            "batches": len(store.rows(COLLECTION_BATCHES)),
            "held": sum(
                1 for item in store.rows(SITE_DATASETS) if item.get("status") == HELD_STATUS
            ),
        }
        return base
