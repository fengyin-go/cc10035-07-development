"""发布交接接口：审核回执、冲突报告、发布、数据集、模板采集输入与采集批次。

业务判断都在领域服务里，这里只做参数形状转换和错误映射。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.extract_studio import ExtractStudioService
from app.services.template_rule import TemplateRuleService

router = APIRouter(prefix="/api", tags=["发布交接"])

studio = ExtractStudioService()
templates = TemplateRuleService()


class ReviewSubmitPayload(BaseModel):
    remark: str | None = None


class PublishPayload(BaseModel):
    remark: str | None = None


class BatchPayload(BaseModel):
    pages: list[str] = Field(default_factory=list)
    confirmed_fields: list[str] | None = None


def _require_task(task_id: int) -> dict[str, Any]:
    task = studio.get_entry(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="抽取工坊任务不存在")
    return task


def _require_template(template_id: int) -> dict[str, Any]:
    template = templates.get_entry(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail=f"规则模板 {template_id} 不存在或已归档")
    return template


# ---------------------------------------------------------------- 抽取工坊侧
@router.get("/extract_studio/{task_id}/review-candidates")
def list_review_candidates(task_id: int) -> dict[str, Any]:
    _require_task(task_id)
    return {
        "ok": True,
        "items": [
            item for item in studio.list_candidates()
            if int(item.get("task_id", 0)) == task_id
        ],
    }


@router.post("/extract_studio/{task_id}/review-submit")
def submit_review(task_id: int, payload: ReviewSubmitPayload | None = None) -> dict[str, Any]:
    _require_task(task_id)
    result = studio.submit_review(task_id)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["reason"])
    return result


@router.get("/extract_studio/{task_id}/conflicts")
def conflict_report(task_id: int) -> dict[str, Any]:
    _require_task(task_id)
    return studio.conflict_report(task_id)


@router.get("/extract_studio/{task_id}/collection-rules")
def build_collection_rules(task_id: int) -> dict[str, Any]:
    _require_task(task_id)
    return studio.build_dataset(task_id)


@router.post("/extract_studio/{task_id}/publish")
def publish(task_id: int, payload: PublishPayload | None = None) -> dict[str, Any]:
    _require_task(task_id)
    result = studio.publish(task_id)
    if not result.get("task"):
        raise HTTPException(status_code=409, detail=result.get("reason") or "发布失败")
    return result


@router.get("/extract_studio/{task_id}/collection-version")
def collection_version(task_id: int) -> dict[str, Any]:
    _require_task(task_id)
    return studio.collection_version(task_id)


@router.get("/extract_studio/{task_id}/publish-trace")
def publish_trace(task_id: int) -> dict[str, Any]:
    _require_task(task_id)
    return studio.trace(task_id)


# ---------------------------------------------------------------- 模板规则 / 采集端
@router.get("/template_rule/{template_id}/collection-input")
def collection_input(template_id: int) -> dict[str, Any]:
    _require_template(template_id)
    result = templates.collection_input(template_id)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["reason"])
    return result


@router.get("/template_rule/{template_id}/collection-batches")
def list_collection_batches(template_id: int) -> dict[str, Any]:
    _require_template(template_id)
    return {"ok": True, "items": templates.list_batches(template_id=template_id)}


@router.post("/template_rule/{template_id}/collection-batches")
def create_collection_batch(template_id: int, payload: BatchPayload) -> dict[str, Any]:
    _require_template(template_id)
    result = templates.create_collection_batch(
        template_id,
        pages=payload.pages,
        confirmed_fields=payload.confirmed_fields,
    )
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["reason"])
    return result


@router.get("/collection_batches/{batch_id}")
def get_collection_batch(batch_id: int) -> dict[str, Any]:
    batch = next(
        (row for row in _batch_rows() if int(row.get("id", 0)) == batch_id),
        None,
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="采集批次不存在")
    return {"ok": True, "batch": templates.batch_detail(batch)}


@router.get("/collection_batches")
def list_all_batches(task_id: int | None = None) -> dict[str, Any]:
    return {"ok": True, "items": templates.list_batches(task_id=task_id)}


@router.post("/collection_batches/{batch_id}/run")
def run_collection_batch(batch_id: int) -> dict[str, Any]:
    result = templates.run_collection_batch(batch_id)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["reason"])
    return result


def _batch_rows() -> list[dict[str, Any]]:
    from app.services.handoff import COLLECTION_BATCHES
    from app.store import store

    return store.rows(COLLECTION_BATCHES)
