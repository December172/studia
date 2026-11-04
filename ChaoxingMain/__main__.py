"""命令行入口：直接跑超星章节作业自动作答。

    python -m ChaoxingMain --list
    python -m ChaoxingMain --course 246831735 --submit

账号也可以放在 CHAOXING_USERNAME / CHAOXING_PASSWORD 里。AI 模型使用 studia 设置里
当前启用的那个，和网页版、终端版共用一份配置。
"""
import argparse
import os

from ai.client import AIClient, AIError
from base.auth import AuthenticationExpired, PlatformUnavailable
from ChaoxingMain.api import ChaoxingApi, ChaoxingError, LoginError
from ChaoxingMain.main import ChaoxingHandler
from settings import SettingsStore

COURSE_STATE = {"success": "完成", "skipped": "跳过", "error": "有失败", "cancelled": "已取消"}
UNIT_STATE = {"done": "完成", "skipped": "跳过", "failed": "失败"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="python -m ChaoxingMain", description="超星学习通章节作业自动作答")
    parser.add_argument("--username", default=os.environ.get("CHAOXING_USERNAME", ""), help="超星账号（手机号）")
    parser.add_argument("--password", default=os.environ.get("CHAOXING_PASSWORD", ""), help="超星密码")
    parser.add_argument("--course", action="append", default=[], metavar="ID", help="只处理这些课程 ID，可重复")
    parser.add_argument("--skip", action="append", default=[], metavar="词", help="课程名包含该词就跳过，可重复")
    parser.add_argument("--submit", action="store_true", help="覆盖率达到阈值时自动交卷；默认只保存")
    parser.add_argument("--cover-rate", type=float, default=0.8, help="交卷所需最低覆盖率，默认 0.8")
    parser.add_argument("--delay", type=float, default=0.0, help="两道题之间等待的秒数，默认 0")
    parser.add_argument("--list", action="store_true", help="只列出课程、章节和作业，不答题")
    return parser.parse_args(argv)


def report(event):
    if event.kind == "begin":
        print("%s：%s %s" % (event.name, event.total, event.unit))
    elif event.kind == "result":
        print("%s：%s" % (event.name, COURSE_STATE.get(event.status, event.status)))
    elif event.kind == "unit" and event.status in UNIT_STATE:
        print("  %s：%s" % (event.name, UNIT_STATE[event.status]))


def list_courses(args):
    api = ChaoxingApi(args.username, args.password)
    try:
        for course in api.course_list():
            print("%s（courseId=%s）" % (course["title"], course["courseId"]))
            for point in api.course_points(course)["points"]:
                jobs, _ = api.job_list(course, point)
                for job in jobs:
                    print("  %s / %s" % (point["title"], job.get("name") or job["jobid"]))
    finally:
        api.close()
    return 0


def main(argv=None):
    args = parse_args(argv)
    if not args.username or not args.password:
        print("需要超星账号：--username/--password 或 CHAOXING_USERNAME/CHAOXING_PASSWORD")
        return 2
    if args.list:
        try:
            return list_courses(args)
        except LoginError as error:
            print("登录失败：%s" % error)
            return 1
        except (AuthenticationExpired, PlatformUnavailable, ChaoxingError) as error:
            print("读取失败：%s" % error)
            return 1
    config = SettingsStore().ai_config()
    if config is None:
        print("还没有启用的 AI 模型：先在 studia 设置里添加并选中一个模型")
        return 2
    ai = AIClient(config)
    try:
        handler = ChaoxingHandler(
            args.username, args.password, ai_client=ai, course_ids=args.course, skip=args.skip,
            auto_submit=args.submit, cover_rate=args.cover_rate, delay=args.delay, on_progress=report)
    except LoginError as error:
        print("登录失败：%s" % error)
        return 1
    except (AuthenticationExpired, PlatformUnavailable, ChaoxingError, AIError) as error:
        print("运行失败：%s" % error)
        return 1
    finally:
        ai.session.close()
    counts = handler.counts
    print("已交卷 %d · 已保存 %d · 跳过 %d · 失败 %d" % (
        counts["submitted"], counts["saved"], counts["skipped"], counts["error"] + counts["submission_unknown"]))
    return 0 if not (counts["error"] or counts["submission_unknown"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
