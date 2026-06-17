#!/usr/bin/env python3
"""诊断脚本：直接调用 GSSC /v1/manHour/task/page 接口，打印完整返回结构"""
import json, ssl, urllib.request
from datetime import datetime, timedelta

GSSC_API_BASE = "https://gssc-mh.gwm.cn/api"
GSSC_USERNAME = "GW00295409"
GSSC_PASSWORD = "5352Zhao123456."

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

def api_request(path, data=None, token=None):
    url = f"{GSSC_API_BASE}{path}"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://gssc-mh.gwm.cn",
        "Referer": "https://gssc-mh.gwm.cn/admin",
    }
    if token:
        headers["token"] = token
    body = json.dumps(data or {}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    resp = urllib.request.urlopen(req, timeout=30, context=ssl_ctx)
    return json.loads(resp.read().decode("utf-8"))

# 1. 登录
print("=" * 60)
print("[1] 登录...")
login_result = api_request("/v1/manHour/user/login", {
    "workerNum": GSSC_USERNAME,
    "password": GSSC_PASSWORD,
    "loginType": 1,
})
print(f"  code={login_result.get('code')}, msg={login_result.get('msg')}")
token = login_result.get("data")
if not token:
    print("  登录失败！退出")
    exit(1)
print(f"  token={token[:30]}...")

# 2. 测试所有可能的API路径
today = datetime.now()
one_year_ago = today - timedelta(days=365)
params = {
    "endDate": today.strftime("%Y-%m-%d"),
    "startDate": one_year_ago.strftime("%Y-%m-%d"),
    "taskName": "",
    "currentPage": 1,
    "pageSize": 5,
}

paths_to_test = [
    "/v1/manHour/task/personal/history/page",
    "/v1/manHour/task/personal/page",
    "/v1/manHour/task/page",
    "/v1/manHour/task/my/list",
    "/v1/manHour/task/project/page",
    "/v1/manHour/userHour/task/personal/page",
    "/v1/manHour/userHour/task/personal/history/page",
    "/v1/manHour/task/personal/list",
    "/v1/manHour/myTask/page",
]

print("\n" + "=" * 60)
print("[2] 遍历所有路径...")

for path in paths_to_test:
    try:
        result = api_request(path, dict(params), token)
        code = result.get("code")
        data = result.get("data", {})
        records = data.get("list", data.get("records", []))
        total = data.get("total", "?")

        if code == "00000" and records:
            print(f"\n{'='*60}")
            print(f"✅ {path}  成功! 共{total}条, 本页{len(records)}条")
            print(f"{'='*60}")
            r0 = records[0]
            # 打印全部字段名和值
            print(f"\n--- 第1条记录的全部字段 ({len(r0)} 个) ---")
            for k in sorted(r0.keys()):
                v = r0[k]
                val_str = json.dumps(v, ensure_ascii=False, default=str) if not isinstance(v, str) else v
                if len(val_str) > 80:
                    val_str = val_str[:80] + "..."
                print(f"  {k}: {val_str}")

            # 如果有多条，也看看第2条的结构是否一致
            if len(records) >= 2:
                r1 = records[1]
                keys0 = set(r0.keys())
                keys1 = set(r1.keys())
                if keys0 != keys1:
                    print(f"\n⚠️ 第2条记录字段不同! 差异: {keys1 - keys0}")

            # 打印完整JSON（截断）
            print(f"\n--- 第1条记录完整JSON ---")
            full_json = json.dumps(r0, ensure_ascii=False, default=str, indent=2)
            if len(full_json) > 2000:
                print(full_json[:2000] + "\n... (已截断)")
            else:
                print(full_json)

        elif code == "00000":
            print(f"  ⚠️ {path}  返回空 (total={total})")
        else:
            print(f"  ❌ {path}  code={code}, msg={result.get('msg')}")

    except Exception as e:
        print(f"  ❌ {path}  异常: {type(e).__name__}: {e}")

# 3. 再试试不带任何参数的请求（有些接口不需要日期范围）
print("\n" + "=" * 60)
print("[3] 尝试无参/极简参数...")
simple_paths = [
    ("/v1/manHour/task/list", {"currentPage": 1, "pageSize": 3}),
    ("/v1/manHour/task/personal/list", {}),
    ("/v1/manHour/myTask/list", {"currentPage": 1, "pageSize": 3}),
]
for path, sp_params in simple_paths:
    try:
        result = api_request(path, sp_params, token)
        code = result.get("code")
        data = result.get("data", {})
        records = data.get("list", data.get("records", []))
        if code == "00000" and records:
            print(f"\n✅ {path} 成功! {len(records)}条")
            print(json.dumps(records[0], ensure_ascii=False, default=str, indent=2)[:1000])
        else:
            print(f"  {path}: code={code}, records={len(records) if isinstance(records, list) else 'N/A'}")
    except Exception as e:
        print(f"  {path}: {e}")

print("\n" + "=" * 60)
print("诊断完成!")
