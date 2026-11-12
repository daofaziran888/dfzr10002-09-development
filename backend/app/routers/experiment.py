"""分析实验管理接口：维护分析实验，覆盖启动实验、结束实验、关闭实验等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.experiment import ExperimentService

router = APIRouter(prefix="/api/experiment", tags=["分析实验管理"])

service = ExperimentService()

LIST_FIELDS = ["实验编号", "实验名称", "分析数据集", "分组字段", "目标度量", "基准区间", "显著阈值", "实验状态"]
STATUSES = ["草稿", "运行中", "已完成", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按实验编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已完成、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按实验编号与状态过滤分析实验管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条分析实验明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分析实验 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条分析实验，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="分析实验已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条分析实验执行启动实验、结束实验、关闭实验；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/sync-dependency", response_model=ActionResult)
def sync_dependency(entry_id: int) -> ActionResult:
    """同步实验实际引用的分析数据集画像版本，阻断状态随当前画像结论重算。"""
    entry, message = service.sync_dependency(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分析实验管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "experiment", "total": total, "items": items}
