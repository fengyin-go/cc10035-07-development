"""业务路由汇总：发布交接的固定路径先注册，通用资源路由随后兜底。

资源路由含 /api/{module} 和 /api/{module}/{entry_id}，如果把通用路由放在
前面，/api/collection_batches 这类固定路径会被当成模块名而返回 404。
"""
from __future__ import annotations

from app.routers import handoff, resource

ROUTERS = [handoff, resource]
