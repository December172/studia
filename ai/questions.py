"""Validate question identity and convert model answers to current ICVE fields."""
import json
import re

from AIMoocMain.export_questions import clean_html_text


TYPES = {1: "单选题", 2: "多选题", 3: "判断题", 4: "填空题", 5: "主观填空", 6: "问答题"}


def data_array(raw):
    value = raw.get("dataJson") or raw.get("dataArr") or []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("题目选项格式未知")
    return value


def contains_media(raw):
    text = json.dumps(raw, ensure_ascii=False)
    return bool(re.search(r"<(?:img|audio|video|iframe|svg|math)\b|\\(?:frac|sqrt)\b|\.(?:mp3|wav|ogg|m4a|mp4|png|jpe?g)(?:[?\"\s<]|$)", text, re.I)) or any(
        raw.get(k) for k in ("resourcePath", "audioUrl", "videoUrl", "imageUrl", "fileUrl", "attachments")
    )


def prepare_question(raw, number, parent=None, child_number=None):
    if not isinstance(raw, dict):
        raise ValueError("题目格式未知")
    kind = int(raw.get("typeId", 0))
    if kind not in TYPES:
        raise ValueError("暂不支持题型 %s" % kind)
    if contains_media(raw) or (parent and contains_media(parent)):
        raise ValueError("包含图片、音频、附件或公式，需要人工处理")
    paper_id = (parent or raw).get("id")
    if not paper_id or (parent and not raw.get("id")):
        raise ValueError("缺少平台试题 ID")
    title = clean_html_text(raw.get("title") or raw.get("content") or "")
    if not title:
        raise ValueError("题干为空")
    arr = data_array(raw)
    options = []
    if kind in (1, 2):
        if not 2 <= len(arr) <= 26:
            raise ValueError("选项数量无效")
        seen = set()
        for idx, item in enumerate(arr):
            if not isinstance(item, dict) or item.get("SortOrder") is None:
                raise ValueError("缺少选项原始 SortOrder")
            value = str(item["SortOrder"])
            if not re.fullmatch(r"\d+", value) or value in seen:
                raise ValueError("选项 SortOrder 无效或重复")
            seen.add(value)
            text = clean_html_text(item.get("Content", ""))
            if not text:
                raise ValueError("选项内容为空")
            options.append({"label": chr(65 + idx), "text": text, "value": value})
    blanks = 0
    if kind in (4, 5):
        blanks = int(raw.get("answerNum") or len(arr) or 0)
        if blanks < 1:
            raise ValueError("无法确定填空数量")
    return {
        "type_id": kind, "type": TYPES[kind], "title": title,
        "context": clean_html_text(parent.get("title", "")) if parent else "",
        "options": options, "blank_count": blanks,
        "paper_id": str(paper_id), "paper_sub_id": str(raw["id"]) if parent else None,
        "number": number, "child_number": child_number, "raw_options": arr,
    }


def question_fingerprint(q):
    """Identity of a prepared question: any content or position change invalidates saved answers."""
    return {
        "paper_id": q["paper_id"], "paper_sub_id": q.get("paper_sub_id"),
        "number": q["number"], "child_number": q.get("child_number"),
        "type_id": q["type_id"], "title": q["title"], "context": q.get("context", ""),
        "blank_count": q.get("blank_count", 0),
        "options": [[x["label"], x["text"], x["value"]] for x in q.get("options", [])],
        # sorted keys: the string form must not depend on the platform's key order.
        "raw_options": json.dumps(q.get("raw_options", []), ensure_ascii=False, sort_keys=True),
    }


def manual_answer_value(q, text):
    """Convert one human input line into the typed value the AI path produces."""
    raw = (text or "").strip()
    kind = q["type_id"]
    if not raw:
        raise ValueError("答案不能为空")
    if kind in (1, 2):
        letters = raw.replace(" ", "").upper()
        if not letters.isalpha():
            raise ValueError("请输入选项字母，例如 A 或 AB")
        labels = [x["label"] for x in q["options"]]
        if len(set(letters)) != len(letters) or any(x not in labels for x in letters):
            raise ValueError("选项不存在或重复：%s" % letters)
        if kind == 1:
            if len(letters) != 1:
                raise ValueError("单选题只能填一个选项")
            value = letters
        else:
            value = list(letters)
    elif kind == 3:
        lowered = raw.lower()
        if lowered in ("y", "yes", "true", "1", "对", "正确", "是"):
            value = True
        elif lowered in ("n", "no", "false", "0", "错", "错误", "否"):
            value = False
        else:
            raise ValueError("判断题请输入 y 或 n")
    elif kind in (4, 5):
        value = [part.strip() for part in raw.split("|")]
        if len(value) != q["blank_count"] or any(not x for x in value):
            raise ValueError("需要填写 %d 个空，用 | 分隔" % q["blank_count"])
    else:
        value = raw
    answer_record(q, value)
    return value


def model_question(q):
    return {**q, "options": [{"label": x["label"], "text": x["text"]} for x in q["options"]]}


def answer_record(q, answer):
    kind = q["type_id"]
    if answer is None:
        raise ValueError("AI 未提供答案")
    if kind in (1, 2):
        labels = [answer] if kind == 1 else answer
        mapping = {x["label"]: x["value"] for x in q["options"]}
        if not isinstance(labels, list) or not labels or any(not isinstance(x, str) or x not in mapping for x in labels):
            raise ValueError("AI 返回了未知选项")
        if len(set(labels)) != len(labels):
            raise ValueError("AI 返回了重复选项")
        encoded = ",".join(mapping[x] for x in labels)
    elif kind == 3:
        if not isinstance(answer, bool):
            raise ValueError("判断题答案必须为 true/false")
        encoded = "1" if answer else "0"
    elif kind in (4, 5):
        if not isinstance(answer, list) or len(answer) != q["blank_count"] or any(not isinstance(x, str) or not x.strip() for x in answer):
            raise ValueError("填空答案数量不符或存在空答案")
        encoded = json.dumps([x.strip() for x in answer], ensure_ascii=False)
    else:
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("问答题答案为空或格式无效")
        encoded = answer.strip()
    record = {"questionNo": q["number"], "paperId": q["paper_id"], "answer": encoded}
    if q["paper_sub_id"]:
        record.update(paperSubId=q["paper_sub_id"], questionNo=json.dumps([q["number"], q["child_number"]]))
    if kind in (1, 2):
        record["optionSort"] = json.dumps(q["raw_options"], ensure_ascii=False, separators=(",", ":"))
    return record
