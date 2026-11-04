"""Current web exam protocol, verified against public web assets on 2026-10-10.

No speculative endpoints, automatic POST retries, or foreign account sessions.
"""
import base64
import json
import math
import re
import time
from urllib.parse import quote

import requests

from AIMoocMain.export_questions import unwrap_paper

DIGITS = {c: i for i, c in enumerate("零一二三四五六七八九")}


def paper_number(item):
    """名字里第一个序号：专题五 → 5，模块 3 → 3，第二次测试 → 2；没有序号的排在后面，彼此保持原序。"""
    match = re.search(r"\d+|[零一二三四五六七八九十]+", str(item.get("name") or ""))
    if not match:
        return math.inf
    text = match.group()
    if text.isdigit():
        return int(text)
    if "十" in text:  # 十、十二、二十、二十三
        tens, _, ones = text.partition("十")
        return DIGITS.get(tens[-1:], 1) * 10 + DIGITS.get(ones[:1], 0)
    return int("".join(str(DIGITS[c]) for c in text))


class ExamError(RuntimeError):
    pass


class ExamServiceError(ExamError):
    """Authentication, rate limits or unavailable transport affect the queue."""


class TaskUnavailableError(ExamError):
    """The catalog window/type forbids opening this task; other tasks may run."""


def checked_list(payload):
    """An unknown response structure must not mean 'no previous attempts'."""
    if isinstance(payload, list):
        if not all(isinstance(x, dict) for x in payload):
            raise ExamError("平台列表包含未知数据")
        return payload
    if isinstance(payload, dict):
        for key in ("rows", "list", "records", "data", "result"):
            value = payload.get(key)
            if isinstance(value, (list, dict)):
                return checked_list(value)
    raise ExamError("平台列表结构未知，停止处理")


class ExamAPI:
    def __init__(self, client, session=None):
        self.user_id = client.user_id
        self.base_url = client.default_base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + (client.access_token or "")})

    def request(self, method, endpoint, **kwargs):
        try:
            response = self.session.request(method, self.base_url + endpoint, timeout=(10, 30), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise ExamServiceError("平台请求超时或连接失败；写入结果可能未知，请在网页核对，程序不会自动重试") from None
        if response.status_code in (401, 429) or response.status_code >= 500:
            raise ExamServiceError("平台登录失效、请求受限或服务不可用（HTTP %s）" % response.status_code)
        if response.status_code != 200:
            raise ExamError("平台返回 HTTP %s" % response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise ExamError("平台返回非 JSON 数据") from None
        if isinstance(payload, dict) and str(payload.get("code")) in ("401", "429"):
            raise ExamServiceError("平台登录失效或请求受限（code=%s）" % payload["code"])
        # Unlike catalog/history/write endpoints, the current paper endpoint
        # returns the paper directly, without a code/data envelope. Limit this
        # exception to a validated GET paper shape; explicit error codes still fail.
        if (method == "GET" and endpoint == "/course/exam/paper"
                and isinstance(payload, dict) and "code" not in payload
                and isinstance(payload.get("questions"), list)
                and isinstance(payload.get("taskExamRecord"), dict)
                and payload["taskExamRecord"].get("id")):
            return payload
        if not isinstance(payload, dict) or str(payload.get("code")) != "200":
            raise ExamError("平台拒绝请求（code=%s）；请在网页检查开放时间、权限及作答条件" % (payload.get("code") if isinstance(payload, dict) else "unknown"))
        return payload

    def catalog(self, course, category):
        items, seen = [], set()
        for page in range(1, 101):
            result = self.request("GET", "/course/exam/record/getExamListByStudent", params={
                "courseInfoId": course.get("id") or course.get("courseInfoId"),
                "courseId": course["courseId"], "categoryId": category,
                "pageNum": page, "pageSize": 100,
            })
            rows = checked_list(result)
            fresh = [x for x in rows if str(x.get("id") or x.get("examId")) not in seen]
            if rows and not fresh:
                raise ExamError("试卷列表分页重复，停止处理")
            for row in fresh:
                seen.add(str(row.get("id") or row.get("examId")))
                items.append(row)
            if len(rows) < 100:
                # 平台给的顺序是乱的（专题五、二、四、三、一），也没有排序字段：按名字里的序号排。
                return sorted(items, key=paper_number)
        raise ExamError("试卷列表超过分页上限")

    def records(self, exam_id):
        if not self.user_id:
            raise ExamError("无法确认当前登录用户 ID")
        result = self.request("GET", "/course/exam/record/getStudentRecordByExamId", params={"examId": exam_id, "studentId": self.user_id})
        return checked_list(result)

    def completion_status(self, exam_id):
        """Read history only: fetching a paper can start an attempt/timer."""
        if not exam_id:
            return "unknown"
        records = self.records(exam_id)
        if not records:
            return "not_started"
        statuses = {str(record.get("status")) for record in records}
        if "2" in statuses:
            return "submitted"
        if statuses == {"0"}:
            return "draft"
        # Other codes have no verified completion meaning in our protocol.
        return "unknown"

    def paper(self, exam_id):
        return unwrap_paper(self.request("GET", "/course/exam/paper", params={"id": exam_id, "groupId": 0}))

    def heartbeat(self, course, exam_id, task_id, seconds):
        return self.request("POST", "/course/exam/record/updateExamTime", json={
            "courseId": course["courseId"], "courseInfoId": course.get("id") or course.get("courseInfoId"),
            "examId": exam_id, "taskId": task_id, "groupId": 0, "examTime": seconds,
        })

    def submit(self, payload):
        # Browser: btoa(encodeURIComponent(JSON.stringify(payload))).
        encoded = base64.b64encode(quote(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), safe="~()*!.'-").encode("ascii")).decode("ascii")
        return self.request("POST", "/course/exam/record", data=encoded, headers={"Content-Type": "application/json;charset=UTF-8"})

    def verify_submitted(self, exam_id, task_id):
        # Status 2 is the submitted record used by upstream PR #198.
        for attempt in range(3):
            if any(str(x.get("id") or x.get("taskId")) == str(task_id) and str(x.get("status")) == "2" for x in self.records(exam_id)):
                return True
            if attempt < 2:
                time.sleep(1)
        return False
