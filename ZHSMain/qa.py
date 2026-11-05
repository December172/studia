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
import random
import re
import time

from MoocMain.log import Logger
from ai.client import AIResponseError
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
    const me = document.querySelector('#app').__vue__.$store.state.baseData.userId;
    for (const q of list) {
      if (q.answerNum <= maxAnswers) {
        out.push({id: q.questionId, content: q.content, answers: q.answerNum,
                  user: (q.userDto || {}).username || '', time: q.timeStr || '',
                  chapter: q.chapterName || '', asker: q.questionUserId, at: q.createTime,
                  mine: q.questionUserId === me});
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
    # 多取很多再筛：最新的提问大多是刷帖和水帖，回答它们会随它们被删，白费一条。
    items = [q for q in worth_answering(qa.questions(max_answers=max_answers, limit=limit * 30))
             if q["id"] not in seen][:limit]
    stats = {"answered": 0, "skipped": 0, "failed": 0, "ids": []}
    on_event("begin", "问答区答题", "running")
    for index, item in enumerate(items, 1):
        if should_stop():
            break
        title = item["content"]
        try:
            answer = ai.answer({"type": "问答题", "title": title}).get("answer")
            # 规则：回答开头不要加「答案：」「回答：」这类词。
            answer = re.sub(r"^\s*(?:答案|回答|答)\s*[:：]\s*", "", answer) if isinstance(answer, str) else answer
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


# 智慧树问答规则（wiki-wenda.zhihuishu.com「问答规则」）：有效提问每条 5 分、最多 10 条；
# 有效回答每条 2 分、最多 20 条；自己的提问下有 5 个有效回答再 1 分、最多 10 条；
# 合计再乘课程互动分占比。数据次日上午更新。封顶以后再发不加分，只多一分被判刷帖的风险。
QUESTION_CAP, ANSWER_CAP = 10, 20
# 每门课每天最多发这么多：规则说短时间内连发、同一句式连发会被删帖，第三方刷帖整学期禁言。
DAILY_QUESTIONS, DAILY_ANSWERS = 5, 10

# 让模型的「问答题」能力产出提问正文：它只需把问题写成答案，答卷格式不变。
QUESTION_RULES = (
    "要求：只输出提问本身，一句话，20 到 60 个字，以一个问号结尾，中间不要再出现问号；"
    "结合这一节讲的内容，问原因、比较、影响，或者提出自己的看法请大家讨论；"
    "不要问能直接搜到固定答案的事实（谁、哪一年、什么时候），"
    "不要写成名词解释（什么是、是什么意思、指什么）或是非题（是不是、对不对、有没有、能不能、是否、……吗）；"
    "不要用「请问」开头，不要称呼老师同学，不要「作为大学生」「我们」这类铺垫；"
    "不要带括号、下划线、序号、选项、答案、链接。"
)
QUESTION_PROMPT = "这门课是「%s」。请围绕课程里「%s」这一节的内容，写一个发到课程问答区的提问。\n" + QUESTION_RULES
QUESTION_AVOID = "\n问答区里已经有这些提问，不要重复，也不要换个说法问同一件事：\n%s"
QUESTION_REDO = "\n上一次写的「%s」不合格：%s。请重写。"
MIN_QUESTION, MAX_QUESTION = 8, 200
QA_PACE = (60, 180)        # 两条回答之间随机隔几秒，每次重抽：间隔一样像脚本；网页和终端共用
# 提问隔得更开：同一个人几分钟里连发几条，正是问答区清理刷帖的样子（实测满屏都是这种帖）。
ASK_PACE = (600, 1200)
REFERENCE_ANSWERS = 1      # 参考题目只取回答数为 0–1 的
MAX_REFERENCES = 8         # 塞进提示词的参考题目数量上限
MAX_REFERENCE_CHARS = 70   # 每条参考题目截到这个长度

# 规则和「什么是无效提问」「审核会删哪些帖」两篇里点名会被判无效或删掉的写法。
QUESTION_FLAWS = (
    (r"^请问", "不要用「请问」开头"),
    (r"^\s*(?:\d+|[一二三四五六七八九十]+)\s*[、.．)）]", "不要加序号"),
    (r"[()（）\[\]【】_＿]", "不要带括号或下划线"),
    (r"https?://|www\.|[Qq][Qq]|微信|公众号", "不要带链接或联系方式"),
    (r"(?:^|[^A-Za-z])[A-D][.．、]", "不要写成选择题"),
    (r"(?<!为)什么是|是什么意思|是什么[?？]$|指什么|指的是什么|的含义|的定义|名词解释", "名词解释类提问会被判无效"),
    (r"是不是|对不对|好不好|有没有|能不能|会不会|可不可以|是否|吗[?？]$", "是非题会被判无效"),
    (r"是谁|哪一年|什么时候|何时", "问人物、时间这类固定答案会被判无效"),
)
BURST = (3, 10 * 60 * 1000)   # 同一个人 10 分钟内发 3 条以上，多半是刷帖，迟早被删
OLDEST = 180 * 24 * 3600 * 1000  # 往期学期的提问，回答也算无效


def question_flaw(text):
    """按智慧树问答规则会被判无效或删掉的毛病；没有返回 None。"""
    text = str(text or "").strip()
    if sum(text.count(mark) for mark in "?？") != 1 or text[-1:] not in ("?", "？"):
        return "要以一个问号结尾，中间不能再有问号"
    for pattern, reason in QUESTION_FLAWS:
        if re.search(pattern, text):
            return reason
    if not MIN_QUESTION <= len(text) <= MAX_QUESTION:
        return "要 %d–%d 个字" % (MIN_QUESTION, MAX_QUESTION)
    return None


def worth_answering(items, now=None):
    """问答区里值得回答的提问。水帖、刷帖、自问自答和往期的提问会被删，回答它们的也一并作废。"""
    now = now or time.time() * 1000
    times = {}
    for item in items:
        if item.get("asker") is not None and item.get("at"):
            times.setdefault(item["asker"], []).append(item["at"])
    count, window = BURST
    bursty = {asker for asker, stamps in times.items()
              if any(len([t for t in stamps if 0 <= t - start <= window]) >= count for start in stamps)}
    return [item for item in items
            if not item.get("mine") and item.get("asker") not in bursty
            and not (item.get("at") and now - item["at"] > OLDEST)
            and question_flaw(item.get("content")) is None]


def qa_room(valid, posted_today, cap, daily):
    """还能发几条：离封顶还差多少、今天还剩多少，取小的。valid 是平台记的有效数，次日才算上今天发的。"""
    return max(0, min(cap - int(valid or 0) - posted_today, daily - posted_today))


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
    """只留问题本身：去掉称呼、客套、引号、序号和多余空白；结尾的英文问号换成中文问号。"""
    text = " ".join(str(text or "").split()).strip("\"'“”‘’` ")
    if len(text) > 2 and text[0].isdigit() and text[1] in ".、)）":
        text = text[2:].lstrip()
    text = _strip_lead_ins(text)
    return text[:-1] + "？" if text.endswith("?") else text


def write_questions(ai, reference, count, *, course_name="", topics=(), on_progress=None):
    """让模型写 count 条新提问，每条围绕课程里的一节（topics，小节名）谈原因、比较或看法。

    reference 是问答区已有的提问，只用来让模型避开：规则说相似度 98% 以上算无效。
    写出来按 question_flaw 检查，不合格带着原因再写，每条最多三次；和已有的重复也重写。
    on_progress(序号, 总数) 在每条开始前调用。
    """
    reference = [str(text).strip()[:MAX_REFERENCE_CHARS]
                 for text in (reference or []) if str(text).strip()][:MAX_REFERENCES]
    avoid = QUESTION_AVOID % "\n".join("- " + text for text in reference) if reference else ""
    topics = [str(t) for t in dict.fromkeys(topics or ()) if str(t).strip()]
    order = random.sample(topics, len(topics)) if topics else []
    total = max(1, int(count))
    texts = []
    for index in range(1, total + 1):
        topic = order[(index - 1) % len(order)] if order else "课程的核心内容"
        base = QUESTION_PROMPT % (course_name or "这门课", topic) + avoid
        if on_progress is not None:
            on_progress(index, total)
        redo = ""
        for _ in range(3):
            try:
                answer = ai.answer({"type": "问答题", "title": base + redo}).get("answer")
            except AIResponseError:
                # 推理模型偶尔只回思考、正文为空或被截断：再要一次。超时等其他错误照常抛出。
                logger.warning("AI 第 %d 条提问的回复不完整，重试", index)
                continue
            text = _clean_question(answer)
            flaw = question_flaw(text) or ("和已有的提问重复" if text in texts or text in reference else None)
            if flaw is None:
                texts.append(text)
                break
            logger.warning("AI 给出的第 %d 条提问不合格（%s）：%s", index, flaw, text[:40])
            redo = QUESTION_REDO % (text[:60], flaw)
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
