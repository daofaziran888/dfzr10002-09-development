"""数据集质量画像业务规则。

一条画像 = 某个分析数据集在某次提交时的可复用质量结论，校验四类硬性规则：
字段完整性（对照数据源默认库的表结构逐项比对）、行数上限、空值、刷新时效（48 小时）。

关键约束：
- 画像带版本号，同一数据集重复提交只保留最新一次有效结果，旧版本进入历史区；
- 批量提交逐条出结论，个别失败只标记该条，绝不计入合格数；
- 启用资格只对当前画像版本有效，未通过画像的数据集不能进入启用状态；
- 结论同步到引用该数据集的图表配置与分析实验，并随版本重算。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "dataset"
# 下划线前缀的内部表：不进运营概览的业务模块统计，只作为画像版本存储。
PROFILE_MODULE = "_quality_profile"
CHART_MODULE = "chart"
EXPERIMENT_MODULE = "experiment"
DATASOURCE_MODULE = "datasource"

# 刷新时效阈值：最近刷新超过该小时数即判“刷新过旧”。
STALE_AFTER_HOURS = 48.0

# 数据源默认库中各来源表的标准字段目录，字段完整性比对以此为准。
SOURCE_SCHEMA: dict[str, list[str]] = {
    "dwh_trade.fact_order_wide": [
        "order_id", "user_id", "order_status", "pay_amount", "pay_channel",
        "province", "city", "category_id", "sku_id", "order_time",
        "coupon_amount", "refund_amount", "settle_time",
    ],
    "dwh_profile.dim_user_profile": [
        "user_id", "gender", "age_band", "city_level", "member_level",
        "register_days", "active_days_30d", "last_login_time", "rfm_score",
    ],
    "dwh_marketing.fact_campaign_event": [
        "event_id", "campaign_id", "user_id", "event_type",
        "event_time", "channel", "conversion_flag",
    ],
    "dwh_trade.agg_member_repurchase": [
        "user_id", "member_level", "first_order_time", "last_order_time",
        "order_count_90d", "repurchase_amount_90d", "repurchase_rate",
    ],
}


def _parse_json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if not raw:
        return []
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        return [item.strip() for item in str(raw).split(",") if item.strip()]
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def _parse_json_dict(raw: Any) -> dict[str, int]:
    if isinstance(raw, dict):
        return {str(key): int(value or 0) for key, value in raw.items()}
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): int(value or 0) for key, value in data.items()}


def _parse_int(raw: Any) -> int | None:
    try:
        return int(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_number(value: int) -> str:
    return f"{value:,}"


class QualityProfileService:
    """质量画像的生成、版本管理、启用闸门与下游同步都收敛在这一个服务里。"""

    def __init__(self) -> None:
        # store.lock 是进程级 RLock，整批画像在锁内串行生成，杜绝并发互相覆盖。
        self._lock = store.lock

    # ------------------------------------------------------------------ 查询

    def current_profiles(self, dataset_code: str | None = None) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(PROFILE_MODULE) if row.get("is_current")]
        if dataset_code:
            rows = [row for row in rows if row.get("dataset_code") == dataset_code]
        return sorted(rows, key=lambda row: int(row.get("dataset_id", 0)))

    def history(self, dataset_code: str) -> list[dict[str, Any]]:
        rows = [
            row for row in store.rows(PROFILE_MODULE)
            if row.get("dataset_code") == dataset_code
        ]
        return sorted(rows, key=lambda row: int(row.get("version", 0)), reverse=True)

    def current_profile(self, dataset_code: str) -> dict[str, Any] | None:
        for row in store.rows(PROFILE_MODULE):
            if row.get("dataset_code") == dataset_code and row.get("is_current"):
                return row
        return None

    def current_version(self, dataset_code: str) -> int:
        profile = self.current_profile(dataset_code)
        return int(profile["version"]) if profile else 0

    def is_qualified(self, dataset_code: str) -> bool:
        profile = self.current_profile(dataset_code)
        return bool(profile and profile.get("qualified"))

    # ------------------------------------------------------------------ 画像

    def create_batch(
        self,
        dataset_codes: list[str],
        *,
        operator: str | None = None,
        expected_versions: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """整组提交画像：逐条校验、逐条给结论，失败项不影响同批其他数据集。"""
        expected_versions = expected_versions or {}
        # 同批内重复选择同一条数据集没有意义，按编号去重并保留首次出现顺序。
        ordered_codes: list[str] = []
        for code in dataset_codes:
            code = str(code or "").strip()
            if code and code not in ordered_codes:
                ordered_codes.append(code)

        items: list[dict[str, Any]] = []
        with self._lock:
            for code in ordered_codes:
                entry = self._find_dataset(code)
                if entry is None:
                    items.append({
                        "dataset_code": code,
                        "state": "not_found",
                        "qualified": False,
                        "message": f"数据集 {code} 不存在，未生成画像",
                        "profile": None,
                    })
                    continue

                current = self.current_profile(code)
                current_version = int(current["version"]) if current else 0
                if code in expected_versions and int(expected_versions[code]) != current_version:
                    # 乐观并发：两个操作员同时提交同一数据集时，过期的一次提交记冲突、不落库，
                    # 更不会把后到的有效结论顶掉。
                    items.append({
                        "dataset_code": code,
                        "state": "conflict",
                        "qualified": False,
                        "version": current_version,
                        "message": (
                            f"数据集 {code} 已存在更新的画像 v{current_version}，"
                            f"本次基于 v{expected_versions[code]} 的提交已冲突作废"
                        ),
                        "profile": None,
                    })
                    continue

                profile = self._build_profile(entry, operator or "值班管理员")
                if current is not None:
                    current["is_current"] = False
                    current["superseded_at"] = profile["submitted_at"]
                stored = store.insert(PROFILE_MODULE, profile)
                self._apply_eligibility(entry, stored)
                self._sync_downstream(stored)
                items.append({
                    "dataset_code": code,
                    "state": "profiled",
                    "qualified": bool(stored["qualified"]),
                    "version": stored["version"],
                    "message": "画像合格，可申请启用" if stored["qualified"] else "画像未通过，已阻断启用",
                    "profile": stored,
                })

        qualified_count = sum(1 for item in items if item["state"] == "profiled" and item["qualified"])
        failed_count = sum(1 for item in items if item["state"] == "profiled" and not item["qualified"])
        return {
            "total": len(items),
            "qualified_count": qualified_count,
            "failed_count": failed_count,
            "conflict_count": sum(1 for item in items if item["state"] == "conflict"),
            "not_found_count": sum(1 for item in items if item["state"] == "not_found"),
            "all_qualified": bool(items) and qualified_count == len(items),
            "items": items,
        }

    # ------------------------------------------------------------------ 启用

    def enable_batch(self, dataset_codes: list[str]) -> dict[str, Any]:
        """整组启用：全部数据集当前画像均合格才允许整组进入启用状态。"""
        ordered_codes: list[str] = []
        for code in dataset_codes:
            code = str(code or "").strip()
            if code and code not in ordered_codes:
                ordered_codes.append(code)

        items: list[dict[str, Any]] = []
        with self._lock:
            for code in ordered_codes:
                entry = self._find_dataset(code)
                if entry is None:
                    items.append({"dataset_code": code, "enabled": False,
                                  "message": f"数据集 {code} 不存在"})
                    continue
                profile = self.current_profile(code)
                if profile is None:
                    items.append({"dataset_code": code, "enabled": False,
                                  "message": f"数据集 {code} 尚未生成质量画像，不能启用"})
                    continue
                if not profile["qualified"]:
                    items.append({
                        "dataset_code": code, "enabled": False,
                        "message": f"数据集 {code} 当前画像 v{profile['version']} 未通过："
                                   f"{'；'.join(profile['reasons'])}",
                    })
                    continue
                entry["status"] = "已启用"
                entry["是否启用"] = "是"
                entry["pending"] = False
                entry["abnormal"] = False
                entry.pop("停用原因", None)
                items.append({"dataset_code": code, "enabled": True,
                              "message": f"数据集 {code} 已按画像 v{profile['version']} 启用"})

        enabled_count = sum(1 for item in items if item["enabled"])
        return {
            "total": len(items),
            "enabled_count": enabled_count,
            "all_enabled": bool(items) and enabled_count == len(items),
            "items": items,
        }

    def ensure_enable_allowed(self, entry: dict[str, Any]) -> str | None:
        """单条启用动作的闸门：返回 None 表示放行，否则返回可读的阻断原因。"""
        code = str(entry.get("数据集编号") or "")
        profile = self.current_profile(code)
        if profile is None:
            return f"数据集 {code} 尚未生成质量画像，请先提交画像再启用"
        if not profile["qualified"]:
            return f"数据集 {code} 当前画像 v{profile['version']} 未通过：{'；'.join(profile['reasons'])}"
        return None

    # -------------------------------------------------------------- 下游同步

    def sync_reference(self, module: str, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """图表/实验手动同步：把依赖指针移到所引用数据集的当前画像版本并重算阻断状态。"""
        entry = store.find(module, entry_id)
        if entry is None:
            label = "图表配置" if module == CHART_MODULE else "分析实验"
            return None, f"{label} {entry_id} 不存在或已归档"
        binding_field = "数据源绑定" if module == CHART_MODULE else "分析数据集"
        code = str(entry.get(binding_field) or "")
        with self._lock:
            profile = self.current_profile(code)
            if profile is None:
                return entry, f"引用的数据集 {code} 尚未生成有效画像，暂无法同步"
            self._apply_reference(entry, profile)
        return entry, f"已同步到数据集 {code} 的当前画像 v{profile['version']}"

    def _sync_downstream(self, profile: dict[str, Any]) -> None:
        """画像生成后把结论推给所有引用该数据集的图表与实验。"""
        code = str(profile["dataset_code"])
        for module, binding_field in (
            (CHART_MODULE, "数据源绑定"),
            (EXPERIMENT_MODULE, "分析数据集"),
        ):
            for entry in store.rows(module):
                if str(entry.get(binding_field) or "") == code:
                    self._apply_reference(entry, profile)

    def _apply_reference(self, entry: dict[str, Any], profile: dict[str, Any]) -> None:
        """重算单条下游记录的依赖指针、阻断标记，并让启用状态与依赖结果保持一致。"""
        entry["依赖画像版本"] = profile["version"]
        entry["依赖结论"] = "合格" if profile["qualified"] else "不合格"
        entry["依赖阻断原因"] = None if profile["qualified"] else "；".join(profile["reasons"])

        if profile["qualified"]:
            return

        module = None
        if "图表编号" in entry:
            module = CHART_MODULE
        elif "实验编号" in entry:
            module = EXPERIMENT_MODULE

        if module == CHART_MODULE and entry.get("status") == "已启用":
            # 依赖数据失去资格后，图表不允许继续保持启用。
            entry["status"] = "已停用"
            entry["pending"] = True
            entry["abnormal"] = True
        elif module == EXPERIMENT_MODULE and entry.get("status") == "运行中":
            # 运行中的实验失去数据依据，自动关闭并保留阻断原因供复盘。
            entry["status"] = "已关闭"
            entry["实验状态"] = "已关闭"
            entry["pending"] = False
            entry["abnormal"] = True

    # -------------------------------------------------------------- 内部工具

    def _find_dataset(self, dataset_code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("数据集编号") or "") == dataset_code:
                return row
        return None

    def _apply_eligibility(self, entry: dict[str, Any], profile: dict[str, Any]) -> None:
        """把画像结论回写到数据集主记录；不合格且处于启用态的数据集立即停用。"""
        entry["当前画像版本"] = profile["version"]
        entry["启用资格"] = f"{'合格' if profile['qualified'] else '不合格'}（v{profile['version']}）"
        if not profile["qualified"]:
            entry["abnormal"] = True
            entry["停用原因"] = "；".join(profile["reasons"])
            if entry.get("status") == "已启用" or entry.get("是否启用") == "是":
                entry["status"] = "已停用"
                entry["是否启用"] = "否"
                entry["pending"] = True
        else:
            entry.pop("停用原因", None)

    def _build_profile(self, entry: dict[str, Any], operator: str) -> dict[str, Any]:
        code = str(entry.get("数据集编号") or "")
        table = str(entry.get("来源表名") or "")
        refreshed_at = str(entry.get("最近刷新") or "")
        checks: list[dict[str, Any]] = []

        # 1) 字段完整性：数据源默认库表结构 vs 数据集字段列表，逐项比对。
        checks.append(self._check_fields(entry, table, code))
        # 2) 行数上限。
        checks.append(self._check_row_limit(entry))
        # 3) 空值。
        checks.append(self._check_nulls(entry))
        # 4) 刷新时效。
        checks.append(self._check_freshness(entry))

        qualified = all(bool(check["passed"]) for check in checks)
        reasons = [str(check["detail"]) for check in checks if not check["passed"]]
        submitted_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        return {
            "dataset_id": int(entry.get("id", 0)),
            "dataset_code": code,
            "dataset_name": str(entry.get("数据集名称") or ""),
            "version": self.current_version(code) + 1,
            "submitted_by": operator,
            "submitted_at": submitted_at,
            "is_current": True,
            "superseded_at": None,
            "qualified": qualified,
            "reasons": reasons,
            "checks": checks,
            # 保留画像时刻的来源表名与刷新时间，历史版本可追溯当时依据。
            "snapshot": {
                "来源表名": table,
                "最近刷新": refreshed_at,
                "刷新频率": str(entry.get("刷新频率") or ""),
            },
        }

    def _check_fields(self, entry: dict[str, Any], table: str, code: str) -> dict[str, Any]:
        actual = _parse_json_list(entry.get("字段列表"))
        expected = SOURCE_SCHEMA.get(table)
        datasource = self._find_datasource(table)
        source_desc = (
            f"数据源 {datasource.get('连接编号')} 默认库 {datasource.get('默认库名')}"
            if datasource else f"来源表 {table}"
        )
        if expected is None:
            return {
                "key": "field_completeness",
                "label": "字段完整性",
                "passed": False,
                "detail": f"字段完整性无法比对：{source_desc} 中未登记来源表 {table} 的标准结构",
                "missing_fields": [],
            }
        missing = [field for field in expected if field not in actual]
        passed = not missing
        if passed:
            detail = (
                f"字段完整性通过：{source_desc} 中表 {table} 的 {len(expected)} 个标准字段"
                f"在数据集 {code} 字段列表中全部齐备"
            )
        else:
            detail = (
                f"字段缺口：缺少 {len(missing)} 个字段（{'、'.join(missing)}）；"
                f"{source_desc} 中表 {table} 标准结构含 {len(expected)} 个字段，"
                f"数据集 {code} 字段列表仅 {len(actual)} 个"
            )
        return {
            "key": "field_completeness",
            "label": "字段完整性",
            "passed": passed,
            "detail": detail,
            "missing_fields": missing,
            "expected_field_count": len(expected),
            "actual_field_count": len(actual),
        }

    def _check_row_limit(self, entry: dict[str, Any]) -> dict[str, Any]:
        limit = _parse_int(entry.get("行数上限"))
        actual = _parse_int(entry.get("实际行数"))
        if limit is None or actual is None:
            return {
                "key": "row_limit", "label": "行数上限", "passed": False,
                "detail": "行数上限或实际行数缺失，无法完成超限校验",
            }
        passed = actual <= limit
        if passed:
            detail = f"行数上限通过：实际 {_format_number(actual)} 行，未超过上限 {_format_number(limit)} 行"
        else:
            detail = (
                f"行数超限：实际 {_format_number(actual)} 行，"
                f"超过上限 {_format_number(limit)} 行（超出 {_format_number(actual - limit)} 行）"
            )
        return {
            "key": "row_limit", "label": "行数上限", "passed": passed,
            "detail": detail, "actual_rows": actual, "row_limit": limit,
        }

    def _check_nulls(self, entry: dict[str, Any]) -> dict[str, Any]:
        null_stats = _parse_json_dict(entry.get("空值统计"))
        null_fields = {field: count for field, count in null_stats.items() if count > 0}
        if null_fields:
            detail = "空值异常：" + "、".join(
                f"字段 {field} 空值 {_format_number(count)} 条"
                for field, count in null_fields.items()
            )
            passed = False
        else:
            detail = "空值检查通过：字段列表内各字段均无空值记录"
            passed = True
        return {
            "key": "null_check", "label": "空值检查", "passed": passed,
            "detail": detail, "null_fields": null_fields,
        }

    def _check_freshness(self, entry: dict[str, Any]) -> dict[str, Any]:
        refreshed = _parse_time(entry.get("最近刷新"))
        if refreshed is None:
            return {
                "key": "freshness", "label": "刷新时效", "passed": False,
                "detail": "最近刷新时间缺失或格式无法识别，按刷新过旧处理",
                "stale_hours": None,
            }
        stale_hours = (datetime.now(timezone.utc) - refreshed).total_seconds() / 3600
        passed = stale_hours <= STALE_AFTER_HOURS
        if passed:
            detail = (
                f"刷新时效通过：最近刷新距今约 {stale_hours:.1f} 小时，"
                f"未超过 {STALE_AFTER_HOURS:.0f} 小时阈值"
            )
        else:
            detail = (
                f"刷新过旧：最近刷新距今约 {stale_hours:.1f} 小时，"
                f"超过 {STALE_AFTER_HOURS:.0f} 小时阈值（过期 {stale_hours - STALE_AFTER_HOURS:.1f} 小时）"
            )
        return {
            "key": "freshness", "label": "刷新时效", "passed": passed,
            "detail": detail, "stale_hours": round(stale_hours, 1),
        }

    def _find_datasource(self, table: str) -> dict[str, Any] | None:
        for row in store.rows(DATASOURCE_MODULE):
            default_db = str(row.get("默认库名") or "")
            if default_db and table.startswith(f"{default_db}."):
                return row
        return None


quality_service = QualityProfileService()
