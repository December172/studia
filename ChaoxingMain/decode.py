"""超星学习通页面解析：课程、章节、章节任务点与作业题目。

对应 chaoxing 项目的 api/decode.py。原实现用 BeautifulSoup，这里改成项目已有的
lxml（不新增解析依赖），选择器含义保持一致：
- 课程卡片是 ``div.course``，未开放课程带 ``.not-open-tip``；
- 章节是 ``div.chapter_unit`` 下的 ``li``，点位 ``div#cur<id>``；
- 任务点来自页面脚本里的 ``mArg={...};`` JSON，作业/测验是 ``type=workid`` 的卡片；
- 题目页面是 ``form`` 里的 ``div.singleQuesId``，题号在 ``div[data]``，
  题型在 ``div.TiMu[data]``，题干 ``div.Zy_TItle``，选项 ``ul li``。
"""
import json
import re

from lxml import etree
from lxml import html as lxml_html

from ChaoxingMain.font import FontDecoder, encrypted as font_encrypted
from MoocMain.log import Logger

logger = Logger(__name__).get_log()

# 题型代码 → 内部题型名；未知题型不猜题意，交给调用方标记为暂不支持。
QUESTION_TYPES = {
    "0": "single",
    "1": "multiple",
    "2": "completion",
    "3": "judgement",
    "4": "shortanswer",
}

_CHOICE_NOISE = re.compile(r"[\r\t\n]")
_TITLE_NOISE = re.compile(r"[\r\t\n]")
# 页面脚本里的 mArg 赋值可能带换行；不能用「先删掉所有空格」的做法，那会把任务名里的
# 空格一起删掉。这里容忍等号周围的空白和 JSON 内部的换行。
_MARG = re.compile(r"mArg\s*=\s*(\{.*?\})\s*;", re.S)


def _document(html_text):
    if not html_text or not html_text.strip():
        return None
    try:
        # document_fromstring 总是补出 <html>，片段和整页都能用 .// 查找。
        return lxml_html.document_fromstring(html_text)
    except (etree.ParserError, ValueError, TypeError):
        logger.warning("页面不是可解析的 HTML")
        return None


def _class(name):
    return "contains(concat(' ', normalize-space(@class), ' '), ' %s ')" % name


def _all(node, tag, name):
    return node.xpath(".//%s[%s]" % (tag, _class(name)))


def _one(node, tag, name):
    found = _all(node, tag, name)
    return found[0] if found else None


def _text(node):
    return "".join(node.itertext()) if node is not None else ""


def decode_course_list(html_text):
    """课程列表：id、clazzId、courseId、cpi 和标题；未开放的课程不返回。"""
    root = _document(html_text)
    if root is None:
        return []
    courses = []
    for card in _all(root, "div", "course"):
        if _all(card, "a", "not-open-tip") or _all(card, "div", "not-open-tip"):
            continue
        links = card.xpath(".//a")
        clazz, course_id = _one(card, "input", "clazzId"), _one(card, "input", "courseId")
        if not links or clazz is None or course_id is None:
            logger.warning("跳过结构不完整的课程卡片")
            continue
        found = re.findall(r"cpi=(.*?)&", links[0].get("href") or "")
        if not card.get("id") or not found:
            logger.warning("课程卡片缺少 id 或 cpi")
            continue
        title, desc, teacher = _one(card, "span", "course-name"), _one(card, "p", "margint10"), _one(card, "p", "color3")
        courses.append({
            "id": card.get("id"),
            "info": card.get("info", ""),
            "roleid": card.get("roleid", ""),
            "clazzId": clazz.get("value", ""),
            "courseId": course_id.get("value", ""),
            "cpi": found[0],
            "title": (title.get("title") if title is not None else "") or "",
            "desc": (desc.get("title") if desc is not None else "") or "",
            "teacher": (teacher.get("title") if teacher is not None else "") or "",
            "progress": 0,
        })
    return courses


def decode_course_folder(html_text):
    """第二层课程列表：用户自建的课程文件夹（fileid）。"""
    root = _document(html_text)
    if root is None:
        return []
    folders = []
    for item in root.xpath(".//ul[%s]/li[@fileid]" % _class("file-list")):
        rename = _one(item, "input", "rename-input")
        folders.append({"id": item.get("fileid"), "rename": (rename.get("value") if rename is not None else "") or ""})
    return folders


def decode_course_point(html_text):
    """章节列表；hasLocked 表示这门课有需要完成任务点才能解锁的章节。"""
    root = _document(html_text)
    if root is None:
        return {"hasLocked": False, "points": []}
    points = []
    for unit in _all(root, "div", "chapter_unit"):
        points.extend(_chapter_points(unit))
    return {"hasLocked": any(point["need_unlock"] for point in points), "points": points}


def _chapter_points(unit):
    points = []
    for item in unit.xpath(".//li"):
        owners = item.xpath("./div")
        if not owners:
            continue
        point = owners[0]
        found = re.match(r"^cur(\d{1,20})$", point.get("id") or "")
        if not found:
            continue
        title = _one(point, "a", "clicktitle")
        count = _one(point, "input", "knowledgeJobCount")
        tip_node = _one(point, "span", "bntHoverTips")
        tip = _text(tip_node)
        points.append({
            "id": found.group(1),
            "title": _text(title).replace("\n", "").strip(),
            "jobCount": _job_count(count),
            "has_finished": "已完成" in tip,
            # 与超星一致：带任务点数量的章节用数量判断，没有数量才看「解锁」提示。
            "need_unlock": count is None and "解锁" in tip,
        })
    return points


def _job_count(node):
    if node is None:
        return 1
    value = node.get("value") or ""
    return int(value) if value.isdigit() else 1


def decode_jobs(html_text, include_finished=False):
    """章节任务点：视频、文档、阅读、作业，以及这一章的公共参数（ktoken/cpi/knowledgeid 等）。

    对应源码 ``decode_course_card``：默认跳过已完成的非作业卡片（学习流程用），
    ``include_finished=True`` 时保留它们（网页课程页展示 1/1 用）；作业卡片的
    isPassed 不可靠所以始终保留。``otherInfo`` 只取第一段；没有 mid 但有 objectId
    的 mp4 仍可处理；pdf/ppt 等文档类型统一归到 document。``notOpen`` 表示这一章
    还没开放。
    """
    if "章节未开放" in (html_text or ""):
        return [], {"notOpen": True}
    found = _MARG.search(html_text or "")
    if not found:
        return [], {}
    try:
        data = json.loads(found.group(1))
    except ValueError:
        logger.warning("任务点 JSON 无法解析")
        return [], {}
    if not isinstance(data, dict) or not data:
        return [], {}
    jobs = []
    cards = data.get("attachments")
    for card in cards if isinstance(cards, list) else []:
        job = _card_job(card, include_finished=include_finished)
        if job is not None:
            jobs.append(job)
    return jobs, _job_info(data)


# 任务点类型别名：平台/不同课程可能把文档叫 document，也可能直接给 pdf/ppt，
# 视频也可能叫 audio/mp4；都归到同一组接口上处理。
VIDEO_TYPES = {"video", "audio", "mp4", "mp3", "video/mp4"}
DOCUMENT_TYPES = {"document", "doc", "docx", "pdf", "ppt", "pptx", "xls", "xlsx",
                  "txt", "zip", "image", "img", "pic"}
READ_TYPES = {"read"}
WORK_TYPES = {"workid"}


def _flag(value):
    """平台有些字段是 "0"/"1" 或 "true"/"false" 字符串，直接用 bool 会把 "0" 当成真。"""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _card_job(card, include_finished=False):
    if not isinstance(card, dict):
        return None
    prop = card.get("property") if isinstance(card.get("property"), dict) else {}
    name = prop.get("name") or prop.get("title") or ""
    kind = str(card.get("type") or "").lower().strip()
    # 有些任务点只把类型写在 property.type / resourceType 里。
    for candidate in (kind, str(prop.get("type") or "").lower(), str(prop.get("resourceType") or "").lower()):
        if candidate in VIDEO_TYPES | DOCUMENT_TYPES | READ_TYPES | WORK_TYPES:
            kind = candidate
            break
    passed = _flag(card.get("isPassed"))
    job_id = card.get("jobid")
    if not job_id:
        logger.warning("跳过没有 jobid 的任务点：[%s] %s", kind or "?", name or "?")
        return None
    # 作业卡片的 isPassed 不可靠（源码特意不跳过）；其他任务默认跳过已完成的，
    # 但网页课程页需要把已完成的任务点画出来（1/1），所以带了 include_finished。
    if passed and kind not in WORK_TYPES and not include_finished:
        logger.info("跳过已完成的任务点：[%s] %s", kind or "?", name or job_id)
        return None
    # otherInfo 有时带一串附加参数，超星只用第一段。
    other = str(card.get("otherInfo") or "").split("&")[0]
    common = {"jobid": job_id, "otherinfo": other, "is_passed": passed,
              "mid": card.get("mid", ""), "enc": card.get("enc", ""), "aid": card.get("aid", "")}
    if kind in VIDEO_TYPES:
        object_id = _first(card.get("objectId"), card.get("objectid"), prop.get("objectid"))
        if not object_id:
            logger.warning("跳过缺少 objectId 的视频任务点：%s", name or job_id)
            return None
        if not card.get("mid"):
            # 源码把缺 mid 的卡片当转码失败跳过；实际有些 mp4 直链视频没有 mid，
            # 进度接口只用 objectId，仍可以正常完成。
            logger.info("视频任务没有 mid，按 objectId 继续处理：%s", name or job_id)
        fast = _first(card.get("fastforward"), prop.get("fastforward"))
        return dict(common, type="video", name=name, objectid=str(object_id),
                    playTime=_first(card.get("playTime"), prop.get("playTime"), 0),
                    rt=_first(prop.get("rt"), card.get("rt")),
                    # 卡片上没有 fastforward 时留空，等播放信息接口给准确值。
                    fastforward=None if fast == "" else _flag(fast),
                    vbegin=_first(prop.get("vbegin"), card.get("vbegin")),
                    vend=_first(prop.get("vend"), card.get("vend")),
                    attDuration=card.get("attDuration", ""),
                    attDurationEnc=card.get("attDurationEnc", ""),
                    videoFaceCaptureEnc=card.get("videoFaceCaptureEnc", ""))
    if kind in DOCUMENT_TYPES:
        return dict(common, type="document", name=name,
                    jtoken=card.get("jtoken", ""),
                    objectid=_first(prop.get("objectid"), card.get("objectId"), card.get("objectid")))
    if kind in READ_TYPES:
        read = _flag(prop.get("read"))
        if read and not include_finished:  # 已经读过的阅读任务
            logger.info("跳过已完成的阅读任务：%s", name or job_id)
            return None
        return dict(common, type="read", name=name, jtoken=card.get("jtoken", ""),
                    page_id=prop.get("id", ""), is_passed=read or passed)
    if kind in WORK_TYPES:
        return dict(common, type="workid", name=prop.get("title") or "",
                    is_passed=False)  # 作业的完成标志不展示、也不跳过
    logger.info("跳过暂不支持的任务点类型：[%s] %s", kind or "?", name or job_id)
    return None


def decode_work_jobs(html_text):
    """只取作业卡片（答题流程用），其余任务点交给 decode_jobs。"""
    jobs, job_info = decode_jobs(html_text)
    return [job for job in jobs if job["type"] == "workid"], job_info


def _job_info(cards_data):
    defaults = cards_data.get("defaults")
    defaults = defaults if isinstance(defaults, dict) else {}
    return {
        "ktoken": defaults.get("ktoken", ""),
        "mtEnc": defaults.get("mtEnc", ""),
        "reportTimeInterval": defaults.get("reportTimeInterval", 60),
        "defenc": defaults.get("defenc", ""),
        "cardid": defaults.get("cardid", ""),
        "cpi": defaults.get("cpi", ""),
        "qnenc": defaults.get("qnenc", ""),
        "knowledgeid": defaults.get("knowledgeid", ""),
    }


def decode_questions_info(html_text):
    """作业页面：表单隐藏字段、题目列表和 answerwqbid。

    带 ``cxSecretStyle`` 的页面会用平台下发的字体加密题干和选项，先解码再返回，
    解码失败抛 FontDecodeError，调用方应把这道作业当作不支持，而不是拿乱码去问 AI。
    """
    root = _document(html_text)
    forms = root.xpath(".//form") if root is not None else []
    if not forms:
        raise ValueError("题目页面没有表单")
    form = _extract_form(forms[0])
    decoder = FontDecoder(html_text) if font_encrypted(html_text) else None
    questions = []
    for div in _all(forms[0], "div", "singleQuesId"):
        question = _process_question(div, decoder)
        if question is not None:
            questions.append(question)
    form["questions"] = questions
    form["answerwqbid"] = ",".join(question["id"] for question in questions) + ","
    return form


def _extract_form(form_node):
    """表单里除答案字段外的隐藏参数，交卷时原样带回。"""
    data = {}
    for node in form_node.xpath(".//input"):
        name = node.get("name")
        if not name or "answer" in name:
            continue
        data[name] = node.get("value", "")
    return data


def _process_question(div_tag, decoder=None):
    question_id = div_tag.get("data") or ""
    wrapper = _one(div_tag, "div", "TiMu")
    if wrapper is None or not question_id:
        return None
    type_code = wrapper.get("data") or ""
    question = {
        "id": question_id,
        "type": QUESTION_TYPES.get(type_code, "unknown"),
        "type_code": type_code,
        "title": _title_text(_one(div_tag, "div", "Zy_TItle"), decoder),
        "options": _choice_list(div_tag, decoder),
    }
    if question["type"] == "unknown":
        logger.info("未知题型代码 -> %s", type_code)
    return question


def _title_text(element, decoder=None):
    if element is None:
        return ""
    parts = [element.text or ""]
    for child in element:
        _walk(child, parts)
    text = _TITLE_NOISE.sub("", "".join(parts))
    return decoder.decode(text) if decoder else text


def _walk(node, parts):
    """按文档顺序收集文字，图片保留成 <img src=...> 占位，便于识别「需要人工处理」。"""
    if not isinstance(node.tag, str):
        if node.tail:
            parts.append(node.tail)
        return
    if node.tag == "img":
        parts.append('<img src="%s">' % (node.get("src") or ""))
    elif node.text:
        parts.append(node.text)
    for child in node:
        _walk(child, parts)
    if node.tail:
        parts.append(node.tail)


def _choice_list(div_tag, decoder=None):
    lists = div_tag.xpath(".//ul")
    choices = []
    for item in lists[0].xpath("./li") if lists else []:
        choice = _choice_text(item, decoder)
        if choice:
            choices.append(choice)
    # 超星页面里的选项按字母顺序给出，但题面顺序偶尔会乱，排序后与提交顺序一致。
    choices.sort()
    return [{"label": _choice_label(index, text), "value": text[:1], "text": text}
            for index, text in enumerate(choices)]


def _choice_label(index, text):
    first = text[:1]
    return first if "A" <= first <= "Z" else chr(65 + index)


def _choice_text(element, decoder=None):
    # aria-label 是超星给读屏软件准备的纯文本，比正文更干净（修复 #474 的思路）。
    choice = element.get("aria-label") or "".join(element.itertext())
    if not choice:
        return ""
    choice = _CHOICE_NOISE.sub("", choice)
    if decoder:
        choice = decoder.decode(choice)
    choice = choice.strip()
    if choice.endswith("选择"):
        choice = choice[:-2].rstrip()
    return choice
