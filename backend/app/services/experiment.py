"""分析实验管理业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "experiment"
REQUIRED_FIELDS = ["实验编号", "实验名称", "分析数据集"]
STATUS_ORDER = ["草稿", "运行中", "已完成", "已关闭"]
ACTION_RULES = {"启动实验": "运行中", "结束实验": "已完成", "关闭实验": "已关闭"}
NEGATIVE_ACTIONS = ["关闭实验"]


class ExperimentService:
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
            rows = [row for row in rows if keyword in str(row.get("实验编号", ""))]
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
            return None, f"分析实验 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分析实验管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "启动实验":
            blocked = self._dependency_block_reason(entry)
            if blocked:
                return None, blocked
        entry["status"] = target
        if "实验状态" in entry:
            entry["实验状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"分析实验已{action}"

    def sync_dependency(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """把实验引用的分析数据集指针同步到当前画像版本，阻断状态随版本重算。"""
        from app.services.quality import quality_service

        return quality_service.sync_reference(MODULE, entry_id)

    def _dependency_block_reason(self, entry: dict[str, Any]) -> str | None:
        """实验能否启动，与它实际引用的那份画像结论保持一致。"""
        code = str(entry.get("分析数据集") or "")
        if entry.get("依赖阻断原因"):
            return f"引用数据集 {code} 的画像 v{entry.get('依赖画像版本')} 未通过：{entry['依赖阻断原因']}"
        if not entry.get("依赖画像版本"):
            return f"引用数据集 {code} 尚未生成质量画像，请先在分析数据集模块提交画像"
        return None
