"""分析数据集接口：维护数据集，覆盖提交校验、启用配置、停用配置等动作。

质量画像相关接口也挂在本路由下：画像的核算对象始终是分析数据集本身，
图表配置与分析实验只是下游引用方。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    ActionResult,
    EnableBatchPayload,
    EntryPayload,
    PageResult,
    ProfileBatchPayload,
)
from app.services.dataset import DatasetService
from app.services.quality import quality_service

router = APIRouter(prefix="/api/dataset", tags=["分析数据集"])

service = DatasetService()

LIST_FIELDS = ["数据集编号", "数据集名称", "来源表名", "字段列表", "行数上限", "刷新频率", "最近刷新", "是否启用"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按数据集编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按数据集编号与状态过滤分析数据集列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# ---------------------------------------------------------- 质量画像（固定路径，须在 /{entry_id} 之前）


@router.post("/quality/profiles")
def create_profiles(payload: ProfileBatchPayload) -> dict[str, Any]:
    """多选数据集整组提交质量画像，逐条返回结论。

    个别失败只影响该数据集本身：失败项不写入合格数，也不会获得启用资格；
    携带过期 expected_version 的提交记为冲突，后到的有效结论始终保留。
    """
    if not payload.dataset_codes:
        raise HTTPException(status_code=400, detail="请至少选择一个分析数据集再提交画像")
    return quality_service.create_batch(
        payload.dataset_codes,
        operator=payload.operator,
        expected_versions=payload.expected_versions,
    )


@router.get("/quality/profiles")
def list_profiles(dataset_code: str | None = None) -> dict[str, Any]:
    """列出各数据集当前生效的画像版本（历史版本走 history 接口查看）。"""
    items = quality_service.current_profiles(dataset_code)
    qualified = sum(1 for item in items if item.get("qualified"))
    return {
        "total": len(items),
        "qualified_count": qualified,
        "failed_count": len(items) - qualified,
        "items": items,
    }


@router.get("/quality/profiles/{dataset_code}/history")
def profile_history(dataset_code: str) -> dict[str, Any]:
    """单个数据集的画像历史区：新版本生成后旧资格结论全部沉淀在这里。"""
    items = quality_service.history(dataset_code)
    return {"dataset_code": dataset_code, "total": len(items), "items": items}


@router.post("/quality/enable-batch")
def enable_batch(payload: EnableBatchPayload) -> dict[str, Any]:
    """整组启用：组内全部数据集当前画像合格才会整体进入启用状态。"""
    if not payload.dataset_codes:
        raise HTTPException(status_code=400, detail="请至少选择一个分析数据集再整组启用")
    return quality_service.enable_batch(payload.dataset_codes)


# ------------------------------------------------------------------ 单条记录


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分析数据集清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dataset", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条数据集明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"数据集 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条数据集，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="数据集已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条数据集执行提交校验、启用配置、停用配置；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
