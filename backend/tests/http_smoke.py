"""端到端 HTTP 冒烟测试：用标准库 urllib 打真实 uvicorn 进程，验证整条链路。

运行方式（在 backend 目录）：
    .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 &
    .venv/bin/python -I tests/http_smoke.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
failures: list[str] = []


def call(method: str, path: str, body: object | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}{(' — ' + detail) if detail else ''}")
    if not condition:
        failures.append(name)


# 1. 批量画像 DS-001..DS-003
status, res = call("POST", "/api/dataset/quality/profiles", {
    "dataset_codes": ["DS-001", "DS-002", "DS-003"], "operator": "操作员甲",
})
check("批量画像 HTTP 200", status == 200, str(status))
check("合格数为 0、失败数为 3", res["qualified_count"] == 0 and res["failed_count"] == 3)
reasons_by_code = {
    item["dataset_code"]: item["profile"]["reasons"] for item in res["items"]
}
check("DS-001 指出字段缺口", any("字段缺口" in r and "settle_time" in r for r in reasons_by_code["DS-001"]))
check("DS-002 指出超限值", any("行数超限" in r and "965,000" in r and "800,000" in r for r in reasons_by_code["DS-002"]))
check("DS-002 指出空值", any("空值" in r for r in reasons_by_code["DS-002"]))
check("DS-003 指出过期时长", any("过期 24.0 小时" in r for r in reasons_by_code["DS-003"]))
check("保留来源表名", all(item["profile"]["snapshot"]["来源表名"] for item in res["items"]))
check("保留刷新时间", all(item["profile"]["snapshot"]["最近刷新"].startswith("20") for item in res["items"]))

# 2. 失败项不能启用（单条动作被闸门拦截）
status, res2 = call("POST", "/api/dataset/1/actions", {"values": {"action": "启用配置"}})
check("失败数据集单条启用被拦截", res2["ok"] is False and "未通过" in res2["message"], res2.get("message", ""))

# 3. 空提交 400
status, _ = call("POST", "/api/dataset/quality/profiles", {"dataset_codes": []})
check("空批量提交返回 400", status == 400)

# 4. 并发冲突：基于过期版本 v0 再提 DS-001
status, conflict = call("POST", "/api/dataset/quality/profiles", {
    "dataset_codes": ["DS-001"], "expected_versions": {"DS-001": 0},
})
check("过期版本提交记冲突", conflict["items"][0]["state"] == "conflict"
      and conflict["qualified_count"] == 0)
status, current = call("GET", "/api/dataset/quality/profiles?dataset_code=DS-001")
check("冲突后仍是 v1 单当前版本", len(current["items"]) == 1 and current["items"][0]["version"] == 1)

# 5. 基于最新版本重提 → v2，旧版本进历史
status, again = call("POST", "/api/dataset/quality/profiles", {
    "dataset_codes": ["DS-001"], "expected_versions": {"DS-001": 1},
})
check("基于最新版本重提生效为 v2", again["items"][0]["version"] == 2)
status, history = call("GET", "/api/dataset/quality/profiles/DS-001/history")
check("历史区含 v1/v2 且仅 v2 当前",
      [p["version"] for p in history["items"]] == [2, 1]
      and sum(1 for p in history["items"] if p["is_current"]) == 1)

# 6. DS-004 合格后可单独启用，且整组启用含失败项时失败项不被启用
status, p4 = call("POST", "/api/dataset/quality/profiles", {"dataset_codes": ["DS-004"]})
check("DS-004 画像合格", p4["items"][0]["qualified"] is True)
ds_id4 = 4
status, act = call("POST", f"/api/dataset/{ds_id4}/actions", {"values": {"action": "启用配置"}})
check("DS-004 合格后启用成功", act["ok"] is True and act["entry"]["status"] == "已启用")
status, batch_enable = call("POST", "/api/dataset/quality/enable-batch", {
    "dataset_codes": ["DS-001", "DS-004"],
})
check("整组启用失败项不放行", batch_enable["enabled_count"] == 1
      and batch_enable["all_enabled"] is False)

# 7. 下游同步：图表显示实际引用版本；运行中实验因 DS-002 不合格被自动关闭
status, charts = call("GET", "/api/chart")
chart1 = next(c for c in charts["items"] if c["数据源绑定"] == "DS-001")
check("图表记录引用画像版本", chart1["依赖画像版本"] == 2 and chart1["依赖结论"] == "不合格"
      and chart1["依赖阻断原因"])
status, chart_act = call("POST", "/api/chart/1/actions", {"values": {"action": "启用配置"}})
check("图表启用被依赖结论阻断", chart_act["ok"] is False and "未通过" in chart_act["message"])
status, experiments = call("GET", "/api/experiment")
exp2 = next(e for e in experiments["items"] if e["分析数据集"] == "DS-002")
check("运行中实验被自动关闭", exp2["status"] == "已关闭" and exp2["abnormal"] is True)

# 8. 手动同步接口把下游指针对齐当前画像版本；历史版本继续保留可追溯
#    （“修复字段后画像转合格、图表恢复启用”的完整恢复链路由单元测试
#     test_downstream_tracks_actual_version_and_recovers 覆盖）
status, sync = call("POST", "/api/chart/1/sync-dependency")
check("图表手动同步接口可用", sync["ok"] is True and "v2" in sync["message"])
status, history_after = call("GET", "/api/dataset/quality/profiles/DS-001/history")
check("旧资格结论保留在历史区", len(history_after["items"]) >= 2)
check("图表继续指向实际引用的当前版本",
      sync["entry"]["依赖画像版本"] == history_after["items"][0]["version"])

# 9. 不存在的数据集不算合格
status, missing = call("POST", "/api/dataset/quality/profiles", {"dataset_codes": ["DS-999"]})
check("不存在数据集标记 not_found 且不计合格",
      missing["items"][0]["state"] == "not_found" and missing["qualified_count"] == 0)

print()
if failures:
    print(f"冒烟测试失败 {len(failures)} 项：{failures}")
    sys.exit(1)
print("全部 HTTP 冒烟测试通过")
