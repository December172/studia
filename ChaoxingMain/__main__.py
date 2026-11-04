"""命令行入口：超星自动学习 / 章节作业 AI 答题。

    python -m ChaoxingMain --list
    python -m ChaoxingMain --course 246831735 --study
    python -m ChaoxingMain --course 246831735 --answer --submit
    python -m ChaoxingMain --course 246831735            # 学习和答题都做

账号也可以放在 CHAOXING_USERNAME / CHAOXING_PASSWORD 里。AI 模型使用 studia 设置里
当前启用的那个，和网页版、终端版共用一份配置。
"""
import argparse
import os

from ai.client import AIClient, AIError
from base.auth import AuthenticationExpired, PlatformUnavailable
from ChaoxingMain.api import ChaoxingApi, ChaoxingError, LoginError
from ChaoxingMain.main import ChaoxingHandler
from ChaoxingMain.study import ChaoxingStudyHandler
from settings import SettingsStore

COURSE_STATE = {"success": "完成", "skipped": "跳过", "error": "有失败", "cancelled": "已取消"}
UNIT_STATE = {"done": "完成", "skipped": "跳过", "failed": "失败"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="python -m ChaoxingMain",
                                     description="超星学习通自动学习与章节作业 AI 答题")
    parser.add_argument("--username", default=os.environ.get("CHAOXING_USERNAME", ""), help="超星账号（手机号）")
    parser.add_argument("--password", default=os.environ.get("CHAOXING_PASSWORD", ""), help="超星密码")
    parser.add_argument("--course", action="append", default=[], metavar="ID", help="只处理这些课程 ID，可重复")
    parser.add_argument("--skip", action="append", default=[], metavar="词", help="课程名包含该词就跳过，可重复")
    parser.add_argument("--study", action="store_true", help="自动学习（视频、文档、阅读）；默认和答题一起做")
    parser.add_argument("--answer", action="store_true", help="章节作业 AI 答题；默认和学习一起做")
    parser.add_argument("--submit", action="store_true", help="作业答题：覆盖率达到阈值时自动交卷")
    parser.add_argument("--cover-rate", type=float, default=0.8, help="交卷所需最低覆盖率，默认 0.8")
    parser.add_argument("--delay", type=float, default=0.0, help="答题时两道题之间等待的秒数，默认 0")
    parser.add_argument("--speed", type=float, default=1.0, help="学习视频的播放倍速 1.0–2.0，默认 1.0")
    parser.add_argument("--jobs", type=int, default=4, metavar="N", help="同时处理的章节数 1–8，默认 4")
    parser.add_argument("--notopen", choices=("retry", "continue"), default="retry",
                        help="遇到未开放章节：retry 重试（默认），continue 跳过")
    parser.add_argument("--list", action="store_true", help="只列出课程、章节和任务点，不学习也不答题")
    return parser.parse_args(argv)


def report(event):
    if event.kind == "begin":
        print("%s：%s %s" % (event.name, event.total, event.unit))
    elif event.kind == "result":
        print("%s：%s" % (event.name, COURSE_STATE.get(event.status, event.status)))
    elif event.kind == "unit" and event.status in UNIT_STATE:
        print("  %s：%s" % (event.name, UNIT_STATE[event.status]))
    elif event.kind == "resources" and event.total:
        print("  %s：%s/%s" % (event.name, event.completed, event.total))


def list_courses(client, args):
    for course in filter_courses(client.course_list(), args):
        print("%s（courseId=%s）" % (course.get("title") or course.get("courseName"), course["courseId"]))
        for point in client.course_points(course)["points"]:
            jobs, _ = client.job_list(course, point)
            print("  %s" % (point.get("title") or "未命名章节"))
            for job in jobs:
                print("    [%s] %s" % (job["type"], job.get("name") or job["jobid"]))


def filter_courses(courses, args):
    selected = []
    ids = {str(value) for value in args.course}
    for course in courses:
        if ids and str(course.get("courseId")) not in ids:
            continue
        title = course.get("title") or course.get("courseName") or ""
        if any(word in title for word in args.skip if word):
            continue
        selected.append(course)
    return selected


def main(argv=None):
    args = parse_args(argv)
    if not args.username or not args.password:
        print("需要超星账号：--username/--password 或 CHAOXING_USERNAME/CHAOXING_PASSWORD")
        return 2
    if args.jobs < 1 or args.jobs > 8:
        print("--jobs 只支持 1–8")
        return 2
    args.speed = min(2.0, max(1.0, args.speed))
    if not args.study and not args.answer:
        args.study = args.answer = True

    client = ChaoxingApi(args.username, args.password)
    ai = None
    try:
        if args.list:
            list_courses(client, args)
            return 0
        courses = filter_courses(client.course_list(), args)
        if not courses:
            print("没有可处理的课程")
            return 1
        if args.study:
            ChaoxingStudyHandler(client=client, course_rows=courses, max_workers=args.jobs,
                                 speed=args.speed, notopen_action=args.notopen, on_progress=report)
        if args.answer:
            config = SettingsStore().ai_config()
            if config is None:
                print("还没有启用的 AI 模型：先在 studia 设置里添加并选中一个模型")
                return 2
            ai = AIClient(config)
            handler = ChaoxingHandler(client=client, courses=courses, ai_client=ai,
                                      auto_submit=args.submit, cover_rate=args.cover_rate,
                                      delay=args.delay, on_progress=report)
            counts = handler.counts
            print("答题：已交卷 %d · 已保存 %d · 跳过 %d · 失败 %d" % (
                counts["submitted"], counts["saved"], counts["skipped"],
                counts["error"] + counts["submission_unknown"]))
            return 0 if not (counts["error"] or counts["submission_unknown"]) else 1
        return 0
    except LoginError as error:
        print("登录失败：%s" % error)
        return 1
    except (AuthenticationExpired, PlatformUnavailable, ChaoxingError, AIError) as error:
        print("运行失败：%s" % error)
        return 1
    finally:
        client.close()
        if ai is not None:
            ai.session.close()


if __name__ == "__main__":
    raise SystemExit(main())
