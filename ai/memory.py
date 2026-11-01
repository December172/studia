"""MOOC answer memory: the standard answers the platform reveals once a paper is submitted.

`GET /course/exam/record/getInfo` for a submitted attempt lists every question with
`answer` (the standard answer as option SortOrder values, judgement 1/0), `stuAnswer`,
`isRight` and options flagged `IsAnswer`; verified on real papers 2026-11-01.
Only single choice, multiple choice and judgement are learned: their format is verified.

Scoped to one course offering (`scope`): the papers of a course share answers (homework and
exams often repeat questions); other courses never reuse them, even for a look-alike question.
"""
import hashlib
import json
import re

from ai.questions import prepare_question
from ZHSMain.memory import AnswerMemory, DatabaseAnswerMemory

JUDGEMENT = {"1": "正确", "0": "错误"}


def scope(course):
    return str(course.get("id") or course.get("courseInfoId"))


def mooc_key(q):
    """Identity of a prepared question within its course (`q["scope"]`, set by every caller);
    option order does not matter."""
    payload = {"scope": q.get("scope") or "", "type": q["type_id"], "title": q["title"], "context": q.get("context", ""),
               "options": sorted(x["text"] for x in q.get("options", [])), "blanks": q.get("blank_count", 0)}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class MoocAnswerMemory(AnswerMemory):
    key = staticmethod(mooc_key)


class MoocDatabaseAnswerMemory(DatabaseAnswerMemory):
    TABLE = "mooc_answers"
    key = staticmethod(mooc_key)


def standard_texts(q, raw):
    """The platform's standard answer of one getInfo question as option texts, or None when unsure."""
    answer = str(raw.get("answer") or "").strip()
    kind = q["type_id"]
    if kind in (1, 2):
        tokens = [t for t in re.split(r"[,，\s]+", answer) if t]
        by_value = {x["value"]: x["text"] for x in q["options"]}
        flagged = {str(o.get("SortOrder")) for o in q["raw_options"] if str(o.get("IsAnswer")).lower() == "true"}
        if (not tokens or any(t not in by_value for t in tokens) or len(set(tokens)) != len(tokens)
                or (flagged and flagged != set(tokens)) or (kind == 1 and len(tokens) != 1)):
            return None
        return [by_value[t] for t in tokens]
    if kind == 3 and answer in JUDGEMENT:
        return [JUDGEMENT[answer]]
    return None


def remembered_value(q, texts):
    """Memory texts back to the value answer_record() takes for this paper, or None if they no longer fit."""
    kind = q["type_id"]
    if kind in (1, 2):
        labels = {x["text"]: x["label"] for x in q["options"]}
        if not texts or any(t not in labels for t in texts):
            return None
        if kind == 1:
            return labels[texts[0]] if len(texts) == 1 else None
        return [labels[t] for t in texts]
    if kind == 3 and texts in (["正确"], ["错误"]):
        return texts == ["正确"]
    return None


def learn(api, memory, course, exam_id, task_id):
    """Remember the standard answers of one submitted attempt; returns how many questions were learned."""
    payload = api.request("GET", "/course/exam/record/getInfo", params={
        "courseInfoId": course.get("id") or course.get("courseInfoId"), "taskId": task_id,
        "examId": exam_id, "studentId": api.user_id, "type": "1"})
    data = payload.get("data") if isinstance(payload, dict) else None
    learned = 0
    for raw in (data or {}).get("questions") or []:
        if not isinstance(raw, dict) or raw.get("questionSubList"):
            continue
        try:
            q = dict(prepare_question({**raw, "id": raw.get("paperId")}, 0), scope=scope(course))
        except (ValueError, TypeError, KeyError):
            continue
        texts = standard_texts(q, raw)
        if texts:
            memory.remember_correct(q, texts)
            learned += 1
    memory.save()
    return learned
