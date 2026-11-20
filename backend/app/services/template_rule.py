"""模板规则领域服务：管理站点级字段抽取模板，并向采集端交付采集批次。

模板只能从“可执行的站点采集规则数据集”生成采集输入；采集批次在创建时
固化完整的字段配对与选择器快照，候选或模板后续状态变化不影响进行中的批次。
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from app.services.base import BaseModuleService
from app.services.handoff import (
    COLLECTION_BATCHES,
    PUBLISH_CANDIDATES,
    SITE_DATASETS,
    REVIEW_RECEIPTS,
    now_iso,
    split_values,
)
from app.store import store

BATCH_CONFIRMED = "已确认"
BATCH_RUNNING = "采集中"
BATCH_DONE = "已完成"
BATCH_FAILED = "失败"

# 批次创建与运行串行化，避免同一批次被并发运行两次。
_BATCH_LOCK = threading.RLock()


def _parse_pairs(template: dict[str, Any]) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in split_values(template.get("字段配对")):
        separator = "=" if "=" in item else ("：" if "：" in item else ":")
        if separator not in item:
            continue
        field, selector = item.split(separator, 1)
        field, selector = field.strip(), selector.strip()
        if field and selector and field not in seen:
            seen.add(field)
            pairs.append({"field": field, "selector": selector})
    if pairs:
        return pairs

    fields = split_values(template.get("提取字段"))
    selectors = split_values(template.get("字段选择器"))
    if len(fields) != len(selectors):
        return []
    for field, selector in zip(fields, selectors):
        if field and selector and field not in seen:
            seen.add(field)
            pairs.append({"field": field, "selector": selector})
    return pairs


def _snapshot_hash(pairs: list[dict[str, str]]) -> str:
    return hashlib.sha256(
        json.dumps(pairs, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class TemplateRuleService(BaseModuleService):
    MODULE = "template_rule"
    ENTITY = "规则模板"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["模板名称", "适用站点", "提取字段", "字段选择器", "采样页面", "校验规则", "更新策略"]
    REQUIRED_FIELDS = ["模板名称", "适用站点", "提取字段"]
    STATUS_ORDER = ["草稿", "已验证", "已发布", "已失效"]
    ACTION_RULES = {"验证模板": "草稿", "发布模板": "已验证", "标记失效": "已发布"}
    NEGATIVE_ACTIONS = []

    def _source_dataset(self, template: dict[str, Any]) -> dict[str, Any] | None:
        dataset_id = template.get("来源数据集")
        if dataset_id is not None:
            dataset = store.find(SITE_DATASETS, int(dataset_id))
            if dataset is not None:
                return dataset
        for item in store.rows(SITE_DATASETS):
            if int(template.get("id", 0)) in item.get("template_ids", []):
                return item
        return None

    def collection_input(self, template_id: int) -> dict[str, Any]:
        """从可执行数据集生成采集输入；字段数、选择器数与审核版本必须对齐。"""
        template = store.find(self.MODULE, template_id)
        if template is None:
            return {"ok": False, "reason": "模板不存在"}
        if template.get("status") != "已发布":
            return {"ok": False, "reason": "仅已发布模板可以生成采集输入"}
        # 只有从“可执行的站点采集规则数据集”生成的模板才能进入采集；
        # 手工模板即使被标成已发布，也没有已审核的字段配对。
        if template.get("来源数据集") is None:
            return {"ok": False, "reason": "模板不是由站点采集规则数据集生成，不能直接进入采集"}

        dataset = self._source_dataset(template)
        if dataset is None:
            return {"ok": False, "reason": "模板没有可执行的站点采集规则数据集"}
        if dataset.get("status") != "已发布" or not dataset.get("executable", False):
            return {
                "ok": False,
                "reason": "来源数据集未发布或已暂挂，不能生成采集输入",
                "conflicts": dataset.get("conflicts", []),
                "impacted_fields": dataset.get("impacted_fields", []),
                "impacted_pages": dataset.get("impacted_pages", []),
            }
        if template.get("审核版本") and template.get("审核版本") != dataset.get("review_version"):
            return {"ok": False, "reason": "模板与数据集的审核版本不一致"}

        pairs = _parse_pairs(template)
        fields = split_values(template.get("提取字段"))
        selectors = split_values(template.get("字段选择器"))
        if not pairs:
            return {"ok": False, "reason": "模板缺少完整的字段与选择器配对"}
        if len(pairs) != len(fields) or len(selectors) != len(fields):
            return {
                "ok": False,
                "reason": (
                    f"字段配对不完整：字段 {len(fields)} 个，选择器 {len(selectors)} 个，"
                    f"有效配对 {len(pairs)} 个"
                ),
            }
        if sorted(pair["field"] for pair in pairs) != sorted(fields):
            return {"ok": False, "reason": "字段配对与提取字段不一致"}

        return {
            "ok": True,
            "template_id": template_id,
            "template_name": template.get("模板名称"),
            "dataset_id": dataset.get("id"),
            "review_version": dataset.get("review_version"),
            "review_receipt_id": dataset.get("receipt_id"),
            "task_id": dataset.get("task_id"),
            "site": dataset.get("site"),
            "layout": dataset.get("layout"),
            "sample_page": dataset.get("sample_page"),
            "pages": dataset.get("pages", []),
            "pairs": pairs,
            "field_count": len(pairs),
            "conflicts": dataset.get("conflicts", []),
        }

    def create_collection_batch(
        self,
        template_id: int,
        *,
        pages: list[str] | None = None,
        confirmed_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """创建采集批次：确认完整字段配对，并固化当次选择器快照。"""
        with _BATCH_LOCK:
            return self._create_collection_batch_locked(
                template_id, pages=pages, confirmed_fields=confirmed_fields
            )

    def _create_collection_batch_locked(
        self,
        template_id: int,
        *,
        pages: list[str] | None = None,
        confirmed_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        input_result = self.collection_input(template_id)
        if not input_result["ok"]:
            return input_result
        pairs = input_result["pairs"]
        expected_fields = [pair["field"] for pair in pairs]

        # 不传字段视为“整份确认”，字段仍然以模板配对为准；传了就必须与模板完全一致，
        # 不允许静默降级成部分字段。
        confirmed = (
            [field.strip() for field in confirmed_fields if field.strip()]
            if confirmed_fields is not None
            else list(expected_fields)
        )
        if sorted(confirmed) != sorted(expected_fields):
            missing = sorted(set(expected_fields) - set(confirmed))
            extra = sorted(set(confirmed) - set(expected_fields))
            detail = []
            if missing:
                detail.append(f"缺少字段：{'、'.join(missing)}")
            if extra:
                detail.append(f"多余字段：{'、'.join(extra)}")
            return {
                "ok": False,
                "reason": "采集批次只能确认完整的字段配对；" + "；".join(detail),
                "expected_fields": expected_fields,
            }

        batch_pages = [page.strip() for page in (pages or []) if page.strip()]
        if not batch_pages:
            batch_pages = list(input_result.get("pages") or [])
        if not batch_pages and input_result.get("sample_page"):
            batch_pages = [input_result["sample_page"]]
        if not batch_pages:
            return {"ok": False, "reason": "采集批次缺少可采集页面"}

        receipt = store.find(REVIEW_RECEIPTS, int(input_result["review_receipt_id"] or 0))
        candidate = next(
            (
                item for item in store.rows(PUBLISH_CANDIDATES)
                if int(item.get("receipt_id", 0)) == int(input_result["review_receipt_id"] or 0)
            ),
            None,
        )
        snapshot = [
            {
                "field": pair["field"],
                "selector": pair["selector"],
                "layout": input_result["layout"],
                "pages": batch_pages,
            }
            for pair in pairs
        ]
        batch = {
            "id": max(
                (int(row.get("id", 0)) for row in store.rows(COLLECTION_BATCHES)),
                default=0,
            ) + 1,
            "template_id": template_id,
            "dataset_id": input_result["dataset_id"],
            "task_id": input_result["task_id"],
            "candidate_id": candidate.get("id") if candidate else None,
            "review_version": input_result["review_version"],
            "review_receipt_id": input_result["review_receipt_id"],
            "site": input_result["site"],
            "layout": input_result["layout"],
            "pages": batch_pages,
            "confirmed_fields": expected_fields,
            "selector_snapshot": snapshot,
            "selector_snapshot_hash": _snapshot_hash(snapshot),
            "status": BATCH_CONFIRMED,
            "review_state_at_confirm": self._review_state(
                input_result["task_id"], candidate, receipt
            ),
            "records": [],
            "created_at": now_iso(),
        }
        store.rows(COLLECTION_BATCHES).append(batch)
        if candidate is not None:
            candidate.setdefault("batch_ids", []).append(batch["id"])
        return {"ok": True, "batch": self.batch_detail(batch)}

    def run_collection_batch(self, batch_id: int) -> dict[str, Any]:
        """运行采集批次：只使用批次确认的选择器快照，不在运行期回读模板。"""
        with _BATCH_LOCK:
            return self._run_collection_batch_locked(batch_id)

    def _run_collection_batch_locked(self, batch_id: int) -> dict[str, Any]:
        batch = store.find(COLLECTION_BATCHES, batch_id)
        if batch is None:
            return {"ok": False, "reason": "采集批次不存在"}
        if batch.get("status") == BATCH_DONE:
            return {"ok": True, "duplicate": True, "message": "该采集批次已完成", "batch": self.batch_detail(batch)}
        if batch.get("status") == BATCH_RUNNING:
            return {"ok": False, "reason": "采集批次正在运行"}

        snapshot = batch.get("selector_snapshot") or []
        confirmed = batch.get("confirmed_fields") or []
        snapshot_fields = [item.get("field") for item in snapshot]
        malformed = [
            item for item in snapshot
            if not str(item.get("selector") or "").strip() or not str(item.get("field") or "").strip()
        ]
        if (
            not snapshot
            or malformed
            or len(set(snapshot_fields)) != len(snapshot_fields)
            or sorted(snapshot_fields) != sorted(confirmed)
            or _snapshot_hash(snapshot) != batch.get("selector_snapshot_hash")
        ):
            batch["status"] = BATCH_FAILED
            batch["failure_reason"] = "采集批次的选择器快照不完整，已拒绝运行"
            return {"ok": False, "reason": batch["failure_reason"], "batch": self.batch_detail(batch)}
        if not batch.get("pages"):
            batch["status"] = BATCH_FAILED
            batch["failure_reason"] = "采集批次没有可采集页面"
            return {"ok": False, "reason": batch["failure_reason"], "batch": self.batch_detail(batch)}

        receipt = store.find(REVIEW_RECEIPTS, int(batch.get("review_receipt_id") or 0))
        candidate = store.find(PUBLISH_CANDIDATES, int(batch.get("candidate_id") or 0))
        batch["status"] = BATCH_RUNNING
        batch["started_at"] = now_iso()
        batch["review_state_at_start"] = self._review_state(batch.get("task_id"), candidate, receipt)

        records = []
        for page in batch["pages"]:
            for pair in snapshot:
                seed = hashlib.md5(f"{page}|{pair['field']}|{pair['selector']}".encode()).hexdigest()
                number = int(seed[:4], 16)
                matched = number % 7 != 0
                records.append({
                    "page": page,
                    "field": pair["field"],
                    "selector": pair["selector"],
                    "layout": pair.get("layout"),
                    "value": f"{pair['field']}样例#{number % 1000}" if matched else None,
                    "matched": matched,
                })

        batch["records"] = records
        batch["status"] = BATCH_DONE
        batch["completed_at"] = now_iso()
        batch["field_data"] = self._field_data(records)
        batch["match_ratio"] = round(
            sum(1 for record in records if record["matched"]) / len(records), 2
        ) if records else 0.0
        batch["review_state_at_complete"] = self._review_state(batch.get("task_id"), candidate, receipt)
        return {
            "ok": True,
            "duplicate": False,
            "message": (
                f"采集批次 {batch_id} 已完成，{len(batch['pages'])} 个页面，"
                f"{len(snapshot)} 个字段"
            ),
            "batch": self.batch_detail(batch),
        }

    def _review_state(
        self,
        task_id: Any,
        candidate: dict[str, Any] | None,
        receipt: dict[str, Any] | None,
    ) -> dict[str, Any]:
        task = store.find("extract_studio", int(task_id or 0))
        return {
            "task_id": task_id,
            "task_status": task.get("status") if task else None,
            "publish_status": task.get("发布状态") if task else None,
            "review_version": receipt.get("review_version") if receipt else None,
            "review_receipt_id": receipt.get("id") if receipt else None,
            "review_receipt_status": receipt.get("status") if receipt else None,
            "candidate_id": candidate.get("id") if candidate else None,
            "candidate_status": candidate.get("status") if candidate else None,
        }

    def _field_data(self, records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        data: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            data.setdefault(record["field"], []).append({
                "page": record["page"],
                "value": record["value"],
                "matched": record["matched"],
                "selector": record["selector"],
            })
        return data

    def batch_detail(self, batch: dict[str, Any]) -> dict[str, Any]:
        current_task = store.find("extract_studio", int(batch.get("task_id") or 0))
        current_receipt = store.find(REVIEW_RECEIPTS, int(batch.get("review_receipt_id") or 0))
        current_candidate = store.find(PUBLISH_CANDIDATES, int(batch.get("candidate_id") or 0))
        return {
            "id": batch.get("id"),
            "template_id": batch.get("template_id"),
            "dataset_id": batch.get("dataset_id"),
            "task_id": batch.get("task_id"),
            "candidate_id": batch.get("candidate_id"),
            "site": batch.get("site"),
            "layout": batch.get("layout"),
            "review_version": batch.get("review_version"),
            "review_receipt_id": batch.get("review_receipt_id"),
            "status": batch.get("status"),
            "pages": batch.get("pages", []),
            "confirmed_fields": batch.get("confirmed_fields", []),
            "selector_snapshot": batch.get("selector_snapshot", []),
            "selector_snapshot_hash": batch.get("selector_snapshot_hash"),
            "field_data": batch.get("field_data", {}),
            "match_ratio": batch.get("match_ratio"),
            "review_state_at_confirm": batch.get("review_state_at_confirm"),
            "review_state_at_start": batch.get("review_state_at_start"),
            "review_state_at_complete": batch.get("review_state_at_complete"),
            # 当前状态变化只作为参照，批次始终按自己的快照追溯。
            "current_review_state": {
                "task_status": current_task.get("status") if current_task else None,
                "review_version": current_receipt.get("review_version") if current_receipt else None,
                "receipt_status": current_receipt.get("status") if current_receipt else None,
                "candidate_status": current_candidate.get("status") if current_candidate else None,
            },
            "failure_reason": batch.get("failure_reason"),
            "created_at": batch.get("created_at"),
            "started_at": batch.get("started_at"),
            "completed_at": batch.get("completed_at"),
        }

    def list_batches(
        self, *, task_id: int | None = None, template_id: int | None = None
    ) -> list[dict[str, Any]]:
        batches = [
            batch for batch in store.rows(COLLECTION_BATCHES)
            if (task_id is None or int(batch.get("task_id", 0)) == task_id)
            and (template_id is None or int(batch.get("template_id", 0)) == template_id)
        ]
        return [self.batch_detail(batch) for batch in batches]

    def refresh_published_cache(self) -> dict[str, Any]:
        """发布后刷新已发布模板统计，供模板列表和指标复用。"""
        published = [row for row in store.rows(self.MODULE) if row.get("status") == "已发布"]
        self._published_cache = {int(row.get("id", 0)) for row in published}
        return {"published": len(published)}

    def compute_template_coverage(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        sites: dict[str, int] = {}
        total_selectors = 0
        total_fields = 0
        for row in rows:
            site = row.get("适用站点", "")
            sites[site] = sites.get(site, 0) + 1
            total_selectors += len(split_values(row.get("字段选择器")))
            total_fields += len(split_values(row.get("提取字段")))
        return {
            "template_count": len(rows),
            "site_count": len(sites),
            "total_selectors": total_selectors,
            "total_target_fields": total_fields,
            "published": sum(1 for r in rows if r.get("status") == "已发布"),
            "collection_batches": len(store.rows(COLLECTION_BATCHES)),
        }

    def get_published_templates(self) -> list[dict[str, Any]]:
        return [
            {
                "id": r.get("id"),
                "name": r.get("模板名称"),
                "site": r.get("适用站点"),
                "fields": r.get("提取字段"),
                "selectors": r.get("字段选择器"),
                "sample_page": r.get("采样页面"),
                "review_version": r.get("审核版本"),
            }
            for r in store.rows(self.MODULE)
            if r.get("status") == "已发布"
        ]

    def suggest_probes(self, template_id: int) -> dict[str, Any]:
        template = store.find(self.MODULE, template_id)
        if template is None:
            return {"ok": False, "reason": "模板不存在"}
        site = template.get("适用站点", "")
        selectors_str = template.get("字段选择器", "")
        sample_page = template.get("采样页面", "")
        selectors = split_values(selectors_str)

        probes = []
        for i, sel in enumerate(selectors):
            page = sample_page if sample_page else site
            probes.append({
                "探针名称": f"{template.get('模板名称')}-probe-{i+1}",
                "目标页面": page,
                "选择器列表": sel,
                "验证关键字": "",
                "采样频率": "daily",
                "超时秒数": "30",
                "重试次数": "3",
            })
        return {
            "ok": True,
            "template_id": template_id,
            "template_name": template.get("模板名称"),
            "suggested_probe_count": len(probes),
            "probes": probes,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["template_coverage"] = self.compute_template_coverage()
        return base
