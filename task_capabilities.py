"""Task capabilities derived from the platform's catalog type, without API calls."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


PLATFORM_TIMEZONE = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class TaskAvailability:
    available: bool
    label: str = ""
    reason: str = ""


def _platform_time(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid catalog time")
    result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=PLATFORM_TIMEZONE)
    return result.astimezone(PLATFORM_TIMEZONE)


def task_time_hint(item):
    """Present known dates without treating missing fields as a deadline."""
    try:
        start, end = (_platform_time(item.get(key)) for key in ("startTime", "endTime"))
    except ValueError:
        return "起止时间无法识别，请刷新或在平台核对。"
    if start is None and end is None:
        return ""
    return "北京时间：%s 至 %s" % (
        start.strftime("%Y-%m-%d %H:%M:%S") if start else "未设开始时间",
        end.strftime("%Y-%m-%d %H:%M:%S") if end else "未设截止时间")


def task_availability(item, category, *, now=None):
    """Use the catalog's opening state; never fetch a paper to classify it.

    Server -1/-2 remain authoritative until the catalog is refreshed. Local
    comparisons additionally catch tasks expiring while waiting in a queue.
    Naive platform timestamps are Beijing time, regardless of host timezone.
    """
    if is_attachment_task(item, category):
        return TaskAvailability(False, "暂不支持", "附件作业暂不支持，请在平台完成。")
    unavailable = item.get("_unavailable")
    if unavailable:
        return TaskAvailability(False, unavailable[0], unavailable[1])
    server_label = {"-1": "未开始", "-2": "已截止"}.get(str(item.get("status")))
    if server_label:
        return TaskAvailability(False, server_label,
                                server_label + "；" + (task_time_hint(item) or "请刷新或在平台核对开放时间。"))
    try:
        start, end = (_platform_time(item.get(key)) for key in ("startTime", "endTime"))
        if start and end and start > end:
            raise ValueError("Reversed catalog window")
    except ValueError:
        return TaskAvailability(False, "时间未知", "起止时间无法识别，请刷新或在平台核对。")
    now = now or datetime.now(PLATFORM_TIMEZONE)
    if now.tzinfo is None:
        now = now.replace(tzinfo=PLATFORM_TIMEZONE)
    if start and now < start:
        return TaskAvailability(False, "未开始", "未开始；" + task_time_hint(item))
    if end and now > end:
        return TaskAvailability(False, "已截止", "已截止；" + task_time_hint(item))
    return TaskAvailability(True)


def is_attachment_task(item, category):
    """The shared platform task card labels typeId=3 as attachment homework.

    This enum is independent of categoryId and questionSettingMethod; see
    docs/research/2026-10-10-attachment-homework.md for the public source.
    Unknown types retain existing behavior instead of guessing from names.
    """
    return item.get("typeId") in (3, "3")


def task_type_label(item, category):
    """Preserve ordinary category labels while identifying attachment tasks."""
    if is_attachment_task(item, category):
        return "附件作业"
    return {1: "作业", 2: "考试", 3: "测验"}.get(category, str(category))
