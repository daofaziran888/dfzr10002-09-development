"""可视化图表配置业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "chart"
REQUIRED_FIELDS = ["图表编号", "图表名称", "图表类型"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]


class ChartService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("图表编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"图表配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于可视化图表配置可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "启用配置":
            blocked = self._dependency_block_reason(entry)
            if blocked:
                return None, blocked
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"图表配置已{action}"

    def sync_dependency(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """把图表引用的数据集指针同步到当前画像版本，启用资格随版本变化。"""
        from app.services.quality import quality_service

        return quality_service.sync_reference(MODULE, entry_id)

    def _dependency_block_reason(self, entry: dict[str, Any]) -> str | None:
        """图表能否启用，与它实际引用的那份画像结论保持一致。"""
        code = str(entry.get("数据源绑定") or "")
        if entry.get("依赖阻断原因"):
            return f"引用数据集 {code} 的画像 v{entry.get('依赖画像版本')} 未通过：{entry['依赖阻断原因']}"
        if not entry.get("依赖画像版本"):
            return f"引用数据集 {code} 尚未生成质量画像，请先在分析数据集模块提交画像"
        return None
