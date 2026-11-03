"""智慧树问答区（qah5）：用 Playwright 复用已登录 Cookie 取题、提问、答题。

为什么不直接用 requests：问答区每个请求都要 `secretStr`，它由页面里的全局函数
`yxyz(params, dynStr)` 生成，`dynStr` 每次登录都会变；仓库现有的 `ZHSApi.secure()`
只覆盖 onlineservice 的 AES 方案，拿不到 creditqa 的签名材料。让页面自己算签名，
就不用逆向，也不用维护密钥。Playwright 已是本项目依赖（见 `ZHSMain/slider.py`）。

用法：

    api = ZHSApi(access_token=saved_token)          # 复用扫码登录的 Cookie
    with QaBridge(api.access_token) as qa:
        items = qa.questions(1000119877, 426135, max_answers=1, limit=10)
        qa.publish(1000119877, 426135, items[0]["id"], "……")

失败语义与仓库其余部分一致：登录过期抛 `AuthenticationExpired`，平台要验证抛
`CaptchaRequired`，其余接口错误抛 `ZHSApiError`。
"""
from __future__ import annotations

import json
import time

from MoocMain.log import Logger
from base.auth import PlatformUnavailable
from ZHSMain.api import AuthenticationExpired, CaptchaRequired, ZHSApiError

logger = Logger(__name__).get_log()

QA_URL = "https://qah5.zhihuishu.com/qa.html#/web/home/%s?role=2&recruitId=%s"
GATEWAY = "https://creditqa-api.zhihuishu.com/creditqa/gateway/t/v1/web/qa/"

# 页面里注入一次：把签名和 fetch 都交给站点自己算，Python 侧只管传参。
BRIDGE_JS = """
window.__studiaQa = async (path, params) => {
  const store = document.querySelector('#app').__vue__.$store.state.baseData;
  const body = 'dateFormate=' + Date.now() +
    '&secretStr=' + encodeURIComponent(window.yxyz(params, store.dynStr));
  const r = await fetch('%s' + path, {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body,
  });
  return await r.json();
};
""" % GATEWAY

LIST_JS = """
async ({courseId, recruitId, maxAnswers, limit, pageSize}) => {
  const out = [];
  let pageIndex = 0, guard = 0;
  while (out.length < limit && guard++ < 40) {
    const j = await window.__studiaQa('getRecommendList',
      {courseId, recruitId, pageIndex, pageSize});
    const list = (j.rt && j.rt.questionInfoList) || [];
    if (!list.length) break;
    for (const q of list) {
      if (q.answerNum <= maxAnswers) {
        out.push({id: q.questionId, content: q.content, answers: q.answerNum,
                  user: (q.userDto || {}).username || '', time: q.timeStr || '',
                  chapter: q.chapterName || ''});
      }
    }
    pageIndex = (j.rt && j.rt.pageIndex) || pageIndex + list.length;
  }
  return out.slice(0, limit);
}
"""

PUBLISH_JS = """
async ({qid, content, courseId, recruitId}) => {
  const store = document.querySelector('#app').__vue__.$store.state.baseData;
  return await window.__studiaQa('saveAnswer', {
    annexs: [], qid, source: store.roleId, aContent: content,
    courseId, recruitId, validate: '', saveSource: 1,
  });
}
"""


ASK_JS = """
async ({content, courseId, recruitId, chapterId}) => {
  const store = document.querySelector('#app').__vue__.$store.state.baseData;
  return await window.__studiaQa('saveQuestion', {
    annexs: [], content, courseId, recruitId, chapterId: chapterId || 0,
    sourceType: store.roleId, validate: '', saveSource: 1,
  });
}
"""


def _playwright_cookies(raw):
    """ZHSApi.access_token 的 JSON 直接喂给 Playwright，只保留智慧树域。"""
    cookies = json.loads(raw or "[]")
    return [{"name": c["name"], "value": c["value"],
             "domain": c.get("domain") or ".zhihuishu.com", "path": c.get("path") or "/",
             "secure": bool(c.get("secure", True)),
             **({"expires": c["expires"]} if c.get("expires") else {})}
            for c in cookies if "zhihuishu.com" in (c.get("domain") or "")]


class QaBridge:
    """一次任务共用一个无头浏览器；页面加载失败或跳登录页按登录过期处理。"""

    def __init__(self, access_token, *, course_id, recruit_id, headless=True, timeout=30000):
        self.cookies = _playwright_cookies(access_token)
        self.course_id, self.recruit_id = int(course_id), int(recruit_id)
        self.headless, self.timeout = headless, timeout
        self.url = QA_URL % (self.course_id, self.recruit_id)
        self._pw = self._browser = self._page = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright  # 只有这个功能需要，延迟导入

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=self.headless)
            context = self._browser.new_context()
            context.add_cookies(self.cookies)
            self._page = context.new_page()
            self._prepare()
        except BaseException:
            # with 只在 __enter__ 成功后才会调用 __exit__，失败时浏览器和 Playwright
            # 的事件循环会留在当前线程：Playwright 把该循环标成 running，之后
            # prompt_toolkit 的 asyncio.run() 会直接抛错，整个终端界面随即退出。
            self.close()
            raise
        return self

    def _prepare(self):
        """打开问答区并注入签名桥。登录后第一次进站会被 SSO 弹一次，重试即可。"""
        from playwright.sync_api import Error as PlaywrightError

        reason = ""
        for _ in range(3):
            try:
                self._page.goto(self.url, wait_until="domcontentloaded", timeout=self.timeout)
                self._page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightError as exc:
                reason = str(exc)
                time.sleep(1.5)
                continue
            if any(part in self._page.url for part in ("/login", "passport.")):
                raise AuthenticationExpired("智慧树登录已过期，请重新扫码")
            try:
                # dynStr 要等首页数据回来才是字符串，早读会拿到 null，
                # 签名函数只会抛一句看不懂的 TypeError。
                self._page.wait_for_function(
                    "() => window.yxyz && (() => { const app = document.querySelector('#app');"
                    " const base = app && app.__vue__ && app.__vue__.$store.state.baseData;"
                    " return !!base && typeof base.dynStr === 'string' && base.dynStr.length > 0; })()",
                    timeout=25000)
                self._page.evaluate(BRIDGE_JS)
                return
            except PlaywrightError as exc:
                reason = str(exc)
                time.sleep(1.5)  # 页面在跳转时执行上下文会被销毁，再开一次
        logger.warning("问答区页面没有加载完成：%s", reason.splitlines()[0] if reason else "未知原因")
        raise PlatformUnavailable("问答区页面没有加载完成，请稍后重试")

    def _eval(self, script, arg):
        try:
            return self._page.evaluate(script, arg)
        except Exception as exc:  # 运行中页面刷新：重新注入一次再试
            if "Execution context was destroyed" not in str(exc):
                raise
            self._prepare()
            return self._page.evaluate(script, arg)

    def close(self):
        for close in (getattr(self._browser, "close", None), getattr(self._pw, "stop", None)):
            if close:
                try:
                    close()
                except Exception:  # 浏览器已经退出时忽略
                    logger.debug("关闭问答区浏览器失败", exc_info=True)

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- 取题

    def questions(self, max_answers=1, limit=10, page_size=50):
        items = self._eval(LIST_JS, {"courseId": self.course_id, "recruitId": self.recruit_id,
                                        "maxAnswers": int(max_answers), "limit": int(limit),
                                        "pageSize": int(page_size)})
        logger.info("问答区取到 %d 条（回答数≤%d）", len(items), max_answers)
        return items

    # ---------------------------------------------------------------- 发布

    def publish(self, question_id, text):
        result = self._eval(PUBLISH_JS, {"qid": int(question_id), "content": text,
                                        "courseId": self.course_id, "recruitId": self.recruit_id})
        if str(result.get("code")) == "1010":
            raise CaptchaRequired("问答区要求完成验证，请在页面上验证后重试")
        if str(result.get("status")) != "200":
            raise ZHSApiError(result.get("code") or result.get("status"),
                              result.get("msg") or "发布回答失败")
        return (result.get("rt") or {}).get("answerId")


    # ---------------------------------------------------------------- 提问

    def ask(self, content, chapter_id=0):
        """在课程问答区发一条提问。chapterId 传 0 表示不挂到具体章节（课程问答）。"""
        result = self._eval(ASK_JS, {"content": content, "courseId": self.course_id,
                                     "recruitId": self.recruit_id, "chapterId": int(chapter_id or 0)})
        if str(result.get("code")) == "1010":
            raise CaptchaRequired("问答区要求完成验证，请在页面上验证后重试")
        if str(result.get("status")) != "200":
            raise ZHSApiError(result.get("code") or result.get("status"),
                              result.get("msg") or "发布提问失败")
        return (result.get("rt") or {}).get("questionId")


def answer_questions(qa, ai, *, max_answers=1, limit=10, interval=60, state_path=None,
                     should_stop=lambda: False, on_event=lambda kind, name, status="": None,
                     sleep=time.sleep):
    """取题 → AI 生成 → 发布，返回统计。ctx 只需 questions/publish 两个方法，便于测试替身。"""
    seen = set()
    if state_path:
        try:
            with open(state_path, encoding="utf-8") as handle:
                seen = set(json.load(handle))
        except (OSError, ValueError):
            seen = set()
    items = [q for q in qa.questions(max_answers=max_answers, limit=limit * 3) if q["id"] not in seen][:limit]
    stats = {"answered": 0, "skipped": 0, "failed": 0, "ids": []}
    on_event("begin", "问答区答题", "running")
    for index, item in enumerate(items, 1):
        if should_stop():
            break
        title = item["content"]
        try:
            answer = ai.answer({"type": "问答题", "title": title}).get("answer")
            if not isinstance(answer, str) or len(answer.strip()) < 30:
                raise ValueError("AI 回答为空或过短")
            answer_id = qa.publish(item["id"], answer.strip()[:1000])
        except CaptchaRequired:
            raise
        except Exception as exc:  # 单题失败不影响队列，与 ai/runner.py 的做法一致
            stats["failed"] += 1
            logger.warning("问答区第 %d 题失败：%s", index, exc)
            on_event("result", title[:20], "error")
            continue
        if not answer_id:
            stats["skipped"] += 1
            on_event("result", title[:20], "skipped")
            continue
        stats["answered"] += 1
        stats["ids"].append(item["id"])
        seen.add(item["id"])
        on_event("result", title[:20], "success")
        if state_path:
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump(sorted(seen), handle)
        if index < len(items) and interval:
            sleep(interval)
    on_event("done", "问答区答题", "done")
    return stats


# 让模型的「问答题」能力产出提问正文：它只需把问题写成答案，答卷格式不变。
QUESTION_RULES = (
    "只输出问题本身：一到两句话，具体到可以讨论；不要称呼老师、同学或「大家」，"
    "不要「请问」「想请教」「想问一下」这类客套，"
    "不要「作为大学生」「我们」这种身份和立场铺垫，不要自我介绍，"
    "不要序号和 Markdown，不要自己作答。"
)
QUESTION_PROMPT = (
    "这门课是「%s」。请围绕「%s」写第 %d 个用于课程问答区的问题。\n" + QUESTION_RULES
)
# 出题的选题池：问答区里回答数最少的那些提问，让模型挑一个还没问透的方向。
QUESTION_FROM_REFERENCE = (
    "这门课是「%s」。课程问答区里回答较少的提问有：\n%s\n"
    "请从中挑一个还没问透的方向，写第 %d 个新的提问；"
    "不要重复上面任何一条，也不要换个说法问同一件事。\n" + QUESTION_RULES
)
MIN_QUESTION, MAX_QUESTION = 8, 200
REFERENCE_ANSWERS = 1      # 参考题目只取回答数为 0–1 的
MAX_REFERENCES = 8         # 塞进提示词的参考题目数量上限
MAX_REFERENCE_CHARS = 70   # 每条参考题目截到这个长度

# 模型仍可能开头带称呼或客套，发布前再剥一层。长前缀先匹配；True 直接去掉，
# False 要求后面紧跟标点——否则「老师布置的作业」会被削成「布置的作业」。
LEAD_INS = (
    ("各位老师好", True), ("老师们好", True), ("老师您好", True), ("老师好", True),
    ("各位同学好", True), ("同学们好", True), ("同学好", True),
    ("请问一下", True), ("请问大家", True), ("请问各位", True), ("请问老师", True), ("请问", True),
    ("想请教一下", False), ("请教一下", False), ("想问一下", False), ("想请教", False), ("想问", False),
    ("各位老师", False), ("各位同学", False), ("同学们", False), ("老师", False), ("同学", False),
)
MARKS = "，,、：:！!。. "
MAX_LEAD_INS = 4


def _strip_lead_ins(text):
    for _ in range(MAX_LEAD_INS):
        text = text.lstrip(MARKS)
        for lead, plain in LEAD_INS:
            if not text.startswith(lead):
                continue
            rest = text[len(lead):]
            if plain or not rest or rest[0] in MARKS:
                text = rest.lstrip(MARKS)
                break
        else:
            break
    return text


def _clean_question(text):
    """只留问题本身：去掉称呼、客套、引号、序号和多余空白。"""
    text = " ".join(str(text or "").split()).strip("\"'“”‘’` ")
    if len(text) > 2 and text[0].isdigit() and text[1] in ".、)）":
        text = text[2:].lstrip()
    return _strip_lead_ins(text)


def write_questions(ai, reference, count, *, course_name="", on_progress=None):
    """参考回答较少的提问，让模型写 count 条新提问。

    reference 是问答区里回答数较少的提问正文；为空时退回到课程主题。
    与参考题目重复（或模型自己重复）的跳过。on_progress(序号, 总数) 在每次请求前调用。
    """
    reference = [str(text).strip()[:MAX_REFERENCE_CHARS]
                 for text in (reference or []) if str(text).strip()][:MAX_REFERENCES]
    bullets = "\n".join("- " + text for text in reference)
    total = max(1, int(count))
    texts = []
    for index in range(1, total + 1):
        if reference:
            prompt = QUESTION_FROM_REFERENCE % (course_name or "这门课", bullets, index)
        else:
            prompt = QUESTION_PROMPT % (course_name or "这门课", "课程的核心概念", index)
        if on_progress is not None:
            on_progress(index, total)
        answer = ai.answer({"type": "问答题", "title": prompt}).get("answer")
        text = _clean_question(answer)
        if not MIN_QUESTION <= len(text) <= MAX_QUESTION:
            logger.warning("AI 给出的第 %d 条提问不可用：%s", index, text[:40])
            continue
        if text in texts or text in reference:
            logger.warning("AI 重复了已有的提问，已跳过：%s", text[:40])
            continue
        texts.append(text)
    return texts


def publish_questions(qa, texts, *, interval=60, should_stop=lambda: False,
                      on_event=lambda kind, name, status="": None, sleep=time.sleep):
    """逐条发布提问，返回统计。qa 只需 ask 一个方法，便于测试替身。"""
    stats = {"published": 0, "failed": 0, "ids": []}
    on_event("begin", "问答区提问", "running")
    for index, text in enumerate(texts, 1):
        if should_stop():
            break
        title = text[:20]
        try:
            question_id = qa.ask(text)
        except CaptchaRequired:
            raise
        except Exception as exc:  # 一条失败不影响后续提问
            stats["failed"] += 1
            logger.warning("问答区第 %d 条提问失败：%s", index, exc)
            on_event("result", title, "error")
            continue
        if not question_id:
            stats["failed"] += 1
            on_event("result", title, "error")
            continue
        stats["published"] += 1
        stats["ids"].append(question_id)
        on_event("result", title, "success")
        if index < len(texts) and interval:
            sleep(interval)
    on_event("done", "问答区提问", "done")
    return stats
