"""质量画像与启用资格的业务规则测试（仅依赖标准库 unittest）。

覆盖需求中的关键场景：
1. 多选整组提交，逐条出结论，失败项不计入合格数；
2. DS-001 字段缺口 / DS-002 行数超限（+空值）/ DS-003 刷新过旧的具体原因；
3. 未通过画像不能启用，整组启用必须全部合格；
4. 重复画像版本递增、旧版本进历史、启用资格只对当前版本有效；
5. 并发提交同一数据集：过期版本冲突作废，只保留后一个有效结论；
6. 下游图表/实验同步版本指针，失去资格时自动停用/关闭。
"""
from __future__ import annotations

import threading
import unittest

from app.services.chart import ChartService
from app.services.dataset import DatasetService
from app.services.experiment import ExperimentService
from app.services.quality import PROFILE_MODULE, quality_service as q
from app.store import store


class QualityProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.dataset_svc = DatasetService()
        self.chart_svc = ChartService()
        self.experiment_svc = ExperimentService()

    def _profile(self, *codes: str, **kwargs):
        return q.create_batch(list(codes), **kwargs)

    # 1. 批量画像：逐条结论、具体原因、失败不计合格数
    def test_batch_profiles_per_item_reasons_and_counts(self) -> None:
        res = self._profile("DS-001", "DS-002", "DS-003", operator="操作员甲")
        self.assertEqual(res["total"], 3)
        self.assertEqual(res["qualified_count"], 0)
        self.assertEqual(res["failed_count"], 3)
        self.assertFalse(res["all_qualified"])

        by_code = {item["dataset_code"]: item for item in res["items"]}
        p1 = by_code["DS-001"]["profile"]
        field_check = next(c for c in p1["checks"] if c["key"] == "field_completeness")
        self.assertFalse(field_check["passed"])
        self.assertEqual(field_check["missing_fields"], ["settle_time"])
        self.assertIn("缺少 1 个字段", field_check["detail"])
        # 画像快照保留来源表名与刷新时间
        self.assertEqual(p1["snapshot"]["来源表名"], "dwh_trade.fact_order_wide")
        self.assertTrue(p1["snapshot"]["最近刷新"])

        p2 = by_code["DS-002"]["profile"]
        row_check = next(c for c in p2["checks"] if c["key"] == "row_limit")
        self.assertFalse(row_check["passed"])
        self.assertEqual(row_check["actual_rows"], 965000)
        self.assertEqual(row_check["row_limit"], 800000)
        self.assertIn("超出 165,000 行", row_check["detail"])
        null_check = next(c for c in p2["checks"] if c["key"] == "null_check")
        self.assertFalse(null_check["passed"])
        self.assertEqual(null_check["null_fields"], {"rfm_score": 1280})

        p3 = by_code["DS-003"]["profile"]
        fresh_check = next(c for c in p3["checks"] if c["key"] == "freshness")
        self.assertFalse(fresh_check["passed"])
        self.assertGreaterEqual(fresh_check["stale_hours"], 72.0)
        self.assertIn("过期 24.0 小时", fresh_check["detail"])

    # 2. 合格项仍可单独启用，失败项启用被拦截
    def test_qualified_one_can_enable_failed_one_blocked(self) -> None:
        self._profile("DS-001", "DS-002", "DS-003", "DS-004")
        # DS-004 四项全过
        self.assertTrue(q.is_qualified("DS-004"))
        entry4 = next(r for r in store.rows("dataset") if r["数据集编号"] == "DS-004")
        _, msg = self.dataset_svc.run_action(entry4["id"], "启用配置")
        self.assertEqual(entry4["status"], "已启用")
        self.assertEqual(entry4["是否启用"], "是")

        for code in ("DS-001", "DS-002", "DS-003"):
            entry = next(r for r in store.rows("dataset") if r["数据集编号"] == code)
            _, message = self.dataset_svc.run_action(entry["id"], "启用配置")
            self.assertNotEqual(entry["status"], "已启用")
            self.assertIn("未通过", message)

    # 3. 没有画像不能启用
    def test_no_profile_cannot_enable(self) -> None:
        entry = next(r for r in store.rows("dataset") if r["数据集编号"] == "DS-001")
        _, message = self.dataset_svc.run_action(entry["id"], "启用配置")
        self.assertNotEqual(entry["status"], "已启用")
        self.assertIn("尚未生成质量画像", message)

    # 4. 整组启用：组内一个不合格则整体不放行合格以外的记录
    def test_enable_batch_all_or_nothing(self) -> None:
        self._profile("DS-001", "DS-004")
        res = q.enable_batch(["DS-001", "DS-004"])
        self.assertEqual(res["enabled_count"], 1)
        self.assertFalse(res["all_enabled"])
        statuses = {
            r["数据集编号"]: r["status"] for r in store.rows("dataset")
        }
        self.assertEqual(statuses["DS-004"], "已启用")
        self.assertNotEqual(statuses["DS-001"], "已启用")

        # 全部合格时整组启用成功
        self._profile("DS-004")
        res2 = q.enable_batch(["DS-004"])
        self.assertTrue(res2["all_enabled"])

    # 5. 版本递增、历史区、资格只对当前版本有效
    def test_versioning_history_and_current_only_eligibility(self) -> None:
        self._profile("DS-001")
        self._profile("DS-001")
        p3 = self._profile("DS-001")["items"][0]["profile"]
        self.assertEqual(p3["version"], 3)
        history = q.history("DS-001")
        self.assertEqual([p["version"] for p in history], [3, 2, 1])
        self.assertEqual(len(q.current_profiles("DS-001")), 1)
        currents = [p for p in store.rows(PROFILE_MODULE) if p["is_current"]]
        self.assertEqual(len(currents), 1)
        self.assertTrue(all(not p["is_current"] for p in history[1:]))

        entry = next(r for r in store.rows("dataset") if r["数据集编号"] == "DS-001")
        self.assertEqual(entry["当前画像版本"], 3)
        self.assertIn("不合格", entry["启用资格"])

    # 6. 并发：过期 expected_version 冲突作废，后到结论保留
    def test_concurrent_conflict_only_latest_valid_kept(self) -> None:
        first = self._profile("DS-001", operator="操作员甲")
        self.assertEqual(first["items"][0]["version"], 1)

        # 操作员乙仍基于 v0 提交：冲突，不落库
        conflict = self._profile(
            "DS-001", operator="操作员乙", expected_versions={"DS-001": 0}
        )
        self.assertEqual(conflict["items"][0]["state"], "conflict")
        self.assertEqual(conflict["qualified_count"], 0)
        self.assertEqual(q.current_version("DS-001"), 1)

        # 基于最新版本 v1 重新提交才生效为 v2
        ok = self._profile(
            "DS-001", operator="操作员乙", expected_versions={"DS-001": 1}
        )
        self.assertEqual(ok["items"][0]["state"], "profiled")
        self.assertEqual(ok["items"][0]["version"], 2)
        self.assertEqual(q.current_version("DS-001"), 2)

    def test_true_concurrent_submissions_keep_single_current(self) -> None:
        barrier = threading.Barrier(4)

        def submit() -> None:
            barrier.wait()
            q.create_batch(["DS-004"])

        threads = [threading.Thread(target=submit) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        history = q.history("DS-004")
        self.assertEqual(len(history), 4)
        self.assertEqual(sum(1 for p in history if p["is_current"]), 1)
        self.assertEqual(q.current_version("DS-004"), 4)

    # 7. 下游同步：图表/实验记录引用版本，失去资格自动停用/关闭
    def test_downstream_sync_and_auto_blocking(self) -> None:
        chart = next(r for r in store.rows("chart") if r["数据源绑定"] == "DS-001")
        exp = next(r for r in store.rows("experiment") if r["分析数据集"] == "DS-002")

        # 画像生成即自动同步下游
        self._profile("DS-001", "DS-002")
        self.assertEqual(chart["依赖画像版本"], 1)
        self.assertEqual(chart["依赖结论"], "不合格")
        self.assertTrue(chart["依赖阻断原因"])
        # 图表未启用时不能启用
        _, msg = self.chart_svc.run_action(chart["id"], "启用配置")
        self.assertIn("未通过", msg)

        # 实验启动同样被挡
        _, msg_exp = self.experiment_svc.run_action(exp["id"], "启动实验")
        self.assertIn("未通过", msg_exp)

        # 实验初始为运行中，画像不合格应自动关闭
        self.assertEqual(exp["status"], "已关闭")
        self.assertEqual(exp["实验状态"], "已关闭")
        self.assertTrue(exp["abnormal"])

    def test_downstream_tracks_actual_version_and_recovers(self) -> None:
        # 先让 DS-001 仍不合格，图表保持阻断；手动同步接口可用
        self._profile("DS-001")
        chart = next(r for r in store.rows("chart") if r["数据源绑定"] == "DS-001")
        entry, message = self.chart_svc.sync_dependency(chart["id"])
        self.assertIsNotNone(entry)
        self.assertIn("v1", message)
        self.assertEqual(entry["依赖画像版本"], 1)

        # 修复 DS-001 字段缺口后重新画像：下游指针推进到新版本并解除阻断
        ds1 = next(r for r in store.rows("dataset") if r["数据集编号"] == "DS-001")
        import json
        fields = json.loads(ds1["字段列表"]) + ["settle_time"]
        ds1["字段列表"] = json.dumps(fields, ensure_ascii=False)
        self._profile("DS-001")
        self.assertEqual(chart["依赖画像版本"], 2)
        self.assertEqual(chart["依赖结论"], "合格")
        self.assertIsNone(chart["依赖阻断原因"])
        # 此时图表可以启用
        _, msg = self.chart_svc.run_action(chart["id"], "启用配置")
        self.assertEqual(chart["status"], "已启用")

        # 再次生成不合格画像（回退字段），已启用图表自动停用
        ds1["字段列表"] = json.dumps(fields[:-1], ensure_ascii=False)
        self._profile("DS-001")
        self.assertEqual(chart["status"], "已停用")
        self.assertTrue(chart["abnormal"])
        self.assertEqual(chart["依赖画像版本"], 3)

    # 8. 批量内个别失败：不存在的数据集不算合格、不影响其他条目
    def test_batch_partial_failure_not_counted_qualified(self) -> None:
        res = self._profile("DS-004", "DS-999")
        states = {i["dataset_code"]: i["state"] for i in res["items"]}
        self.assertEqual(states["DS-004"], "profiled")
        self.assertEqual(states["DS-999"], "not_found")
        self.assertEqual(res["qualified_count"], 1)
        self.assertEqual(res["not_found_count"], 1)
        self.assertFalse(res["all_qualified"])

    # 9. 画像快照保留来源表名与刷新时间
    def test_snapshot_keeps_table_and_refresh_time(self) -> None:
        res = self._profile("DS-002")
        snap = res["items"][0]["profile"]["snapshot"]
        self.assertEqual(snap["来源表名"], "dwh_profile.dim_user_profile")
        self.assertTrue(snap["最近刷新"].startswith("20"))
        self.assertEqual(snap["刷新频率"], "每日")


if __name__ == "__main__":
    unittest.main(verbosity=2)
