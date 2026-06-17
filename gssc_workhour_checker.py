#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GSSC工时系统 - 未写工时人员检测 + 钉钉通知 + 个人加班时间查询
功能：
  1. 自动登录工时系统，查询本月未填写工时的人员，发送钉钉群提醒
  2. 查询ESS考勤系统，计算个人本月加班时间

API接口说明：
  GSSC工时系统:
  - 登录: POST https://gssc-mh-api.gwm.cn/api/v1/manHour/user/login
    参数: {"workerNum": "工号", "password": "密码", "loginType": 1}
    返回: {"code": "00000", "data": "JWT_TOKEN"}
  - 工时统计: POST https://gssc-mh-api.gwm.cn/api/v1/manHour/statistic/admin/all
    参数: {"startDate": "YYYY-MM-DD", "endDate": "YYYY-MM-DD", ...}
    Header: {"token": "JWT_TOKEN"}
    返回: [{workerNum, workerName, dateHoursList: [{date, hours}], orgName}, ...]
  - 填写率: POST /v1/manHour/statistic/admin/hours/fill/percent/statistic
  - 审批率: POST /v1/manHour/statistic/admin/hours/check/percent/statistic

  ESS考勤系统:
  - 登录: POST http://ess.gwm.cn/newAuth/auth/login/self-service
    参数: {"username": AES加密(工号), "password": AES加密(密码)}
    返回: {"success": true, "data": {"access_token": "Bearer ..."}}
  - 人员信息: GET /gwm-hr-masterData-center/employeeSelfLogin/getPersonByPersonNumber4Self
  - 出勤统计: POST /gwm-hr-attendance-center/ding/selfAttendanceStatistical/getAttendanceStatistical4PC
    参数: perId=人员ID&month=YYYYMM
    返回: {code:200, data:{dateList:[{date,onWorkTimeCard,offWorkTimeCard,overtimeHour,...}]}}
"""

import ssl
import json
import sys
import time
import hmac
import hashlib
import base64
from urllib.request import Request, urlopen, ProxyHandler, build_opener
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlparse, parse_qs, urlencode, urlunparse
from pathlib import Path
import socket

# 尝试导入 pycryptodome（ESS考勤系统AES加密需要）
try:
    from Crypto.Cipher import AES as _AES
    from Crypto.Util.Padding import pad as _pad
    HAS_PYCRYPTODOME = True
except ImportError:
    HAS_PYCRYPTODOME = False

# ============ 配置区（请修改为你的信息） ============
GSSC_API_BASE = "https://gssc-mh-api.gwm.cn/api"
GSSC_USERNAME = "GW00295409"       # 工号
GSSC_PASSWORD = "5352Zhao123456."  # 密码

DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=51d07001c23c18f9a9c43bb3e0ecc548c34982babe38750b6f15642a4d006a75"
DINGTALK_SECRET = "SEC96d2f04fac992f3172e5375c93cfa562b316553eb2f66127a305def812746c19"

#DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=e76236cb296e77c44850085b04fe3fb82a34fdde89f4e5b99d285dd61b594b67"
#DINGTALK_SECRET = "SECac14b3fc70cfdbfa6f876cd5e7333dfb81773660c20727866a0569c82b9749f3"


# 只统计这些工号的人员，其他人过滤掉
ALLOWED_WORKER_NUMS = {
    "98702045",
    "GW00295334", "GW00295367", "GW00295369", "GW00295404", "GW00295409",
    "GW00295410", "GW00295411", "GW00295462", "GW00295477", "GW00295491",
    "GW00295498", "GW00295527", "GW00295531", "GW00295541", "GW00295623",
    "GW00295641", "GW00295666", "GW00295677", "GW00295679", "GW00295710",
    "GW00295718", "GW00295806", "GW00303617", "GW00305867", "GW00321704",
    "GW00356539", "GW00361895", "98702318",
}

# 工号 → 钉钉手机号映射（有映射的工号会用手机号 @，更可靠）
WORKER_TO_MOBILE = {
    "GW00356539": "18620154457",
}

# 输出目录（保存结果JSON）
OUTPUT_DIR = str(Path.home() / "Desktop" / "jira_cpm" / "gssc_output")

# ============ ESS 考勤系统配置 ============
ESS_API_BASE = "http://ess.gwm.cn"
ESS_AES_KEY = "1234567890654321"   # AES加密密钥（来自前端JS）
ESS_AES_IV  = "1234567890654321"   # AES加密IV（来自前端JS）
# 加班计算规则：标准工时=9小时（8小时工作+1小时午休），超出部分为加班
# 加班最小单位：0.5小时

# ============ SSL 配置 ============
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE


def api_request(path, data=None, token=None):
    """向GSSC API发起POST请求"""
    url = f"{GSSC_API_BASE}{path}"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://gssc-mh.gwm.cn",
        "Referer": "https://gssc-mh.gwm.cn/admin",
    }
    if token:
        headers["token"] = token

    body = json.dumps(data or {}).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    resp = urlopen(req, timeout=30, context=ssl_ctx)
    return json.loads(resp.read().decode("utf-8"))


def login():
    """登录GSSC工时系统，返回JWT Token"""
    print(f"[登录] 使用工号 {GSSC_USERNAME} 登录...")
    try:
        result = api_request("/v1/manHour/user/login", {
            "workerNum": GSSC_USERNAME,
            "password": GSSC_PASSWORD,
            "loginType": 1,
        })
        if result.get("code") == "00000":
            token = result.get("data")
            print(f"[登录] 成功！Token: {token[:30]}...")
            return token
        else:
            print(f"[登录] 失败: {result.get('msg', '未知错误')}")
            return None
    except (HTTPError, URLError) as e:
        print(f"[登录] 请求异常: {e}")
        return None


def get_user_info(token):
    """获取当前登录用户信息"""
    try:
        result = api_request("/v1/manHour/user/info", {}, token)
        if result.get("code") == "00000":
            return result.get("data", {})
    except (HTTPError, URLError) as e:
        print(f"[用户信息] 请求失败: {e}")
    return {}


def get_workhour_stats(token, start_date, end_date):
    """获取管理员工时统计（全部人员每日工时）"""
    params = {
        "currentPage": 1,
        "pageSize": 500,
        "startDate": start_date,
        "endDate": end_date,
    }
    try:
        result = api_request("/v1/manHour/statistic/admin/all", params, token)
        if result.get("code") == "00000":
            return result.get("data", [])
        print(f"[错误] 获取工时统计失败: {result.get('msg')}")
    except (HTTPError, URLError) as e:
        print(f"[错误] 获取工时统计请求异常: {e}")
    return []


def get_fill_percent(token, start_date, end_date):
    """获取工时填写率统计"""
    params = {
        "currentPage": 1,
        "pageSize": 200,
        "startDate": start_date,
        "endDate": end_date,
    }
    try:
        result = api_request("/v1/manHour/statistic/admin/hours/fill/percent/statistic", params, token)
        if result.get("code") == "00000":
            return result.get("data", [])
    except (HTTPError, URLError) as e:
        print(f"[填写率] 请求失败: {e}")
    return []


def get_check_percent(token, start_date, end_date):
    """获取工时审批率统计（科室审批率）
    API 路径来自 GSSC 前端 JS bundle: group/check/percent/statistic
    """
    params = {
        "currentPage": 1,
        "pageSize": 200,
        "startDate": start_date,
        "endDate": end_date,
    }
    path = "/v1/manHour/statistic/admin/hours/group/check/percent/statistic"
    try:
        result = api_request(path, params, token)
        if result.get("code") == "00000":
            print(f"[审批率] 命中: {path}")
            return result.get("data", [])
    except Exception as e:
        print(f"[审批率] 失败: {e}")
    print("[审批率] 查询失败，跳过")
    return []


# ============ GSSC 工时填写 API（从JS bundle逆向） ============

def gssc_get_projects(token):
    """获取项目列表
    API: POST /v1/manHour/project/list
    返回: [{projectId, projectName, status, dataFrom}, ...]
    """
    try:
        result = api_request("/v1/manHour/project/list", {}, token)
        if result.get("code") == "00000":
            return result.get("data", [])
        print(f"[GSSC项目] 获取失败: {result.get('msg')}")
    except (HTTPError, URLError) as e:
        print(f"[GSSC项目] 请求失败: {e}")
    return []


def gssc_get_task_types(token):
    """获取任务类型枚举
    API: GET /v1/manHour/api/v1/enums/getList?type=taskType
    返回: [{code, value}, ...]  IR=10, CR=20, JIRA_CR=21, 缺陷=30, 需求=40, 请假=50
    """
    url = f"{GSSC_API_BASE}/v1/manHour/api/v1/enums/getList?type=taskType"
    headers = {"token": token, "Accept": "application/json"}
    req = Request(url, headers=headers, method="GET")
    try:
        resp = urlopen(req, timeout=15, context=ssl_ctx)
        result = json.loads(resp.read().decode("utf-8"))
        if result.get("code") == "00000":
            return result.get("data", [])
    except Exception as e:
        print(f"[任务类型] 获取失败: {e}")
    return [{"code": "10", "value": "IR"}, {"code": "20", "value": "CR"},
            {"code": "30", "value": "缺陷"}, {"code": "40", "value": "需求"}]


def gssc_get_calendar(token, start_date, end_date):
    """获取工时日历数据
    API: POST /v1/manHour/userHour/calender
    返回: [{date, hours, hoursText, isHoliday}, ...]
    """
    try:
        result = api_request("/v1/manHour/userHour/calender",
                              {"startDate": start_date, "endDate": end_date}, token)
        if result.get("code") == "00000":
            return result.get("data", [])
    except (HTTPError, URLError) as e:
        print(f"  [日历] 请求失败: {e}")
    return []


def gssc_get_personal_tasks(token, project_id=None):
    """获取个人任务列表（用于获取准确的 projectType / taskId）
    尝试多个API路径，首个成功即返回
    返回: [{id, taskId, taskName, projectId, projectType, projectTypeName,
            orgId, orgName, taskTypeParamId, taskTypeParamDetail, ...}, ...]
    """
    from datetime import datetime, timedelta
    today = datetime.now()
    one_year_ago = today - timedelta(days=365)
    params = {
        "endDate": today.strftime("%Y-%m-%d"),
        "startDate": one_year_ago.strftime("%Y-%m-%d"),
        "taskTypeParamDetail": "",
        "taskName": "",
        "taskTypeList": None,
        "currentPage": 1,
        "pageSize": 200,
    }

    # 尝试多种API路径（不同版本GSSC可能使用不同端点）
    api_paths = [
        "/v1/manHour/task/personal/history/page",
        "/v1/manHour/task/personal/page",
        "/v1/manHour/task/page",
        "/v1/manHour/task/my/list",
        "/v1/manHour/task/project/page",
        "/v1/manHour/userHour/task/personal/page",
        "/v1/manHour/userHour/task/personal/history/page",
        "/v1/manHour/task/personal/list",
        "/v1/manHour/myTask/page",
        "/v1/manHour/myTask/personal/history/page",
    ]

    # 先尝试带项目过滤的查询
    for path in api_paths:
        _params = dict(params)
        _params["statusFlags"] = ["ONGOING_TAB"]
        if project_id:
            _params["projectIds"] = [str(project_id)]
        try:
            result = api_request(path, _params, token)
        except (HTTPError, URLError) as e:
            print(f"  [个人任务] {path} 请求失败: {e}")
            continue
        if result.get("code") == "00000":
            data = result.get("data", {})
            tasks = data.get("list", data.get("records", []))
            if tasks:
                print(f"  [个人任务] 精确查询命中 {len(tasks)} 条 (path={path})")
                # DEBUG: 打印第一条数据的字段名和值，帮助定位 taskId 真实字段
                t0 = tasks[0]
                print(f"  [调试] 第一条数据字段: {sorted(t0.keys())}")
                print(f"  [调试] 第一条数据值: {json.dumps({k: t0[k] for k in sorted(t0.keys())}, ensure_ascii=False, default=str)[:500]}")
                return tasks

    # 放宽条件：不指定状态过滤
    for path in api_paths:
        _params = dict(params)
        _params["statusFlags"] = None
        if project_id:
            _params["projectIds"] = [str(project_id)]
        try:
            result = api_request(path, _params, token)
        except (HTTPError, URLError) as e:
            print(f"  [个人任务] {path} 请求失败: {e}")
            continue
        if result.get("code") == "00000":
            data = result.get("data", {})
            all_tasks = data.get("list", data.get("records", []))
            if all_tasks:
                if project_id:
                    filtered = [t for t in all_tasks
                                if str(t.get("projectId")) == str(project_id)]
                    if filtered:
                        print(f"  [个人任务] 宽松+筛选命中 {len(filtered)} 条 (path={path})")
                        return filtered
                    else:
                        print(f"  [个人任务] 宽松查询有 {len(all_tasks)} 条但未匹配项目{project_id}")
                        return all_tasks
                print(f"  [个人任务] 宽松查询命中 {len(all_tasks)} 条 (path={path})")
                return all_tasks

    # 最后尝试不带任何过滤条件
    for path in api_paths:
        _params = dict(params)
        _params["statusFlags"] = None
        _params["projectIds"] = None
        try:
            result = api_request(path, _params, token)
        except (HTTPError, URLError) as e:
            print(f"  [个人任务] {path} 请求失败: {e}")
            continue
        if result.get("code") == "00000":
            data = result.get("data", {})
            raw = data.get("list", data.get("records", []))
            if raw:
                print(f"  [个人任务] 无过滤查询命中 {len(raw)} 条 (path={path})")
                return raw

    print(f"  [个人任务] 所有API路径和查询条件均无结果")
    return []


def gssc_find_taskId_from_history(token, project_id, date_range=30):
    """从已填写的工时记录中查找该项目的taskId（兜底方案）
    遍历最近N天的已填写工时，找到匹配项目的记录，提取其taskId
    """
    from datetime import datetime, timedelta
    today = datetime.now()
    found = {}
    _tid_candidates = ["taskId", "id", "task_id", "taskid", "manHourTaskId", "taskCode"]
    for i in range(date_range):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        tasks = gssc_get_tasks_by_date(token, d)
        for t in tasks:
            if str(t.get("projectId")) == str(project_id):
                # 尝试多种字段名
                tid = None
                for _f in _tid_candidates:
                    if _f in t and t[_f] is not None and t[_f] != 0:
                        tid = t[_f]
                        break
                if tid and tid not in found:
                    found[tid] = t
                    print(f"  [历史taskId] 在 {d} 找到 taskId={tid}, taskName={t.get('taskName', t.get('name'))}")
    return found


def _map_datafrom_to_type(data_from):
    """将项目 dataFrom 字段值映射为 GSSC API 期望的数字类型码
    前端 project select 的 data-type 属性存的是 dataFrom (MH/RD 等)，
    但 batchAdd API 的 projectType 期望数字类型码。
    """
    # GSSC 项目类型枚举（常见值，具体以系统为准）
    # MH = 工时项目(通用) → 10, RD = 研发项目 → 15
    mapping = {
        "MH": 10,
        "RD": 15,
        "10": 10,
        "15": 15,
        "20": 20,
    }
    val = str(data_from).upper() if data_from else "MH"
    if val in mapping:
        return mapping[val]
    # 尝试直接解析为数字
    try:
        return int(data_from)
    except (ValueError, TypeError):
        pass
    # 默认返回工时项目类型
    return 10


def gssc_get_project_tasks(token, project_id, org_id=None):
    """获取指定项目+科室的任务列表（供前端下拉选择）
    API: POST /v1/manHour/task/page
    返回: [{taskId, taskName, taskLevelOneId, taskLevelOneName, taskLevelTwoId, taskLevelTwoName, orgId, orgName}, ...]
    """
    params = {
        "projectIds": [int(project_id) if str(project_id).isdigit() else project_id],
        "currentPage": 1,
        "pageSize": 500,
    }
    if org_id:
        params["orgIds"] = [str(org_id)]
    try:
        result = api_request("/v1/manHour/task/page", params, token)
        if result.get("code") == "00000":
            raw = result.get("data", {}).get("list", [])
            # 去重：同一 taskName 只保留一条
            seen = set()
            tasks = []
            for t in raw:
                name = t.get("name", "")
                if name and name not in seen:
                    seen.add(name)
                    tasks.append({
                        "taskId": t.get("id"),
                        "taskName": name,
                        "taskLevelOneId": t.get("taskLevelOneId"),
                        "taskLevelOneName": t.get("taskLevelOneName", ""),
                        "taskLevelTwoId": t.get("taskLevelTwoId"),
                        "taskLevelTwoName": t.get("taskLevelTwoName", ""),
                        "orgId": t.get("orgId"),
                        "orgName": t.get("orgName", ""),
                    })
            # 按 taskName 排序
            tasks.sort(key=lambda x: x["taskName"])
            return tasks
        print(f"[项目任务] 获取失败: {result.get('msg')}")
    except (HTTPError, URLError) as e:
        print(f"[项目任务] 请求失败: {e}")
    return []


def gssc_get_type_detail_history(token, project_id, task_name):
    """获取用户历史填写中 typeDetail 的候选值
    只查最近2天的 userHourTask 记录，快速返回
    """
    from datetime import datetime, timedelta

    values = set()
    for i in range(2):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            result = api_request("/v1/manHour/userHourTask/listByDate", {"date": d}, token)
            if result.get("code") == "00000":
                for t in result.get("data", []):
                    if (str(t.get("projectId")) == str(project_id) and
                            t.get("taskName") == task_name and
                            t.get("taskTypeParamDetail")):
                        values.add(t.get("taskTypeParamDetail"))
        except Exception:
            pass

    return sorted(values)


def gssc_get_type_detail_options(token, project_id, task_type):
    """获取类型详细的下拉选项（根据任务类型调用不同API）

    task_type: "10"=IR, "20"=CR, "21"=JIRA_CR, "30"=缺陷, "40"=需求
    返回: [{value: "显示文本", detail: "taskTypeParamDetail", paramId: "taskTypeParamId"}, ...]
    """
    import concurrent.futures

    project_id_str = str(project_id)
    options = []

    if task_type == "10":
        # IR列表: task/ir/page -> id, title, typeLv1Name, typeLv2Name
        try:
            result = api_request("/v1/manHour/task/ir/page",
                                 {"currentPage": 1, "pageSize": 200}, token)
            if result.get("code") == "00000":
                for item in result.get("data", {}).get("list", []):
                    # 过滤同项目的IR
                    rel_projects = item.get("projectModelRelVOList") or []
                    rel_ids = [str(r.get("projectId")) for r in rel_projects]
                    if rel_ids and project_id_str not in rel_ids:
                        continue
                    ir_id = item.get("id", "")
                    title = item.get("title", "")
                    lv1 = item.get("typeLv1Name", "")
                    lv2 = item.get("typeLv2Name", "")
                    label = f"{title}【ID：{ir_id}】（{lv1}"
                    if lv2:
                        label += f"/{lv2}"
                    label += "）"
                    detail = f"{title}【ID：{ir_id}】"
                    options.append({"value": label, "detail": detail, "paramId": str(ir_id)})
        except Exception as e:
            print(f"[IR列表] 请求失败: {e}")

    elif task_type == "20":
        # CR列表: task/cr/page -> id, crName, jiraCrOrder
        try:
            result = api_request("/v1/manHour/task/cr/page",
                                 {"currentPage": 1, "pageSize": 500}, token)
            if result.get("code") == "00000":
                for item in result.get("data", {}).get("list", []):
                    rel_projects = item.get("projectModelRelVOList") or []
                    rel_ids = [str(r.get("projectId")) for r in rel_projects]
                    if rel_ids and project_id_str not in rel_ids:
                        continue
                    cr_id = item.get("id", "")
                    cr_name = item.get("crName", "")
                    order = item.get("jiraCrOrder", "")
                    label = f"{order + ' ' if order else ''}{cr_name}【ID：{cr_id}】"
                    options.append({"value": label, "detail": label, "paramId": str(cr_id)})
        except Exception as e:
            print(f"[CR列表] 请求失败: {e}")

    elif task_type == "21":
        # JIRA_CR: 太小众，用简单搜索即可
        options.append({"value": "— JIRA_CR 请手动输入 —", "detail": "", "paramId": ""})

    elif task_type == "30":
        # 缺陷: 自由输入
        options.append({"value": "— 缺陷ID请手动输入 —", "detail": "", "paramId": ""})

    elif task_type == "40":
        # 需求: productLine/feature/epic lists
        for api_path, label_key, id_key in [
            ("/v1/manHour/task/requirementRelationInfo/productLine/list", "productLineName", "productLineId"),
        ]:
            try:
                result = api_request(api_path, {}, token)
                if result.get("code") == "00000" and result.get("data"):
                    for item in result.get("data", []):
                        name = item.get(label_key, "")
                        pid = item.get(id_key, "")
                        if name:
                            options.append({"value": name, "detail": name, "paramId": str(pid)})
            except Exception:
                pass

    return options


def gssc_get_tasks_by_date(token, date):
    """获取某天的任务列表（已填写的工时）
    API: POST /v1/manHour/userHourTask/listByDate
    返回: [task_object, ...]  每个包含 id/date/taskId/hours/taskName/projectId/projectName/...
    """
    try:
        result = api_request("/v1/manHour/userHourTask/listByDate", {"date": date}, token)
        if result.get("code") == "00000":
            return result.get("data", [])
    except (HTTPError, URLError) as e:
        print(f"  [按日任务] {date} 请求失败: {e}")
    return []


def gssc_batch_fill_hours(token, config):
    """批量填写工时

    config 参数:
      - project_id: 项目ID（必填）
      - project_name: 项目名称（必填）
      - project_type: 项目类型编码，如 "10"（技术通用项目）（必填）
      - project_type_name: 项目类型名称（必填）
      - task_type: 任务类型编码，如 "10"=IR, "20"=CR（必填）
      - task_name: 任务名称（必填）
      - job_desc: 工作描述（可选）
      - start_date: 起始日期 YYYY-MM-DD（必填）
      - end_date: 结束日期 YYYY-MM-DD（必填）
      - hours_per_day: 每天工时数，默认 8.0
      - skip_filled: 是否跳过已填写的日期，默认 True
      - skip_weekend: 是否跳过周末，默认 True
      - dry_run: 仅预览不提交，默认 False

    返回: {ok: bool, filled: [...], skipped: [...], errors: [...], summary: str}
    """
    from datetime import datetime, timedelta

    project_id = config.get("project_id", "")
    project_name = config.get("project_name", "")
    project_type = config.get("project_type", "")
    project_type_name = config.get("project_type_name", "")
    task_type = config.get("task_type", "10")
    task_name = config.get("task_name", "")
    job_desc = config.get("job_desc", task_name or "批量填写")
    start_date_str = config.get("start_date", "")
    end_date_str = config.get("end_date", "")
    hours_per_day = float(config.get("hours_per_day", 8))
    skip_filled = config.get("skip_filled", True)
    skip_weekend = config.get("skip_weekend", True)
    dry_run = config.get("dry_run", False)

    # 验证必填参数
    missing = []
    for k, label in [("project_id","项目"), ("project_name","项目名称"),
                     ("start_date","起始日期"), ("end_date","结束日期"),
                     ("task_name","任务名称")]:
        if not config.get(k):
            missing.append(label)
    if missing:
        return {"ok": False, "error": f"缺少必填参数: {', '.join(missing)}"}

    # 解析日期范围
    try:
        start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
    except ValueError:
        return {"ok": False, "error": "日期格式错误，请使用YYYY-MM-DD格式"}

    if start_dt > end_dt:
        return {"ok": False, "error": "起始日期不能大于结束日期"}

    # 获取用户信息
    user_info = get_user_info(token)
    worker_number = user_info.get("userCode", "")
    worker_name = user_info.get("userName", "")
    # 提取用户组织/分组/团队信息（批量填写接口必填）
    org_id = str(user_info.get("areaTeamId", ""))
    group_id = str(user_info.get("groupId", ""))
    group_name = user_info.get("groupName", "")
    team_id = str(user_info.get("teamId", ""))
    team_name = user_info.get("teamName", "")

    # 获取已有填写数据（用于跳过已填写的日期）
    # 使用 listByDate 逐日检查，比 calendar 更准确
    filled_dates = set()
    if skip_filled:
        current = start_dt
        while current <= end_dt:
            date_str = current.strftime("%Y-%m-%d")
            try:
                day_tasks = gssc_get_tasks_by_date(token, date_str)
                if any(t.get("hours", 0) > 0 for t in day_tasks):
                    filled_dates.add(date_str)
            except Exception:
                pass
            current += timedelta(days=1)

    # 生成日期列表
    current = start_dt
    dates_to_fill = []
    skipped_filled_dates = []  # 记录被跳过的已填日期
    while current <= end_dt:
        date_str = current.strftime("%Y-%m-%d")
        is_weekend = current.weekday() >= 5  # 5=Saturday, 6=Sunday

        # 跳过周末
        if skip_weekend and is_weekend:
            pass
        # 跳过已有数据的日期
        elif skip_filled and date_str in filled_dates:
            skipped_filled_dates.append(date_str)
            pass
        else:
            dates_to_fill.append(date_str)

        current += timedelta(days=1)

    if not dates_to_fill:
        skip_info = ""
        if skipped_filled_dates:
            skip_info = f"，已跳过{len(skipped_filled_dates)}天已填日期({', '.join(skipped_filled_dates)})"
        return {
            "ok": True,
            "filled": [],
            "skipped": skipped_filled_dates,
            "errors": [],
            "summary": f"{start_date_str} ~ {end_date_str} 无需填写的日期（已全部跳过）{skip_info}",
            "dry_run": dry_run,
        }

    # 如果前端已提供了 task_id，直接使用；否则通过 API 匹配
    frontend_task_id = config.get("task_id")
    # 注意：空字符串 "" 是 falsy, 需要显式排除
    has_frontend_task_id = frontend_task_id and str(frontend_task_id).strip()
    if has_frontend_task_id:
        try:
            task_id = int(frontend_task_id)
        except (ValueError, TypeError):
            task_id = None
        personal_org_id = ""
        personal_org_name = ""
        project_type = _map_datafrom_to_type(config.get("project_type", "MH"))
        project_type_name = config.get("project_type_name", "工时项目")
        print(f"  [前端指定] taskId={task_id}, projectType={project_type}")
    else:
        task_id = None
        personal_org_id = ""
        personal_org_name = ""

    if task_id is None:
        # 获取用户个人任务列表，从中匹配项目获取正确的 taskId / projectType / taskTypeParam
        personal_tasks = gssc_get_personal_tasks(token, project_id)
        matched_task = None
        for pt in personal_tasks:
            # 优先匹配同项目 + 同任务名的个人任务
            if str(pt.get("projectId")) == str(project_id):
                if pt.get("taskName") == task_name:
                    matched_task = pt
                    break
                if matched_task is None:
                    matched_task = pt  # 相同项目下的第一个任务作为备选

        if matched_task:
            # 从匹配任务中提取 taskId
            _tid_candidates = ["taskId", "id", "task_id", "taskid", "manHourTaskId", "taskCode", "taskNo"]
            _raw_tid = None
            for _f in _tid_candidates:
                if _f in matched_task and matched_task[_f] is not None and matched_task[_f] != 0:
                    _raw_tid = matched_task[_f]
                    break
            if _raw_tid is not None:
                task_id = int(_raw_tid)
                if task_id == 0:
                    task_id = None

            # projectType
            _pt_candidates = ["projectType", "type", "dataFrom", "projectTypeCode", "projectTypeId"]
            _raw_pt = None
            for _f in _pt_candidates:
                if _f in matched_task and matched_task[_f] is not None:
                    _raw_pt = matched_task[_f]
                    break
            if _raw_pt is not None:
                try:
                    project_type = int(_raw_pt)
                except (ValueError, TypeError):
                    project_type = _map_datafrom_to_type(_raw_pt)
            else:
                project_type = _map_datafrom_to_type(config.get("project_type", "MH"))

            # projectTypeName
            project_type_name = (matched_task.get("projectTypeName") or
                                 matched_task.get("typeName") or
                                 config.get("project_type_name", project_type_name))

            # orgId / orgName
            _org_keys = ["orgId", "org_id", "deptId", "departmentId"]
            for _k in _org_keys:
                if _k in matched_task and matched_task[_k]:
                    personal_org_id = str(matched_task[_k])
                    break
            personal_org_name = matched_task.get("orgName", matched_task.get("org_name", ""))

            # taskTypeParamDetail from matched task
            task_type_param_detail = matched_task.get("taskTypeParamDetail") or ""

            print(f"  [匹配] taskId={task_id}, projectType={project_type}, orgId={personal_org_id}")
        else:
            # 尝试从历史已填工时中找taskId
            print(f"  [警告] 未找到项目「{project_name}」的个人任务，尝试从历史记录查找...")
            history_tasks = gssc_find_taskId_from_history(token, project_id)
            if history_tasks:
                _best = list(history_tasks.values())[0]
                _tid_keys = ["taskId", "id", "task_id", "taskid", "manHourTaskId", "taskCode"]
                for _f in _tid_keys:
                    if _f in _best and _best[_f] is not None and _best[_f] != 0:
                        task_id = int(_best[_f])
                        if task_id == 0:
                            task_id = None
                        break
                _pt_keys = ["projectType", "type", "dataFrom", "taskType"]
                for _f in _pt_keys:
                    if _f in _best and _best[_f] is not None:
                        try:
                            project_type = int(_best[_f])
                        except (ValueError, TypeError):
                            project_type = _map_datafrom_to_type(_best[_f])
                        break
                    else:
                        project_type = _map_datafrom_to_type(config.get("project_type", "MH"))
                project_type_name = (_best.get("projectTypeName") or
                                     _best.get("typeName") or
                                     config.get("project_type_name", ""))
                for _k in _org_keys:
                    if _k in _best and _best[_k]:
                        personal_org_id = str(_best[_k])
                        break
                personal_org_name = _best.get("orgName", _best.get("org_name", ""))
                task_type_param_detail = _best.get("taskTypeParamDetail") or ""
                print(f"  [兜底] 使用历史taskId={task_id}, projectType={project_type}")
            else:
                print(f"  [最终兜底] 无任何任务来源，将不带taskId提交（服务端自动关联）")
                task_id = None
                project_type = _map_datafrom_to_type(config.get("project_type", "MH"))
                project_type_name = config.get("project_type_name", "工时项目")
                personal_org_id = ""
                personal_org_name = ""
                task_type_param_detail = ""

        # 无论匹配结果如何，如果前端传了类型详细则优先使用
        frontend_type_detail = config.get("type_detail", "")
        if frontend_type_detail:
            task_type_param_detail = frontend_type_detail

        # 组织信息优先用个人任务中的（更准确），其次用用户信息
    final_org_id = personal_org_id or org_id or group_id
    final_org_name = personal_org_name or group_name or user_info.get("areaTeamName", "")
    if not final_org_id:
        print(f"  [警告] 未获取到 orgId，可能导致提交失败")

    # 构造批量写入的请求数据
    # API: POST /v1/manHour/userHour/save
    # 格式: {dateList: ["YYYY-MM-DD", ...], taskList: [{task模板}]}
    # 每条 task 模板没有 date 字段，日期由 dateList 统一指定

    # 类型详细：前端传入 > 匹配任务的 > 历史记录的 > 空
    type_detail = config.get("type_detail", "") or ""
    if not type_detail:
        type_detail = task_type_param_detail or ""

    type_detail_param_id = config.get("type_detail_param_id", "") or ""

    # 计算 relateRdmProjectId
    rdm_id = project_id if str(config.get("project_type", "")).upper() == "RD" else "0"

    task_entry = {
        "dataFrom": "MH",
        "hours": hours_per_day,
        "jobDesc": job_desc or task_name,
        "orgId": final_org_id,
        "orgName": final_org_name,
        "projectId": int(project_id) if str(project_id).isdigit() else project_id,
        "projectName": project_name,
        "projectType": project_type,
        "projectTypeName": project_type_name or "工时项目",
        "taskId": task_id,
        "taskName": task_name,
        "taskType": str(task_type),
        "taskTypeName": _get_task_type_name(task_type),
        "taskTypeParamId": type_detail_param_id,
        "taskTypeParamDetail": type_detail,
        "relateRdmProjectId": rdm_id,
        "relateRdmProjectName": "",
    }

    if dry_run:
        return {
            "ok": True,
            "filled": dates_to_fill,
            "skipped": [],
            "errors": [],
            "summary": f"[预览] 将为 {len(dates_to_fill)} 天填写工时，每天 {hours_per_day}h，共 {len(dates_to_fill) * hours_per_day}h",
            "dry_run": True,
        }

    if task_id is None:
        return {
            "ok": False,
            "error": "未找到有效的 taskId，无法提交",
            "filled": [],
            "skipped": [],
            "errors": [{"date": "batch", "error": "taskId 为空"}],
            "summary": "批量填写失败: 未找到有效的 taskId",
        }

    # 调用正确的批量填写 API
    # 前端用到 /v1/manHour/userHour/save 提交 dateList + taskList
    save_path = "/v1/manHour/userHour/save"
    save_body = {
        "dateList": dates_to_fill,
        "taskList": [task_entry],
    }

    try:
        print(f"  [批量提交] 日期数={len(dates_to_fill)}, path={save_path}, "
              f"taskId={task_id}, projectType={project_type}, projectId={project_id}")
        result = api_request(save_path, save_body, token)
        if result.get("code") == "00000":
            print(f"  [批量提交] ✅ 成功! {len(dates_to_fill)} 天已填写")
            return {
                "ok": True,
                "filled": dates_to_fill,
                "skipped": [],
                "errors": [],
                "summary": f"成功为 {len(dates_to_fill)} 天填写工时，每天 {hours_per_day}h，"
                           f"项目={project_name}，任务={task_name}",
                "dry_run": False,
            }
        else:
            err = result.get("msg", f"code={result.get('code')}")
            print(f"  [批量提交] 返回: {err}")
            return {
                "ok": False,
                "error": err,
                "filled": [],
                "skipped": [],
                "errors": [{"date": "batch", "error": err}],
                "summary": f"批量填写失败: {err}",
            }
    except (HTTPError, URLError) as e:
        print(f"  [批量提交] 请求失败: {e}")
        return {
            "ok": False,
            "error": str(e),
            "filled": [],
            "skipped": [],
            "errors": [{"date": "batch", "error": str(e)}],
            "summary": f"批量填写失败: {e}",
        }


def _strip_empty(task: dict) -> dict:
    """移除任务字典中的 None 值，保留空字符串（API 需要字段存在）"""
    return {k: v for k, v in task.items() if v is not None}


def _get_task_type_name(code):
    """根据类型编码返回中文名称"""
    type_map = {
        "10": "IR", "20": "CR", "21": "JIRA_CR",
        "30": "缺陷", "40": "需求", "50": "请假",
    }
    return type_map.get(str(code), code)


# ============ ESS 考勤系统 API ============

def _ess_opener():
    """创建不使用代理的 URL opener（ESS内网需要绕过代理）"""
    return build_opener(ProxyHandler({}))


def ess_aes_encrypt(plaintext):
    """AES-CBC加密（与ESS前端se()函数一致）
    Key/IV: "1234567890654321", Mode: CBC, Padding: PKCS7
    """
    if not HAS_PYCRYPTODOME:
        raise ImportError("ESS考勤查询需要 pycryptodome 库，请运行: pip install pycryptodome")
    key = ESS_AES_KEY.encode('utf-8')
    iv  = ESS_AES_IV.encode('utf-8')
    cipher = _AES.new(key, _AES.MODE_CBC, iv)
    padded = _pad(plaintext.encode('utf-8'), _AES.block_size)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode('utf-8')


def ess_api_request(path, data=None, token=None, method="POST",
                    content_type="application/x-www-form-urlencoded"):
    """向ESS API发起请求（绕过代理）"""
    url = f"{ESS_API_BASE}{path}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Origin": ESS_API_BASE,
        "Referer": f"{ESS_API_BASE}/",
    }
    if token:
        headers["Authorization"] = token
    if method == "POST":
        headers["Content-Type"] = content_type
        if content_type == "application/json;charset=UTF-8":
            body = json.dumps(data or {}).encode("utf-8")
        else:
            body = urlencode(data or {}).encode("utf-8")
        req = Request(url, data=body, headers=headers, method="POST")
    else:
        req = Request(url, headers=headers, method="GET")

    opener = _ess_opener()
    resp = opener.open(req, timeout=30)
    return json.loads(resp.read().decode("utf-8"))


def ess_login(username=None, password=None):
    """登录ESS考勤系统，返回 access_token
    username/password: 显式传入（含空字符串）则使用传入值；None 则使用配置区默认值
    """
    if not HAS_PYCRYPTODOME:
        print("[ESS] 缺少 pycryptodome 库，无法登录考勤系统")
        return None

    _user = username if username is not None else GSSC_USERNAME
    _pwd  = password  if password  is not None else GSSC_PASSWORD
    if not _user or not _pwd:
        print("[ESS登录] 缺少账号或密码")
        return None

    print(f"[ESS登录] 使用工号 {_user} 登录...")
    try:
        enc_username = ess_aes_encrypt(_user)
        enc_password = ess_aes_encrypt(_pwd)
        result = ess_api_request(
            "/newAuth/auth/login/self-service",
            data={"username": enc_username, "password": enc_password},
            method="POST",
            content_type="application/json;charset=UTF-8"
        )
        if result.get("success"):
            access_token = result["data"]["access_token"]
            print(f"[ESS登录] 成功！")
            return access_token
        else:
            print(f"[ESS登录] 失败: {result.get('message', '未知错误')}")
            return None
    except Exception as e:
        print(f"[ESS登录] 异常: {e}")
        return None


def ess_get_person_info(token):
    """获取ESS当前登录用户的人员信息（personId等）"""
    try:
        result = ess_api_request(
            "/gwm-hr-masterData-center/employeeSelfLogin/getPersonByPersonNumber4Self",
            token=token, method="GET"
        )
        if result.get("code") == 200:
            return result.get("data", {})
        else:
            print(f"[ESS] 获取人员信息失败: {result.get('message', '')}")
            return {}
    except Exception as e:
        print(f"[ESS] 获取人员信息异常: {e}")
        return {}


def ess_get_attendance_stats(token, person_id, month):
    """获取出勤统计数据
    token: ESS access_token
    person_id: 人员ID（数字）
    month: 月份，格式 YYYYMM，如 "202606"
    """
    try:
        result = ess_api_request(
            "/gwm-hr-attendance-center/ding/selfAttendanceStatistical/getAttendanceStatistical4PC",
            data={"perId": person_id, "month": month},
            token=token, method="POST",
            content_type="application/x-www-form-urlencoded"
        )
        if result.get("code") == 200:
            return result.get("data", {})
        else:
            print(f"[ESS] 获取考勤数据失败: {result.get('message', '')}")
            return {}
    except Exception as e:
        print(f"[ESS] 获取考勤数据异常: {e}")
        return {}


def calculate_overtime(attendance_data, months_info=None):
    """根据考勤数据计算加班时间
    规则：
    - 标准工作日 = 9小时（8小时工作 + 1小时午休）
    - 超出9小时的部分为加班时间
    - 加班最小单位0.5小时（向下取整）
    - 早上打卡时间在8:30之前，从8:30开始计算在岗时长
    - 只计算有完整上下班打卡记录的日期

    months_info: 跨月查询时传入的月份列表信息（用于汇总），如 [{"month": "202605", "label": "5月"}, ...]
    单月查询时为 None

    返回: {
        "total_overtime": 总加班小时数,
        "overtime_days": [{date, week, clock_in, clock_out, total_hours, overtime_hours, adjusted_clock_in}, ...],
        "summary": 汇总信息字符串,
        "months_summary": 跨月汇总（仅跨月查询时有）
    }
    """
    from datetime import datetime, timedelta

    date_list = attendance_data.get("dateList", [])
    overtime_days = []
    total_overtime = 0.0

    # 跨月汇总数据
    months_overtime = {}  # month_str -> {"total": float, "days": int}

    STANDARD_START_TIME = datetime.strptime("08:30:00", "%H:%M:%S").time()

    for day in date_list:
        date_str = day.get("date", "")
        on_time = day.get("onWorkTimeCard", "")
        off_time = day.get("offWorkTimeCard", "")

        # 跳过没有打卡记录的日期
        if not on_time or not off_time:
            continue

        try:
            clock_in = datetime.strptime(on_time, "%Y-%m-%d %H:%M:%S")
            clock_out = datetime.strptime(off_time, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue

        # 需求2: 如果早上打卡时间在8:30之前，从8:30开始计算在岗时长
        adjusted_clock_in = clock_in
        if clock_in.time() < STANDARD_START_TIME:
            # 将上班时间调整为当天8:30
            adjusted_clock_in = datetime.combine(clock_in.date(), STANDARD_START_TIME)

        # 计算在岗总时长（小时），使用调整后的上班时间
        delta = clock_out - adjusted_clock_in
        total_hours = delta.total_seconds() / 3600

        # 加班计算：在岗时长 - 9小时（8小时工作 + 1小时午休）
        overtime = total_hours - 9.0
        if overtime <= 0:
            overtime = 0.0

        # 向下取整到0.5小时
        overtime = int(overtime * 2) / 2.0

        # 用于显示的上班打卡时间（如果被调整了，显示实际打卡+调整说明）
        display_clock_in = on_time
        was_adjusted = clock_in.time() < STANDARD_START_TIME

        if overtime > 0:
            total_overtime += overtime
            entry = {
                "date": date_str,
                "week": day.get("week", ""),
                "clock_in": on_time,
                "clock_out": off_time,
                "adjusted_clock_in": adjusted_clock_in.strftime("%Y-%m-%d %H:%M:%S") if was_adjusted else "",
                "was_adjusted": was_adjusted,
                "total_hours": round(total_hours, 2),
                "overtime_hours": overtime,
            }
            overtime_days.append(entry)

            # 跨月汇总：按月份分组
            month_key = date_str[:7].replace("-", "")  # "202606"
            if month_key not in months_overtime:
                months_overtime[month_key] = {"total": 0.0, "days": 0}
            months_overtime[month_key]["total"] += overtime
            months_overtime[month_key]["days"] += 1

    # 构建汇总信息
    if months_info and len(months_info) > 1:
        # 跨月汇总
        months_summary_list = []
        for mi in months_info:
            m = mi["month"]
            if m in months_overtime:
                months_summary_list.append(f"{mi['label']}: {months_overtime[m]['total']}h（{months_overtime[m]['days']}天）")
            else:
                months_summary_list.append(f"{mi['label']}: 0h")
        summary = f"跨月加班合计: {total_overtime}小时 | " + " | ".join(months_summary_list)
    else:
        summary = f"本月加班合计: {total_overtime}小时（共{len(overtime_days)}天有加班）"

    result = {
        "total_overtime": total_overtime,
        "overtime_days": overtime_days,
        "summary": summary,
    }

    # 跨月时附上各月汇总
    if months_info and len(months_info) > 1:
        result["months_summary"] = []
        for mi in months_info:
            m = mi["month"]
            mo = months_overtime.get(m, {"total": 0.0, "days": 0})
            result["months_summary"].append({
                "month": m,
                "label": mi["label"],
                "total_overtime": mo["total"],
                "overtime_days_count": mo["days"],
            })

    return result


def run_overtime_check(month=None, start_month=None, end_month=None,
                       username=None, password=None):
    """查询个人加班时间，返回结构化数据
    单月查询: 传入 month（YYYYMM格式）
    跨月查询: 传入 start_month + end_month（YYYYMM格式），会逐月查询合并
    默认: 不传参数时查本月
    username/password: ESS考勤系统账号密码，为空则使用配置区默认值
    """
    import datetime as dt

    if not HAS_PYCRYPTODOME:
        return {"ok": False, "error": "ESS考勤查询需要 pycryptodome 库，请运行: pip install pycryptodome"}

    today = dt.date.today()

    # 确定查询的月份列表
    if start_month and end_month:
        # 跨月查询模式
        try:
            sm = dt.datetime.strptime(start_month, "%Y%m")
            em = dt.datetime.strptime(end_month, "%Y%m")
        except ValueError:
            return {"ok": False, "error": "月份格式错误，请使用YYYYMM格式，如202605"}

        if sm > em:
            return {"ok": False, "error": "起始月份不能大于结束月份"}

        # 生成月份列表
        months_list = []
        current = sm
        while current <= em:
            m_str = current.strftime("%Y%m")
            months_list.append({
                "month": m_str,
                "label": f"{current.year}年{current.month}月",
            })
            # 下一个月
            if current.month == 12:
                current = dt.datetime(current.year + 1, 1, 1)
            else:
                current = dt.datetime(current.year, current.month + 1, 1)
    elif month:
        # 单月查询模式
        months_list = [{"month": month, "label": f"{int(month[:4])}年{int(month[4:6])}月"}]
    else:
        # 默认本月
        m_str = today.strftime("%Y%m")
        months_list = [{"month": m_str, "label": f"{today.year}年{today.month}月"}]

    _user = username if username is not None else GSSC_USERNAME
    _pwd  = password  if password  is not None else GSSC_PASSWORD
    if not _user or not _pwd:
        return {"ok": False, "error": "请先输入 ESS 考勤系统的账号和密码"}

    # Step 1: 登录ESS
    token = ess_login(username=_user, password=_pwd)
    if not token:
        return {"ok": False, "error": "ESS登录失败，请检查账号密码"}

    # Step 2: 获取人员信息
    person_info = ess_get_person_info(token)
    if not person_info:
        return {"ok": False, "error": "获取人员信息失败"}

    person_id = person_info.get("personId")
    person_name = person_info.get("chineseName", "")
    person_number = person_info.get("personNumber", "")
    department = person_info.get("departmentName", "")
    print(f"[ESS] 人员: {person_name}（{person_number}），部门: {department}")

    # Step 3: 逐月获取考勤数据并合并
    all_overtime_days = []
    total_overtime = 0.0
    total_should_work = 0
    total_actual_work = 0
    has_data = False
    months_attendance = {}  # 各月考勤原始数据

    for mi in months_list:
        m = mi["month"]
        print(f"[ESS] 正在查询 {mi['label']} 考勤数据...")
        attendance = ess_get_attendance_stats(token, person_id, m)
        if not attendance:
            print(f"[ESS] {mi['label']} 获取考勤数据失败，跳过")
            months_attendance[m] = None
            continue

        months_attendance[m] = attendance
        has_data = True

        # 累加应出勤/实出勤天数
        sw = attendance.get("shouldWork") or attendance.get("companyShouldWork") or 0
        aw = attendance.get("actualWork") or 0
        total_should_work += int(sw) if sw else 0
        total_actual_work += int(aw) if aw else 0

        # 计算加班
        overtime_result = calculate_overtime(attendance, months_info=months_list)
        all_overtime_days.extend(overtime_result["overtime_days"])
        total_overtime += overtime_result["total_overtime"]

    if not has_data:
        return {"ok": False, "error": "所有月份均获取考勤数据失败"}

    # 重新生成整体汇总
    if len(months_list) > 1:
        # 跨月汇总
        months_summary = []
        for mi in months_list:
            m = mi["month"]
            att = months_attendance.get(m)
            if att:
                ov = calculate_overtime(att, months_info=months_list)
                months_summary.append({
                    "month": m,
                    "label": mi["label"],
                    "total_overtime": ov["total_overtime"],
                    "overtime_days_count": len(ov["overtime_days"]),
                    "should_work": att.get("shouldWork") or att.get("companyShouldWork") or 0,
                    "actual_work": att.get("actualWork") or 0,
                })
            else:
                months_summary.append({
                    "month": m,
                    "label": mi["label"],
                    "total_overtime": 0.0,
                    "overtime_days_count": 0,
                    "should_work": 0,
                    "actual_work": 0,
                })

        summary_parts = [f"{ms['label']}: {ms['total_overtime']}h" for ms in months_summary]
        summary = f"跨月加班合计: {total_overtime}小时 | " + " | ".join(summary_parts)
    else:
        months_summary = None
        summary = f"本月加班合计: {total_overtime}小时（共{len(all_overtime_days)}天有加班）"

    print(f"[ESS] {summary}")

    # 保存结果
    month_range = months_list[0]["month"] if len(months_list) == 1 else f"{months_list[0]['month']}-{months_list[-1]['month']}"
    save_result({
        "month": month_range,
        "months": [mi["month"] for mi in months_list],
        "person_info": {
            "name": person_name,
            "number": person_number,
            "department": department,
        },
        "should_work": total_should_work,
        "actual_work": total_actual_work,
        "total_overtime": total_overtime,
        "overtime_days": all_overtime_days,
        "months_summary": months_summary,
        "timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, f"ess_overtime_{month_range}.json")

    return {
        "ok": True,
        "month": month_range,
        "months": [mi["month"] for mi in months_list],
        "person_info": {
            "name": person_name,
            "number": person_number,
            "department": department,
        },
        "should_work": total_should_work,
        "actual_work": total_actual_work,
        "total_overtime": total_overtime,
        "overtime_days": all_overtime_days,
        "months_summary": months_summary,
        "summary": summary,
    }


def analyze_no_workhour(workers, start_date, end_date):
    """
    分析未写工时的人员
    - 仅统计白名单内工号（ALLOWED_WORKER_NUMS）
    - 当天的工时不纳入统计（即使没写也不算）
    返回: {
        "no_hours": [{name, num, org, total_hours, detail}],
        "partial": [{name, num, org, total_hours, detail}],
        "ok": [{name, num, org, total_hours}],
    }
    """
    from datetime import datetime

    today_str = datetime.now().strftime("%Y-%m-%d")

    # 计算工作日天数（排除今天）
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    workdays = 0
    current = start
    while current <= end:
        if current.weekday() < 5 and current.strftime("%Y-%m-%d") != today_str:
            workdays += 1
        current += __import__('datetime').timedelta(days=1)

    no_hours = []      # 完全没写工时的
    partial = []        # 部分写了但不够8h/天的
    ok = []             # 正常
    filtered_out = 0    # 不在白名单被过滤的人数

    for worker in workers:
        name = worker.get("workerName", "")
        num = worker.get("workerNum", "")

        # 需求1: 只统计白名单内的工号
        if num not in ALLOWED_WORKER_NUMS:
            filtered_out += 1
            continue

        org = worker.get("orgName", "")
        date_list = worker.get("dateHoursList", [])

        total_hours = 0
        zero_days = 0      # 完全没填的天数
        partial_days = 0   # 填了但不够8h的天数

        for day in date_list:
            day_date = day.get("date", "")
            # 需求2: 跳过当天，不纳入统计
            if day_date == today_str:
                continue
            is_holiday = day.get("isHoliday", False)
            hours = day.get("hours", 0)
            total_hours += hours
            if not is_holiday and hours == 0:
                zero_days += 1
            elif not is_holiday and hours < 8:
                partial_days += 1

        detail = f"总工时: {total_hours}h, 未填: {zero_days}天, 不足: {partial_days}天"
        entry = {"name": name, "num": num, "org": org, "total_hours": total_hours, "zero_days": zero_days, "partial_days": partial_days, "detail": detail}

        if total_hours == 0:
            no_hours.append(entry)
        elif zero_days > 0 or partial_days > 0:
            partial.append(entry)
        else:
            ok.append({"name": name, "num": num, "org": org, "total_hours": total_hours})

    # 按未填天数对 no_hours 分组：<3天 / 3~7天 / >7天
    no_hours_lt3 = [w for w in no_hours if w["zero_days"] < 3]
    no_hours_3to7 = [w for w in no_hours if 3 <= w["zero_days"] <= 7]
    no_hours_gt7 = [w for w in no_hours if w["zero_days"] > 7]

    return {
        "no_hours": no_hours, "no_hours_lt3": no_hours_lt3, "no_hours_3to7": no_hours_3to7, "no_hours_gt7": no_hours_gt7,
        "partial": partial, "ok": ok, "workdays": workdays, "filtered_out": filtered_out,
    }


def send_dingtalk(text, is_markdown=True, at_users=None, at_mobiles=None):
    """发送钉钉群消息（加签模式）
    at_users:   工号列表（文本中 @工号）
    at_mobiles: 手机号列表（用于 atMobiles 字段，钉钉会可靠地 @到）
    """
    if not DINGTALK_WEBHOOK:
        print("[钉钉] Webhook未配置，跳过发送")
        return False, "Webhook未配置"

    at_mobiles = at_mobiles or []

    payload = {
        "msgtype": "markdown" if is_markdown else "text",
        "markdown" if is_markdown else "text": {
            "title": "GSSC工时提醒",
            "text": text
        },
        "at": {
            "atUserIds": at_users or [],
            "atMobiles": at_mobiles,
            "isAtAll": False,
        }
    }

    # 加签：生成 timestamp + sign
    url = DINGTALK_WEBHOOK
    if DINGTALK_SECRET:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{DINGTALK_SECRET}"
        hmac_code = hmac.new(
            DINGTALK_SECRET.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256
        ).digest()
        sign = quote_plus(base64.b64encode(hmac_code))
        # 用 urlparse 安全拼接参数，避免 URL 格式问题
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        query["timestamp"] = [timestamp]
        query["sign"] = [sign]
        new_query = urlencode(query, doseq=True)
        url = urlunparse((
            parsed.scheme, parsed.netloc, parsed.path,
            parsed.params, new_query, parsed.fragment
        ))

    headers = {"Content-Type": "application/json; charset=utf-8"}
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers=headers, method="POST")
    try:
        resp = urlopen(req, timeout=10, context=ssl_ctx)
        result = json.loads(resp.read().decode("utf-8"))
        if result.get("errcode") == 0:
            mobile_count = len(at_mobiles) if at_mobiles else 0
            print(f"[钉钉] 消息发送成功（at {len(at_users or [])} 工号，手机号 at {mobile_count} 人）")
            return True, "消息发送成功"
        else:
            print(f"[钉钉] 发送失败: {result}")
            return False, str(result)
    except Exception as e:
        print(f"[钉钉] 发送异常: {e}")
        return False, str(e)


def build_dingtalk_message(analysis, start_date, end_date, user_info, fill_data, check_data):
    """构建钉钉消息，返回 (msg_text, at_users, at_mobiles)
    at_users: 工号列表（文本中 @工号）
    at_mobiles: 手机号列表（用于 atMobiles 字段，钉钉会更可靠地 @到人）
    """
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    total_workers = len(analysis["no_hours"]) + len(analysis["partial"]) + len(analysis["ok"])
    no_count = len(analysis["no_hours"])
    partial_count = len(analysis["partial"])
    ok_count = len(analysis["ok"])

    at_users = []
    at_mobiles = []

    msg = f"## 【科室小助手】GSSC 工时提醒\n\n"
    msg += f"**查询日期**: {start_date} ~ {end_date}（共{analysis['workdays']}个工作日）\n"
    msg += f"**统计人数**: {total_workers}人\n"
    msg += f"**统计时间**: {today}\n\n"

    # 科室填写率 & 审批率汇总
    if fill_data or check_data:
        msg += "---\n\n"
        msg += "| 科室 | 填写率 | 审批率 | 未满8h |\n"
        msg += "|------|--------|--------|--------|\n"

        # 合并 fill_data 和 check_data（按 orgName 匹配）
        fill_map = {}
        if isinstance(fill_data, list):
            for d in fill_data:
                fill_map[d.get("orgName", d.get("org_name", ""))] = d
        elif isinstance(fill_data, dict):
            org = fill_data.get("orgName", fill_data.get("org_name", ""))
            fill_map[org] = fill_data

        check_map = {}
        if isinstance(check_data, list):
            for d in check_data:
                check_map[d.get("orgName", d.get("org_name", ""))] = d
        elif isinstance(check_data, dict):
            org = check_data.get("orgName", check_data.get("org_name", ""))
            check_map[org] = check_data

        # 取两个数据集的所有科室名
        all_orgs = set()
        for d in (fill_data if isinstance(fill_data, list) else [fill_data] if fill_data else []):
            all_orgs.add(d.get("orgName", d.get("org_name", "")))
        for d in (check_data if isinstance(check_data, list) else [check_data] if check_data else []):
            all_orgs.add(d.get("orgName", d.get("org_name", "")))

        for org in sorted(all_orgs):
            f = fill_map.get(org, {})
            c = check_map.get(org, {})
            fill_pct = f.get("percentageText", f.get("percentage_text", "N/A"))
            check_pct = c.get("percentageText", c.get("percentage_text", "N/A"))
            not_enough = f.get("notEnough8", f.get("not_enough_8", "N/A"))
            msg += f"| {org} | {fill_pct} | {check_pct} | {not_enough}人 |\n"
        msg += "\n"

    # 未写工时人员 - 按未填天数分3档 @工号
    # <3天（轻微）
    if analysis.get("no_hours_lt3"):
        msg += f"### 未填写工时 <3天（{len(analysis['no_hours_lt3'])}人）\n\n"
        for w in analysis["no_hours_lt3"]:
            num = w['num']
            mobile = WORKER_TO_MOBILE.get(num, "")
            at_users.append(num)
            if mobile:
                at_mobiles.append(mobile)
            # 有手机号的用 @手机号，没手机号的用 @工号
            at_target = mobile if mobile else num
            msg += f"- @{at_target} **{w['name']}**（{num}）- {w['org']} - 未填{w['zero_days']}天\n"
        msg += "\n"

    # 3~7天（中等）
    if analysis.get("no_hours_3to7"):
        msg += f"### 未填写工时 3~7天（{len(analysis['no_hours_3to7'])}人）\n\n"
        for w in analysis["no_hours_3to7"]:
            num = w['num']
            mobile = WORKER_TO_MOBILE.get(num, "")
            at_users.append(num)
            if mobile:
                at_mobiles.append(mobile)
            at_target = mobile if mobile else num
            msg += f"- @{at_target} **{w['name']}**（{num}）- {w['org']} - 未填{w['zero_days']}天\n"
        msg += "\n"

    # >7天（严重）
    if analysis.get("no_hours_gt7"):
        msg += f"### 未填写工时 >7天（{len(analysis['no_hours_gt7'])}人）\n\n"
        for w in analysis["no_hours_gt7"]:
            num = w['num']
            mobile = WORKER_TO_MOBILE.get(num, "")
            at_users.append(num)
            if mobile:
                at_mobiles.append(mobile)
            at_target = mobile if mobile else num
            msg += f"- @{at_target} **{w['name']}**（{num}）- {w['org']} - 未填{w['zero_days']}天\n"
        msg += "\n"

    # 部分填写 - @工号
    if analysis["partial"]:
        msg += f"### 工时不足（{partial_count}人）\n\n"
        for w in analysis["partial"]:
            num = w['num']
            mobile = WORKER_TO_MOBILE.get(num, "")
            at_users.append(num)
            if mobile:
                at_mobiles.append(mobile)
            at_target = mobile if mobile else num
            msg += f"- @{at_target} **{w['name']}**（{num}）- {w['detail']}\n"
        msg += "\n"

    # 汇总
    msg += "---\n\n"
    msg += f"> **统计**: 未填 <3天 {len(analysis.get('no_hours_lt3', []))}人 | 3~7天 {len(analysis.get('no_hours_3to7', []))}人 | >7天 {len(analysis.get('no_hours_gt7', []))}人 | 不足 {partial_count}人 | 正常 {ok_count}人\n"
    msg += f"> 请各位及时补充工时记录，谢谢！"

    return msg, at_users, at_mobiles


def save_result(data, filename):
    """保存结果到JSON文件"""
    try:
        Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        filepath = Path(OUTPUT_DIR) / filename
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[保存] {filepath}")
    except Exception as e:
        print(f"[保存] 失败: {e}")


def run_check(start_date=None, end_date=None):
    """核心检测逻辑，供 Web 端和命令行共用，返回结构化数据"""
    import datetime as dt

    today = dt.date.today()
    if not start_date:
        start_date = today.strftime("%Y-%m-01")
    if not end_date:
        end_date = today.strftime("%Y-%m-%d")

    token = login()
    if not token:
        return {"ok": False, "error": "登录失败，请检查账号密码"}

    user_info = get_user_info(token)
    workers = get_workhour_stats(token, start_date, end_date)
    if not workers:
        return {"ok": False, "error": "未获取到员工数据，可能权限不足"}

    fill_data = get_fill_percent(token, start_date, end_date)
    check_data = get_check_percent(token, start_date, end_date)
    analysis = analyze_no_workhour(workers, start_date, end_date)

    msg, at_users, at_mobiles = build_dingtalk_message(
        analysis, start_date, end_date, user_info, fill_data, check_data
    )

    # 保存 JSON
    save_result({
        "date": f"{start_date} ~ {end_date}",
        "user_info": user_info,
        "fill_data": fill_data,
        "check_data": check_data,
        "total_workers": len(workers),
        "no_hours": analysis["no_hours"],
        "partial": analysis["partial"],
        "ok": analysis["ok"],
        "timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, f"gssc_result_{today.strftime('%Y%m%d')}.json")

    return {
        "ok": True,
        "start_date": start_date,
        "end_date": end_date,
        "user_info": user_info,
        "fill_data": fill_data,
        "check_data": check_data,
        "analysis": analysis,
        "msg": msg,
        "at_users": at_users,
        "at_mobiles": at_mobiles,
        "total_workers": len(workers),
    }


# ============ Web 模式（纯 stdlib，无需 pip install） ============
_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GSSC 工时检测</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f0f2f5; min-height: 100vh; padding: 24px; color: #1a1a2e; }
  .container { max-width: 960px; margin: 0 auto; }
  h1 { font-size: 22px; font-weight: 700; margin-bottom: 20px; color: #1a1a2e;
       display: flex; align-items: center; gap: 8px; }
  h1 span { font-size: 26px; }
  .card { background: #fff; border-radius: 12px; padding: 20px 24px;
          box-shadow: 0 2px 8px rgba(0,0,0,.07); margin-bottom: 16px; }
  .row { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; }
  label { font-size: 13px; color: #666; display: block; margin-bottom: 4px; }
  input[type=date] { padding: 8px 12px; border: 1px solid #d9d9d9; border-radius: 8px;
                      font-size: 14px; outline: none; transition: border .2s; }
  input[type=date]:focus { border-color: #1677ff; }
  .btn { padding: 9px 20px; border: none; border-radius: 8px; font-size: 14px;
         font-weight: 600; cursor: pointer; transition: all .2s; }
  .btn-primary { background: #1677ff; color: #fff; }
  .btn-primary:hover:not(:disabled) { background: #0958d9; }
  .btn-success { background: #52c41a; color: #fff; }
  .btn-success:hover:not(:disabled) { background: #389e0d; }
  .btn-warning { background: #fa8c16; color: #fff; }
  .btn-warning:hover:not(:disabled) { background: #d46b08; }
  .btn:disabled { opacity: .55; cursor: not-allowed; }
  .status-bar { padding: 10px 16px; border-radius: 8px; font-size: 13px; margin-top: 12px;
                display: none; align-items: center; gap: 8px; }
  .status-bar.info  { background: #e6f4ff; color: #1677ff; display: flex; }
  .status-bar.ok    { background: #f6ffed; color: #389e0d; display: flex; }
  .status-bar.error { background: #fff2f0; color: #cf1322; display: flex; }
  .spin { animation: spin 1s linear infinite; display: inline-block; }
  @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

  .stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; }
  .stat-item { background: #f9f9f9; border-radius: 10px; padding: 14px 16px; text-align: center; }
  .stat-item .num { font-size: 28px; font-weight: 700; line-height: 1; }
  .stat-item .lbl { font-size: 12px; color: #888; margin-top: 4px; }
  .stat-item.red .num { color: #f5222d; }
  .stat-item.orange .num { color: #fa8c16; }
  .stat-item.yellow .num { color: #faad14; }
  .stat-item.green .num { color: #52c41a; }
  .stat-item.blue .num { color: #1677ff; }

  .section-title { font-size: 15px; font-weight: 600; margin: 16px 0 8px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; margin-right: 4px; }
  .badge-red { background: #fff1f0; color: #f5222d; border: 1px solid #ffa39e; }
  .badge-orange { background: #fff7e6; color: #fa8c16; border: 1px solid #ffd591; }
  .badge-yellow { background: #fffbe6; color: #faad14; border: 1px solid #ffe58f; }
  .badge-gray { background: #f5f5f5; color: #595959; border: 1px solid #d9d9d9; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { background: #fafafa; padding: 8px 12px; text-align: left; border-bottom: 1px solid #f0f0f0;
       font-weight: 600; color: #555; }
  td { padding: 8px 12px; border-bottom: 1px solid #f0f0f0; color: #333; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #fafafa; }

  .msg-preview { background: #f6f6f6; border-radius: 8px; padding: 14px 16px;
                 font-size: 13px; white-space: pre-wrap; word-break: break-all;
                 max-height: 320px; overflow-y: auto; border: 1px solid #e8e8e8; }

  .hidden { display: none !important; }
  .fill-rate-list { font-size: 13px; color: #444; }
  .fill-rate-list li { padding: 3px 0; list-style: none; }
  .overtime-highlight { background: linear-gradient(135deg, #fff7e6 0%, #fff1f0 100%);
    border: 2px solid #fa8c16; border-radius: 12px; padding: 16px 20px; margin: 12px 0; }
  .overtime-highlight .big-num { font-size: 36px; font-weight: 800; color: #fa541c; }
  .overtime-highlight .big-lbl { font-size: 14px; color: #8c8c8c; margin-top: 2px; }
  .tag-overtime { display: inline-block; background: #fff7e6; color: #fa8c16; border: 1px solid #ffd591;
    border-radius: 4px; padding: 1px 6px; font-size: 12px; font-weight: 600; }
  .ess-cred-row { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; margin-top: 14px;
    padding-top: 14px; border-top: 1px dashed #e8e8e8; }
  .ess-cred-row label { font-size: 13px; color: #666; display: block; margin-bottom: 4px; }
  .ess-cred-row input[type=text], .ess-cred-row input[type=password],
  .ess-cred-row input[type=month] {
    padding: 7px 12px; border: 1px solid #d9d9d9; border-radius: 8px; font-size: 14px;
    outline: none; transition: border .2s; width: 170px; }
  .ess-cred-row input:focus { border-color: #fa8c16; }
  .remember-wrap { display: flex; align-items: center; gap: 4px; margin-bottom: 2px; }
  .remember-wrap input[type=checkbox] { width: 16px; height: 16px; accent-color: #fa8c16; cursor: pointer; }
  .remember-wrap span { font-size: 13px; color: #888; cursor: pointer; }
  .cred-hint { font-size: 11px; color: #bbb; margin-top: 6px; }
  .month-summary-card { background: #f9f9f9; border-radius: 8px; padding: 10px 14px;
    margin-bottom: 8px; display: flex; align-items: center; gap: 16px; }
  .month-summary-card .month-label { font-size: 14px; font-weight: 600; color: #333; min-width: 80px; }
  .month-summary-card .month-stat { font-size: 13px; color: #666; }
  .month-summary-card .month-ot { font-size: 18px; font-weight: 700; color: #fa541c; }
  .tag-adjusted { display: inline-block; background: #e6f4ff; color: #1677ff; border: 1px solid #91caff;
    border-radius: 4px; padding: 1px 6px; font-size: 11px; font-weight: 600; }
  /* 批量填写工时 */
  #bfProject { max-height: 40px; }
  .bf-preview-table { width:100%;border-collapse:collapse;margin-top:8px;font-size:13px; }
  .bf-preview-table th { background:#f5f5f5;padding:6px 10px;text-align:left;font-weight:600;border-bottom:2px solid #e8e8e8; }
  .bf-preview-table td { padding:5px 10px;border-bottom:1px solid #f0f0f0; }
  .bf-preview-table tr:hover { background:#fafafa; }
  .bf-stat-card { display:inline-flex;align-items:center;gap:8px;padding:8px 16px;background:#f0f9ff;border-radius:8px;
    border:1px solid #bae7ff;margin:4px 4px 4px 0; }
  .bf-stat-num { font-size:24px;font-weight:700;color:#1677ff; }
  .bf-stat-lbl { font-size:12px;color:#666; }
</style>
</head>
<body>
<div class="container">
  <h1><span>⏱</span> GSSC 工时检测 &amp; 钉钉通知</h1>

  <div class="card">
    <div class="row">
      <div>
        <label>开始日期</label>
        <input type="date" id="startDate">
      </div>
      <div>
        <label>结束日期</label>
        <input type="date" id="endDate">
      </div>
      <button class="btn btn-primary" id="btnCheck" onclick="doCheck()">🔍 开始检测</button>
      <button class="btn btn-success hidden" id="btnSend" onclick="doSend()">📨 发送钉钉通知</button>
    </div>
    <div class="ess-cred-row">
      <div>
        <label>ESS 工号</label>
        <input type="text" id="essUsername" placeholder="如 GW00295409" autocomplete="username">
      </div>
      <div>
        <label>ESS 密码</label>
        <input type="password" id="essPassword" placeholder="考勤系统密码" autocomplete="current-password">
      </div>
      <div>
        <label>&nbsp;</label>
        <div class="remember-wrap">
          <input type="checkbox" id="essRemember"><span onclick="document.getElementById('essRemember').click()">记住密码</span>
        </div>
      </div>
      <div>
        <label>起始月份</label>
        <input type="month" id="startMonth">
      </div>
      <div>
        <label>结束月份</label>
        <input type="month" id="endMonth">
      </div>
      <button class="btn btn-warning" id="btnOvertime" onclick="doOvertime()">⏰ 查询加班时间</button>
    </div>
    <div class="cred-hint">提示：账号密码用于登录 ESS 考勤系统 (ess.gwm.cn)，仅在浏览器本地保存 | 选择起止月份可查看跨月加班工时</div>
    <div class="status-bar" id="statusBar"></div>
  </div>

  <div id="resultArea" class="hidden">
    <div class="card">
      <div class="section-title">📊 统计概览</div>
      <div class="stats" id="statsRow"></div>
      <div style="margin-top:12px;" id="fillRateArea"></div>
    </div>
    <div class="card" id="noHoursArea"></div>
    <div class="card hidden" id="partialArea">
      <div class="section-title">⚠️ 工时不足人员</div>
      <table>
        <thead><tr><th>姓名</th><th>工号</th><th>部门</th><th>详情</th></tr></thead>
        <tbody id="partialBody"></tbody>
      </table>
    </div>
    <div class="card">
      <div class="section-title">💬 钉钉消息预览</div>
      <div class="msg-preview" id="msgPreview"></div>
    </div>
  </div>

  <div id="overtimeArea" class="hidden">
    <div class="card">
      <div class="section-title">⏰ 个人加班时间</div>
      <div id="overtimeSummary"></div>
      <div style="margin-top:12px;" id="overtimeDetail"></div>
    </div>
  </div>

  <!-- 批量填写工时区域 -->
  <div class="card" style="margin-top:16px;">
    <div class="section-title">📝 批量填写工时</div>
    <div id="batchFillForm">
      <div class="batch-fill-row" style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px;">
        <div style="flex:1;min-width:200px;">
          <label style="font-size:13px;color:#666;display:block;margin-bottom:4px;">项目</label>
          <select id="bfProject" onchange="onBfProjectChange()" style="width:100%;padding:7px 10px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;">
            <option value="">-- 加载中... --</option>
          </select>
        </div>
        <div style="flex:1;min-width:150px;">
          <label style="font-size:13px;color:#666;display:block;margin-bottom:4px;">起始日期</label>
          <input type="date" id="bfStartDate" style="width:100%;padding:7px 10px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;">
        </div>
        <div style="flex:1;min-width:150px;">
          <label style="font-size:13px;color:#666;display:block;margin-bottom:4px;">结束日期</label>
          <input type="date" id="bfEndDate" style="width:100%;padding:7px 10px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;">
        </div>
        <div style="flex:0;min-width:100px;">
          <label style="font-size:13px;color:#666;display:block;margin-bottom:4px;">每天(h)</label>
          <input type="number" id="bfHours" value="8" min="0.5" max="24" step="0.5"
                 style="width:100%;padding:7px 10px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;text-align:center;">
        </div>
      </div>
      <div class="batch-fill-row" style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px;">
        <div style="flex:2;min-width:200px;">
          <label style="font-size:13px;color:#666;display:block;margin-bottom:4px;">任务名称</label>
          <select id="bfTaskName" onchange="onBfTaskChange()" style="width:100%;padding:7px 10px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;">
            <option value="">-- 请先选项目 --</option>
          </select>
        </div>
        <div style="flex:1;min-width:120px;">
          <label style="font-size:13px;color:#666;display:block;margin-bottom:4px;">任务类型</label>
          <select id="bfTaskType" onchange="onBfTaskTypeChange()" style="width:100%;padding:7px 10px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;">
            <option value="10">IR</option>
            <option value="20">CR</option>
            <option value="30">缺陷</option>
            <option value="40">需求</option>
          </select>
        </div>
        <div style="flex:1;min-width:200px;">
          <label style="font-size:13px;color:#666;display:block;margin-bottom:4px;">类型详细</label>
          <select id="bfTypeDetail" style="width:100%;padding:7px 10px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;">
            <option value="">-- 不填 --</option>
          </select>
        </div>
      </div>
      <div class="batch-fill-row" style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px;">
        <div style="flex:2;min-width:200px;">
          <label style="font-size:13px;color:#666;display:block;margin-bottom:4px;">工作描述</label>
          <input type="text" id="bfJobDesc" placeholder="输入工作描述..."
                 style="width:100%;padding:7px 10px;border:1px solid #d9d9d9;border-radius:8px;font-size:14px;">
        </div>
        <div style="flex:1;min-width:200px;display:flex;align-items:center;gap:8px;padding-top:18px;">
          <label style="font-size:13px;display:flex;align-items:center;gap:4px;cursor:pointer;">
            <input type="checkbox" id="bfSkipFilled" checked> 跳过已填写
          </label>
          <label style="font-size:13px;display:flex;align-items:center;gap:4px;cursor:pointer;">
            <input type="checkbox" id="bfSkipWeekend" checked> 跳过周末
          </label>
        </div>
      </div>
      <div style="display:flex;gap:10px;align-items:center;">
        <button class="btn btn-primary" onclick="doBatchFillPreview()">👁 预览</button>
        <button class="btn btn-warning" onclick="doBatchFillSubmit()" id="btnBatchFill">✅ 提交填写</button>
        <span id="batchFillStatus" style="font-size:13px;"></span>
      </div>
    </div>
    <div id="batchFillResult" style="margin-top:12px;"></div>
  </div>
</div>

<script>
(function() {
  const now = new Date();
  const y = now.getFullYear(), m = String(now.getMonth()+1).padStart(2,'0');
  const d = String(now.getDate()).padStart(2,'0');
  document.getElementById('startDate').value = `${y}-${m}-01`;
  document.getElementById('endDate').value = `${y}-${m}-${d}`;
  // 默认月份选择器：本月
  document.getElementById('startMonth').value = `${y}-${m}`;
  document.getElementById('endMonth').value = `${y}-${m}`;

  // 恢复记住的 ESS 账号密码
  try {
    const saved = JSON.parse(localStorage.getItem('ess_creds') || '{}');
    if (saved.username) document.getElementById('essUsername').value = saved.username;
    if (saved.password) document.getElementById('essPassword').value = atob(saved.password);
    if (saved.remember) document.getElementById('essRemember').checked = true;
  } catch(e) {}
})();

function saveEssCreds() {
  const username = document.getElementById('essUsername').value.trim();
  const password = document.getElementById('essPassword').value;
  const remember = document.getElementById('essRemember').checked;
  if (remember && username && password) {
    localStorage.setItem('ess_creds', JSON.stringify({
      username: username,
      password: btoa(password),   // 简单编码，仅防窥视，非加密
      remember: true
    }));
  } else if (!remember) {
    localStorage.removeItem('ess_creds');
  }
}

function loadEssCreds() {
  const username = document.getElementById('essUsername').value.trim();
  const password = document.getElementById('essPassword').value;
  return { username, password };
}

let cachedResult = null;

function setStatus(msg, type) {
  const bar = document.getElementById('statusBar');
  bar.className = `status-bar ${type}`;
  bar.innerHTML = msg;
  bar.style.display = 'flex';
}

async function doCheck() {
  const startDate = document.getElementById('startDate').value;
  const endDate   = document.getElementById('endDate').value;
  if (!startDate || !endDate) { setStatus('⚠️ 请选择日期范围', 'error'); return; }

  document.getElementById('btnCheck').disabled = true;
  document.getElementById('btnSend').classList.add('hidden');
  document.getElementById('resultArea').classList.add('hidden');
  setStatus('<span class="spin">⏳</span>&nbsp;正在登录并拉取工时数据，请稍候...', 'info');

  try {
    const resp = await fetch('/api/check', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({start_date: startDate, end_date: endDate})
    });
    const data = await resp.json();

    if (!data.ok) {
      setStatus(`❌ 检测失败: ${data.error}`, 'error');
      return;
    }

    cachedResult = data;
    renderResult(data);
    setStatus('✅ 检测完成，请确认结果后点击「发送钉钉通知」', 'ok');
    document.getElementById('btnSend').classList.remove('hidden');
  } catch(e) {
    setStatus(`❌ 请求异常: ${e.message}`, 'error');
  } finally {
    document.getElementById('btnCheck').disabled = false;
  }
}

function renderResult(data) {
  const a = data.analysis;
  const statsRow = document.getElementById('statsRow');
  statsRow.innerHTML = `
    <div class="stat-item red"><div class="num">${a.no_hours_gt7.length}</div><div class="lbl">未填 &gt;7天</div></div>
    <div class="stat-item orange"><div class="num">${a.no_hours_3to7.length}</div><div class="lbl">未填 3~7天</div></div>
    <div class="stat-item yellow"><div class="num">${a.no_hours_lt3.length}</div><div class="lbl">未填 &lt;3天</div></div>
    <div class="stat-item orange"><div class="num">${a.partial.length}</div><div class="lbl">工时不足</div></div>
    <div class="stat-item green"><div class="num">${a.ok.length}</div><div class="lbl">正常</div></div>
    <div class="stat-item blue"><div class="num">${a.workdays}</div><div class="lbl">统计工作日</div></div>
  `;

  const fillArea = document.getElementById('fillRateArea');
  if ((data.fill_data && data.fill_data.length) || (data.check_data && data.check_data.length)) {
    let html = '<div class="section-title" style="margin-top:12px;">📈 科室填写率 &amp; 审批率</div>';
    html += '<table><thead><tr><th>科室</th><th>填写率</th><th>审批率</th><th>未满8h</th></tr></thead><tbody>';

    // 合并 fill_data 和 check_data（按 orgName 匹配）
    const fillMap = {}, checkMap = {};
    (Array.isArray(data.fill_data) ? data.fill_data : (data.fill_data ? [data.fill_data] : [])).forEach(d => {
      fillMap[d.orgName || d.org_name || ''] = d;
    });
    (Array.isArray(data.check_data) ? data.check_data : (data.check_data ? [data.check_data] : [])).forEach(d => {
      checkMap[d.orgName || d.org_name || ''] = d;
    });

    const allOrgs = new Set([...Object.keys(fillMap), ...Object.keys(checkMap)]);
    [...allOrgs].sort().forEach(org => {
      const f = fillMap[org] || {};
      const c = checkMap[org] || {};
      html += `<tr>
        <td><b>${org}</b></td>
        <td>${f.percentageText || f.percentage_text || 'N/A'}</td>
        <td>${c.percentageText || c.percentage_text || 'N/A'}</td>
        <td>${f.notEnough8 || f.not_enough_8 || 'N/A'} 人</td>
      </tr>`;
    });
    html += '</tbody></table>';
    fillArea.innerHTML = html;
  } else {
    fillArea.innerHTML = '';
  }

  const noArea = document.getElementById('noHoursArea');
  let noHtml = '';

  function renderGroup(list, title, badgeClass) {
    if (!list || !list.length) return '';
    let h = `<div class="section-title">${title}</div>
    <table><thead><tr><th>姓名</th><th>工号</th><th>部门</th><th>未填天数</th></tr></thead><tbody>`;
    list.forEach(w => {
      h += `<tr><td><b>${w.name}</b></td><td><span class="badge ${badgeClass}">${w.num}</span></td>
            <td>${w.org}</td><td>${w.zero_days} 天</td></tr>`;
    });
    h += '</tbody></table>';
    return h;
  }

  noHtml += renderGroup(a.no_hours_gt7, '🔴 未填写工时 >7天', 'badge-red');
  noHtml += renderGroup(a.no_hours_3to7, '🟠 未填写工时 3~7天', 'badge-orange');
  noHtml += renderGroup(a.no_hours_lt3, '🟡 未填写工时 <3天', 'badge-yellow');

  if (!noHtml) noHtml = '<div style="color:#52c41a;padding:8px 0;">🎉 所有人员均已填写工时！</div>';
  noArea.innerHTML = noHtml;

  if (a.partial && a.partial.length) {
    document.getElementById('partialArea').classList.remove('hidden');
    const tbody = document.getElementById('partialBody');
    tbody.innerHTML = a.partial.map(w =>
      `<tr><td><b>${w.name}</b></td><td><span class="badge badge-gray">${w.num}</span></td>
       <td>${w.org}</td><td>${w.detail}</td></tr>`
    ).join('');
  } else {
    document.getElementById('partialArea').classList.add('hidden');
  }

  document.getElementById('msgPreview').textContent = data.msg;
  document.getElementById('resultArea').classList.remove('hidden');
}

async function doSend() {
  if (!cachedResult) { setStatus('⚠️ 请先执行检测', 'error'); return; }

  document.getElementById('btnSend').disabled = true;
  setStatus('<span class="spin">⏳</span>&nbsp;正在发送钉钉通知...', 'info');

  try {
    const resp = await fetch('/api/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        msg: cachedResult.msg,
        at_users: cachedResult.at_users,
        at_mobiles: cachedResult.at_mobiles
      })
    });
    const data = await resp.json();
    if (data.ok) {
      setStatus(`✅ 钉钉通知已发送！将 @ ${cachedResult.at_users.length} 人`, 'ok');
    } else {
      setStatus(`❌ 发送失败: ${data.error}`, 'error');
    }
  } catch(e) {
    setStatus(`❌ 请求异常: ${e.message}`, 'error');
  } finally {
    document.getElementById('btnSend').disabled = false;
  }
}

async function doOvertime() {
  const creds = loadEssCreds();
  if (!creds.username || !creds.password) {
    setStatus('⚠️ 请先输入 ESS 工号和密码后再查询加班', 'error');
    document.getElementById('essUsername').focus();
    return;
  }

  // 获取起止月份
  const startMonthVal = document.getElementById('startMonth').value;  // "YYYY-MM"
  const endMonthVal = document.getElementById('endMonth').value;
  if (!startMonthVal || !endMonthVal) {
    setStatus('⚠️ 请选择起止月份', 'error');
    document.getElementById('startMonth').focus();
    return;
  }
  const startMonth = startMonthVal.replace('-', '');  // "YYYYMM"
  const endMonth = endMonthVal.replace('-', '');

  document.getElementById('btnOvertime').disabled = true;
  document.getElementById('overtimeArea').classList.add('hidden');
  const isCrossMonth = startMonth !== endMonth;
  setStatus(`<span class="spin">⏳</span>&nbsp;正在登录ESS考勤系统并查询${isCrossMonth ? '跨月' : '本月'}加班数据...`, 'info');

  try {
    const resp = await fetch('/api/overtime', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        username: creds.username,
        password: creds.password,
        start_month: startMonth,
        end_month: endMonth
      })
    });
    const data = await resp.json();

    if (!data.ok) {
      setStatus(`❌ 加班查询失败: ${data.error}`, 'error');
      return;
    }

    // 查询成功后保存凭据（如果勾选了记住密码）
    saveEssCreds();
    renderOvertime(data);
    setStatus('✅ 加班时间查询完成', 'ok');
  } catch(e) {
    setStatus(`❌ 请求异常: ${e.message}`, 'error');
  } finally {
    document.getElementById('btnOvertime').disabled = false;
  }
}

function renderOvertime(data) {
  const summaryDiv = document.getElementById('overtimeSummary');
  const detailDiv = document.getElementById('overtimeDetail');
  const pi = data.person_info || {};
  const isCrossMonth = data.months && data.months.length > 1;
  const monthLabel = isCrossMonth ? data.months[0] + '~' + data.months[data.months.length-1] : (data.month || '本月');

  // 跨月汇总卡片
  let monthsSummaryHtml = '';
  if (data.months_summary && data.months_summary.length > 1) {
    monthsSummaryHtml = '<div style="margin-top:12px;"><div class="section-title">📊 各月加班汇总</div>';
    data.months_summary.forEach(ms => {
      monthsSummaryHtml += `<div class="month-summary-card">
        <div class="month-label">${ms.label}</div>
        <div class="month-ot">${ms.total_overtime}h</div>
        <div class="month-stat">加班${ms.overtime_days_count}天 | 应出勤${ms.should_work || '-'}天 | 实出勤${ms.actual_work || '-'}天</div>
      </div>`;
    });
    monthsSummaryHtml += '</div>';
  }

  summaryDiv.innerHTML = `
    <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;">
      <div class="overtime-highlight" style="flex:1;min-width:200px;">
        <div class="big-num">${data.total_overtime}</div>
        <div class="big-lbl">${isCrossMonth ? '跨月加班（小时）' : '本月加班（小时）'}</div>
      </div>
      <div style="flex:2;min-width:200px;">
        <div style="font-size:14px;color:#333;margin-bottom:6px;">
          <b>${pi.name || ''}</b>（${pi.number || ''}）
        </div>
        <div style="font-size:13px;color:#666;">${pi.department || ''}</div>
        <div style="font-size:13px;color:#888;margin-top:4px;">
          查询范围 <b>${monthLabel}</b> &nbsp;|&nbsp;
          应出勤 <b>${data.should_work || '-'}</b> 天 &nbsp;|&nbsp;
          实出勤 <b>${data.actual_work || '-'}</b> 天 &nbsp;|&nbsp;
          加班天数 <b>${data.overtime_days ? data.overtime_days.length : 0}</b> 天
        </div>
      </div>
    </div>
    <div style="margin-top:8px;font-size:12px;color:#999;">
      计算规则：在岗时长 - 9小时（8小时工作+1小时午休），向下取整到0.5小时 | 早上8:30前打卡从8:30起算
    </div>
    ${monthsSummaryHtml}
  `;

  const days = data.overtime_days || [];
  if (days.length > 0) {
    let html = '<div class="section-title">📅 加班明细</div>';
    html += '<table><thead><tr><th>日期</th><th>星期</th><th>上班打卡</th><th>下班打卡</th><th>在岗时长</th><th>加班时长</th></tr></thead><tbody>';
    days.forEach(d => {
      const clockIn = d.clock_in ? d.clock_in.split(' ')[1] : '-';
      const clockOut = d.clock_out ? d.clock_out.split(' ')[1] : '-';
      const adjustedTag = d.was_adjusted ? '<span class="tag-adjusted">8:30起算</span>' : '';
      html += `<tr>
        <td><b>${d.date}</b></td>
        <td>${d.week}</td>
        <td>${clockIn} ${adjustedTag}</td>
        <td>${clockOut}</td>
        <td>${d.total_hours}h</td>
        <td><span class="tag-overtime">${d.overtime_hours}h</span></td>
      </tr>`;
    });
    html += '</tbody></table>';
    detailDiv.innerHTML = html;
  } else {
    detailDiv.innerHTML = '<div style="color:#52c41a;padding:8px 0;">🎉 查询范围内暂无加班记录！</div>';
  }

  document.getElementById('overtimeArea').classList.remove('hidden');
}

// ============ 批量填写工时 ============
let bfProjects = [];
let bfTasks = [];  // 当前项目的任务列表
let bfTaskMap = {};  // taskName -> task 对象

function initBatchFill() {
  const now = new Date();
  const y = now.getFullYear(), m = String(now.getMonth()+1).padStart(2,'0');
  const d = String(now.getDate()).padStart(2,'0');
  document.getElementById('bfStartDate').value = `${y}-${m}-01`;
  document.getElementById('bfEndDate').value = `${y}-${m}-${d}`;

  // 恢复上次选择的表单值
  try {
    const last = JSON.parse(localStorage.getItem('bf_last_config') || '{}');
    if (last.task_type) document.getElementById('bfTaskType').value = last.task_type;
    if (last.hours_per_day) document.getElementById('bfHours').value = last.hours_per_day;
    document.getElementById('bfSkipFilled').checked = last.skip_filled !== false;
    document.getElementById('bfSkipWeekend').checked = last.skip_weekend !== false;
    // 恢复工作描述（用户上次输入的内容）
    if (last.job_desc) document.getElementById('bfJobDesc').value = last.job_desc;
    // 记录上次项目/任务名，等项目列表加载完再恢复
    window._bfLastProjectId = last.project_id || '';
    window._bfLastTaskName = last.task_name || '';
    window._bfLastTaskType = last.task_type || '';
    window._bfLastTypeDetail = last.type_detail || '';
    window._bfLastTypeDetailParamId = last.type_detail_param_id || '';
  } catch(e) {}

  loadBfProjects();
}

async function loadBfProjects() {
  try {
    const resp = await fetch('/api/gssc/projects', { method: 'POST' });
    const data = await resp.json();
    if (data.ok) {
      bfProjects = data.projects || [];
      const sel = document.getElementById('bfProject');
      sel.innerHTML = '<option value="">-- 请选择项目 --</option>';
      bfProjects.sort((a,b)=>(a.projectName||'').localeCompare(b.projectName||'','zh'));
      bfProjects.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.projectId;
        opt.textContent = p.projectName;
        opt.dataset.type = p.dataFrom || 'MH';
        sel.appendChild(opt);
      });

      // 恢复上次选中的项目
      if (window._bfLastProjectId) {
        sel.value = window._bfLastProjectId;
        if (sel.value === window._bfLastProjectId) {
          onBfProjectChange();  // 触发加载任务列表
        }
      }
    }
  } catch(e) { console.error('加载项目失败', e); }
}

// 项目切换时，重新加载任务名称列表
async function onBfProjectChange() {
  const proj = getSelectedProject();
  const taskSel = document.getElementById('bfTaskName');
  const detailSel = document.getElementById('bfTypeDetail');

  taskSel.innerHTML = '<option value="">-- 加载中... --</option>';
  detailSel.innerHTML = '<option value="">-- 不填 --</option>';
  bfTasks = [];
  bfTaskMap = {};

  if (!proj.id) {
    taskSel.innerHTML = '<option value="">-- 请先选项目 --</option>';
    return;
  }

  try {
    const resp = await fetch('/api/gssc/project-tasks', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project_id: proj.id})
    });
    const data = await resp.json();
    if (data.ok && data.tasks) {
      bfTasks = data.tasks;
      taskSel.innerHTML = '<option value="">-- 请选择任务 --</option>';
      bfTasks.forEach(t => {
        bfTaskMap[t.taskName] = t;
        const opt = document.createElement('option');
        opt.value = t.taskName;
        opt.textContent = t.taskName;
        taskSel.appendChild(opt);
      });

      // 恢复上次选中的任务名
      if (window._bfLastTaskName && bfTaskMap[window._bfLastTaskName]) {
        taskSel.value = window._bfLastTaskName;
        onBfTaskChange();  // 触发类型详细加载
      }
    } else {
      taskSel.innerHTML = '<option value="">-- 无可用任务 --</option>';
    }
  } catch(e) {
    taskSel.innerHTML = '<option value="">-- 加载失败 --</option>';
  }
}

// 任务类型切换时，重新加载类型详细下拉选项
async function onBfTaskTypeChange() {
  const proj = getSelectedProject();
  const taskType = document.getElementById('bfTaskType').value;
  const detailSel = document.getElementById('bfTypeDetail');

  if (!proj.id || !taskType) {
    detailSel.innerHTML = '<option value="">-- 不填 --</option>';
    return;
  }

  detailSel.innerHTML = '<option value="">-- 加载中... --</option>';

  try {
    const resp = await fetch('/api/gssc/type-detail-options', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({project_id: proj.id, task_type: taskType})
    });
    const data = await resp.json();
    detailSel.innerHTML = '<option value="">-- 不填 --</option>';
    if (data.ok && data.options && data.options.length > 0) {
      data.options.forEach(o => {
        const opt = document.createElement('option');
        opt.value = o.detail || o.value;     // taskTypeParamDetail 值
        opt.dataset.paramId = o.paramId || '';  // taskTypeParamId
        opt.textContent = o.value;            // 显示文本
        detailSel.appendChild(opt);
      });
      // 恢复上次选择的类型详细
      if (window._bfLastTypeDetail && window._bfLastTaskType === taskType) {
        // 精确匹配 option 的 value
        for (let i = 0; i < detailSel.options.length; i++) {
          if (detailSel.options[i].value === window._bfLastTypeDetail ||
              detailSel.options[i].dataset.paramId === window._bfLastTypeDetailParamId) {
            detailSel.selectedIndex = i;
            break;
          }
        }
      }
    }
  } catch(e) {
    detailSel.innerHTML = '<option value="">-- 加载失败 --</option>';
    console.error(e);
  }
}

// 任务名称切换时，刷新工作描述默认值
async function onBfTaskChange() {
  const taskName = document.getElementById('bfTaskName').value;
  document.getElementById('bfJobDesc').placeholder = taskName || '输入工作描述...';
  // 同时触发类型详细重新加载
  onBfTaskTypeChange();
}

function getSelectedProject() {
  const sel = document.getElementById('bfProject');
  const id = sel.value;
  const opt = sel.options[sel.selectedIndex];
  const name = opt.textContent;
  const type = opt.dataset.type || 'MH';
  return { id, name, type };
}

function getSelectedTask() {
  const taskName = document.getElementById('bfTaskName').value;
  return bfTaskMap[taskName] || null;
}

async function doBatchFillPreview() {
  const proj = getSelectedProject();
  if (!proj.id) { setBfStatus('⚠️ 请先选择项目', 'error'); return; }
  const task = getSelectedTask();
  if (!task) { setBfStatus('⚠️ 请先选择任务名称', 'error'); return; }

  const config = buildBfConfig(proj, true);
  setBfStatus('<span class="spin">⏳</span>&nbsp;正在预览...', 'info');
  document.getElementById('btnBatchFill').disabled = true;

  try {
    const resp = await fetch('/api/gssc/batch-fill', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(config)
    });
    const data = await resp.json();

    if (data.ok && data.filled && data.filled.length > 0) {
      renderBatchFillPreview(data.filled, data.summary);
      setBfStatus('✅ 预览完成，确认后可提交', 'ok');
    } else if (data.dry_run && data.summary) {
      document.getElementById('batchFillResult').innerHTML =
        `<div style="color:#fa8c16;padding:10px;background:#fff7e6;border-radius:8px;">${data.summary}</div>`;
      setBfStatus(data.summary, 'info');
    } else {
      setBfStatus(`❌ ${data.error || '预览失败'}`, 'error');
    }
  } catch(e) {
    setBfStatus(`❌ 请求异常: ${e.message}`, 'error');
  } finally {
    document.getElementById('btnBatchFill').disabled = false;
  }
}

async function doBatchFillSubmit() {
  const proj = getSelectedProject();
  if (!proj.id) { setBfStatus('⚠️ 请先选择项目', 'error'); return; }
  const task = getSelectedTask();
  if (!task) { setBfStatus('⚠️ 请先选择任务名称', 'error'); return; }

  const config = buildBfConfig(proj, false);
  setBfStatus('<span class="spin">⏳</span>&nbsp;正在提交批量填写...', 'info');
  document.getElementById('btnBatchFill').disabled = true;

  try {
    const resp = await fetch('/api/gssc/batch-fill', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(config)
    });
    const data = await resp.json();

    if (data.ok) {
      renderBatchFillResult(data);
      setBfStatus('✅ ' + data.summary, 'ok');
      // 保存本次配置到 localStorage，下次打开自动恢复
      saveLastBfConfig();
    } else {
      let errHtml = `<div style="color:#ff4d4f;padding:10px;background:#fff2f0;border-radius:8px;">
        <b>填写失败</b>: ${data.error || '未知错误'}`;
      if (data.errors && data.errors.length > 0) {
        errHtml += '<ul style="margin:6px 0 0 16px;font-size:12px;">';
        data.errors.forEach(e => { errHtml += `<li>${e.date}: ${e.error}</li>`; });
        errHtml += '</ul>';
      }
      errHtml += '</div>';
      document.getElementById('batchFillResult').innerHTML = errHtml;
      setBfStatus('❌ 填写失败', 'error');
    }
  } catch(e) {
    setBfStatus(`❌ 请求异常: ${e.message}`, 'error');
  } finally {
    document.getElementById('btnBatchFill').disabled = false;
  }
}

function buildBfConfig(proj, dryRun) {
  const hoursVal = parseFloat(document.getElementById('bfHours').value) || 8;
  const pType = proj.type || "MH";
  const pTypeNameMap = {"MH": "工时项目", "RD": "研发项目"};
  const pTypeName = pTypeNameMap[pType] || pType;
  const task = getSelectedTask();
  const detailSel = document.getElementById('bfTypeDetail');
  const typeDetail = detailSel.value;
  const typeDetailParamId = detailSel.selectedIndex >= 0 ? (detailSel.options[detailSel.selectedIndex].dataset.paramId || '') : '';
  const jobDesc = (document.getElementById('bfJobDesc').value || '').trim();

  return {
    project_id: proj.id,
    project_name: proj.name,
    project_type: pType,
    project_type_name: pTypeName,
    task_type: document.getElementById('bfTaskType').value,
    task_name: task ? task.taskName : '',
    task_id: task ? task.taskId : '',
    job_desc: jobDesc || (task ? task.taskName : ''),
    type_detail: typeDetail,  // 类型详细（taskTypeParamDetail）
    type_detail_param_id: typeDetailParamId,  // taskTypeParamId
    start_date: document.getElementById('bfStartDate').value,
    end_date: document.getElementById('bfEndDate').value,
    hours_per_day: hoursVal,
    skip_filled: document.getElementById('bfSkipFilled').checked,
    skip_weekend: document.getElementById('bfSkipWeekend').checked,
    dry_run: dryRun,
  };
}

// 保存当前表单配置到 localStorage，下次打开自动恢复
function saveLastBfConfig() {
  const proj = getSelectedProject();
  const task = getSelectedTask();
  const detailSel = document.getElementById('bfTypeDetail');
  const config = {
    project_id: proj.id,
    project_name: proj.name,
    task_type: document.getElementById('bfTaskType').value,
    task_name: document.getElementById('bfTaskName').value,
    type_detail: detailSel.value,
    type_detail_param_id: detailSel.selectedIndex >= 0 ? (detailSel.options[detailSel.selectedIndex].dataset.paramId || '') : '',
    job_desc: (document.getElementById('bfJobDesc').value || '').trim(),
    hours_per_day: parseFloat(document.getElementById('bfHours').value) || 8,
    skip_filled: document.getElementById('bfSkipFilled').checked,
    skip_weekend: document.getElementById('bfSkipWeekend').checked,
  };
  localStorage.setItem('bf_last_config', JSON.stringify(config));
}

function renderBatchFillPreview(filledList, summary) {
  const totalHours = filledList.reduce((s, t) => s + t.hours, 0);
  const weekdays = ['日', '一', '二', '三', '四', '五', '六'];

  let html = `<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;">
    <div class="bf-stat-card"><div class="bf-stat-num">${filledList.length}</div><div class="bf-stat-lbl">天</div></div>
    <div class="bf-stat-card"><div class="bf-stat-num">${totalHours}</div><div class="bf-stat-lbl">总工时(h)</div></div>
    <div class="bf-stat-card"><div class="bf-stat-num">${(totalHours / filledList.length).toFixed(1)}</div><div class="bf-stat-lbl">日均(h)</div></div>
  </div>`;

  html += `<table class="bf-preview-table"><thead><tr>
    <th>日期</th><th>星期</th><th>工时(h)</th><th>项目</th><th>任务</th>
  </tr></thead><tbody>`;
  filledList.forEach(t => {
    const dt = new Date(t.date);
    html += `<tr>
      <td><b>${t.date}</b></td>
      <td>${weekdays[dt.getDay()]}</td>
      <td>${t.hours}h</td>
      <td>${t.projectName}</td>
      <td>${t.taskName}</td>
    </tr>`;
  });
  html += '</tbody></table>';

  document.getElementById('batchFillResult').innerHTML = html;
}

function renderBatchFillResult(data) {
  const filled = data.filled || [];
  const totalHours = filled.reduce((s, t) => s + (t.hours || 0), 0);

  let html = `<div class="bf-stat-card" style="background:#f6ffed;border-color:#b7eb8f;">
    <div class="bf-stat-num" style="color:#52c41a">${filled.length}</div>
    <div class="bf-stat-lbl">天已提交</div>
  </div>
  <div class="bf-stat-card" style="background:#f6ffed;border-color:#b7eb8f;">
    <div class="bf-stat-num" style="color:#52c41a">${totalHours}</div>
    <div class="bf-stat-lbl">总工时(h)</div>
  </div>`;


  if (filled.length <= 20) {
    html += '<table class="bf-preview-table" style="margin-top:8px;"><thead><tr>'
      + '<th>日期</th><th>工时(h)</th><th>任务</th></tr></thead><tbody>';
    filled.forEach(t => {
      html += `<tr><td>${t.date}</td><td>${t.hours}h</td><td>${t.taskName}</td></tr>`;
    });
    html += '</tbody></table>';
  } else {
    html += `<p style="font-size:13px;color:#52c41a;margin-top:8px;">
      已提交 ${filled.length} 天的工时记录（${filled[0].date} ~ ${filled[filled.length-1].date}）</p>`;
  }

  document.getElementById('batchFillResult').innerHTML = html;
}

function setBfStatus(msg, type) {
  const el = document.getElementById('batchFillStatus');
  el.innerHTML = msg;
  el.style.color = type === 'error' ? '#ff4d4f' : type === 'ok' ? '#52c41a' : '#1677ff';
}

// 页面加载完成后初始化批量填写
initBatchFill();
</script>
</body>
</html>"""


def run_web(host="127.0.0.1", port=5002):
    """启动 Web 服务（仅使用 Python 标准库，零依赖）"""
    import threading
    import webbrowser
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import urlparse

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body, content_type="application/json; charset=utf-8"):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self):
            length = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(length).decode("utf-8")

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/" or path == "/index.html":
                data = _HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, {"ok": False, "error": "Not Found"})

        def do_POST(self):
            path = urlparse(self.path).path
            raw = self._read_body()
            try:
                body = json.loads(raw)
            except Exception:
                body = {}

            if path == "/api/check":
                print(f"[Web] 接收检测请求: {body.get('start_date')} ~ {body.get('end_date')}")
                result = run_check(
                    start_date=body.get("start_date"),
                    end_date=body.get("end_date"),
                )
                self._send(200, result)

            elif path == "/api/send":
                msg = body.get("msg", "")
                at_users = body.get("at_users", [])
                at_mobiles = body.get("at_mobiles", [])
                print(f"[Web] 发送钉钉通知，@ {len(at_users)} 人")
                if not msg:
                    self._send(400, {"ok": False, "error": "消息内容为空"})
                else:
                    success, info = send_dingtalk(msg, at_users=at_users, at_mobiles=at_mobiles)
                    self._send(200, {"ok": success, "error": "" if success else info})

            elif path == "/api/overtime":
                month = body.get("month")
                start_month = body.get("start_month")
                end_month = body.get("end_month")
                ess_user = body.get("username") or None   # 空字符串转None，走默认配置
                ess_pwd  = body.get("password")  or None   # 空字符串转None，走默认配置
                # 如果前端明确传了空值，返回错误提示
                if body.get("username") == "" or body.get("password") == "":
                    self._send(200, {"ok": False, "error": "请先输入 ESS 工号和密码"})
                    return
                mode_desc = f"{start_month}~{end_month}" if (start_month and end_month and start_month != end_month) else (month or "本月")
                print(f"[Web] 查询加班时间，月份: {mode_desc}，工号: {ess_user or '默认'}")
                result = run_overtime_check(
                    month=month, start_month=start_month, end_month=end_month,
                    username=ess_user, password=ess_pwd
                )
                self._send(200, result)

            elif path == "/api/gssc/projects":
                # 获取GSSC项目列表
                token = login()
                if not token:
                    self._send(200, {"ok": False, "error": "登录失败"})
                    return
                projects = gssc_get_projects(token)
                self._send(200, {"ok": True, "projects": projects})

            elif path == "/api/gssc/task-types":
                token = login()
                if not token:
                    self._send(200, {"ok": False, "error": "登录失败"})
                    return
                task_types = gssc_get_task_types(token)
                self._send(200, {"ok": True, "task_types": task_types})

            elif path == "/api/gssc/project-tasks":
                # 获取指定项目的任务列表（过滤当前用户科室）
                project_id = body.get("project_id")
                if not project_id:
                    self._send(200, {"ok": False, "error": "缺少 project_id"})
                    return
                token = login()
                if not token:
                    self._send(200, {"ok": False, "error": "登录失败"})
                    return
                user_info = get_user_info(token)
                org_id = user_info.get("areaTeamId", "")
                tasks = gssc_get_project_tasks(token, project_id, org_id)
                self._send(200, {"ok": True, "tasks": tasks})

            elif path == "/api/gssc/type-detail-history":
                # 已废弃 - 改为 type-detail-options（从 IR/CR 等 API 拉取真实选项）
                self._send(200, {"ok": True, "values": []})

            elif path == "/api/gssc/type-detail-options":
                # 获取类型详细的下拉选项（根据任务类型调用不同API：IR/CR/JIRA_CR等）
                project_id = body.get("project_id")
                task_type = body.get("task_type")
                if not project_id or not task_type:
                    self._send(200, {"ok": False, "error": "缺少参数"})
                    return
                token = login()
                if not token:
                    self._send(200, {"ok": False, "error": "登录失败"})
                    return
                options = gssc_get_type_detail_options(token, project_id, str(task_type))
                self._send(200, {"ok": True, "options": options})

            elif path == "/api/gssc/calendar":
                start_date = body.get("start_date")
                end_date = body.get("end_date")
                if not start_date or not end_date:
                    self._send(200, {"ok": False, "error": "请选择日期范围"})
                    return
                token = login()
                if not token:
                    self._send(200, {"ok": False, "error": "登录失败"})
                    return
                cal_data = gssc_get_calendar(token, start_date, end_date)
                self._send(200, {"ok": True, "calendar": cal_data})

            elif path == "/api/gssc/batch-fill":
                # 批量填写工时
                config = body
                dry_run = body.get("dry_run", False)
                print(f"[Web] 批量填写工时 {'[预览]' if dry_run else ''}, 项目={config.get('project_name')}, "
                      f"日期={config.get('start_date')}~{config.get('end_date')}")
                token = login()
                if not token:
                    self._send(200, {"ok": False, "error": "GSSC登录失败"})
                    return
                result = gssc_batch_fill_hours(token, config)
                self._send(200, result)

            else:
                self._send(404, {"ok": False, "error": "Not Found"})

        def log_message(self, format, *args):
            # 精简日志
            pass

    url = f"http://localhost:{port}"
    print(f"\n{'='*50}")
    print("  GSSC 工时检测 Web 服务已启动")
    print(f"  访问地址: {url}")
    print(f"{'='*50}\n")

    server = HTTPServer((host, port), Handler)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # 1.2 秒后自动打开浏览器
    def _open_browser():
        time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[提示] Web 服务已关闭")
        server.server_close()


# ============ 命令行模式（保持原有逻辑） ============
def main():
    import datetime

    print("=" * 60)
    print("GSSC工时系统 - 未写工时人员检测")
    print(f"时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    result = run_check()
    if not result["ok"]:
        print(f"[错误] {result['error']}")
        sys.exit(1)

    analysis = result["analysis"]
    print(f"\n[结果] 统计汇总:")
    print(f"  未填写: {len(analysis['no_hours'])}人（<3天: {len(analysis.get('no_hours_lt3', []))}人, 3~7天: {len(analysis.get('no_hours_3to7', []))}人, >7天: {len(analysis.get('no_hours_gt7', []))}人）")
    print(f"  不足8h: {len(analysis['partial'])}人")
    print(f"  正常: {len(analysis['ok'])}人")

    msg, at_users, at_mobiles = result["msg"], result["at_users"], result["at_mobiles"]
    print(f"\n[钉钉] 发送通知（将 @ {len(at_users)} 人，手机号 @ {len(at_mobiles)} 人）...")
    send_dingtalk(msg, at_users=at_users, at_mobiles=at_mobiles)
    print("\n[完成] 全部流程结束")


if __name__ == "__main__":
    # 默认启动 Web 模式；加 --cli 参数可切换为命令行模式
    if "--overtime" in sys.argv:
        # 加班时间查询模式
        import datetime
        # 支持 --month YYYYMM（单月）或 --start-month YYYYMM --end-month YYYYMM（跨月）
        month_arg = None
        start_month_arg = None
        end_month_arg = None
        args = sys.argv[1:]
        for i, arg in enumerate(args):
            if arg == "--month" and i + 1 < len(args):
                month_arg = args[i + 1]
            elif arg == "--start-month" and i + 1 < len(args):
                start_month_arg = args[i + 1]
            elif arg == "--end-month" and i + 1 < len(args):
                end_month_arg = args[i + 1]

        print("=" * 60)
        print("ESS考勤系统 - 个人加班时间查询")
        print(f"时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        result = run_overtime_check(
            month=month_arg, start_month=start_month_arg, end_month=end_month_arg
        )
        if not result["ok"]:
            print(f"[错误] {result['error']}")
            sys.exit(1)

        pi = result.get("person_info", {})
        is_cross = result.get("months") and len(result["months"]) > 1
        print(f"\n[结果] 人员: {pi.get('name', '')}（{pi.get('number', '')}）")
        print(f"  部门: {pi.get('department', '')}")
        print(f"  查询范围: {result.get('month', '本月')}")
        print(f"  应出勤: {result.get('should_work', '-')}天")
        print(f"  实出勤: {result.get('actual_work', '-')}天")
        print(f"  加班合计: {result.get('total_overtime', 0)}小时")

        # 跨月汇总
        if result.get("months_summary"):
            print(f"\n  各月加班汇总:")
            for ms in result["months_summary"]:
                print(f"    {ms['label']}: {ms['total_overtime']}h（加班{ms['overtime_days_count']}天，应出勤{ms.get('should_work', '-')}天，实出勤{ms.get('actual_work', '-')}天）")

        overtime_days = result.get("overtime_days", [])
        if overtime_days:
            print(f"\n  加班明细:")
            print(f"  {'日期':<14} {'星期':<8} {'上班':<10} {'下班':<10} {'在岗':<8} {'加班':<6} {'备注':<10}")
            print(f"  {'-'*66}")
            for d in overtime_days:
                clock_in = d["clock_in"].split(" ")[1] if " " in d["clock_in"] else d["clock_in"]
                clock_out = d["clock_out"].split(" ")[1] if " " in d["clock_out"] else d["clock_out"]
                note = "8:30起算" if d.get("was_adjusted") else ""
                print(f"  {d['date']:<14} {d['week']:<8} {clock_in:<10} {clock_out:<10} {d['total_hours']}h{'':<5} {d['overtime_hours']}h{'':<4} {note}")

        print("\n[完成] 加班时间查询结束")
    elif "--cli" in sys.argv:
        main()
    else:
        run_web()
