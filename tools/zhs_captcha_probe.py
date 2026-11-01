#!/usr/bin/env python3
"""智慧树验证探测服务：逐接口真实请求，记录验证类型与触发点。

用法：
    python tools/zhs_captcha_probe.py              # 交互式：打印二维码，手机扫码
    python tools/zhs_captcha_probe.py --cookies f  # 用已导出的 cookie JSON

输出：每一步的真实返回、验证类型（captchaType/verifyType）、耗时。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ZHSMain.api import (  # noqa: E402
    CaptchaRequired, ZHSApi, ZHSError, _captcha_type, _shape,
)


class Probe:
    def __init__(self, client):
        self.client = client
        self.rows = []

    def step(self, name, fn, *args, **kwargs):
        started = time.time()
        row = {"接口": name, "开始": time.strftime("%H:%M:%S")}
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, dict):
                shape = _shape(result)
            elif isinstance(result, (list, tuple)):
                shape = "%s[%d]" % (type(result).__name__, len(result))
            else:
                shape = type(result).__name__
            row.update({"结果": "ok", "字段": shape,
                        "耗时": round(time.time() - started, 2)})
            print("  [ok] %-28s %.2fs" % (name, row["耗时"]), flush=True)
        except CaptchaRequired as exc:
            row.update({"结果": "验证", "信息": str(exc), "类型": "见上方日志",
                        "耗时": round(time.time() - started, 2)})
            print("  [验证] %-26s %s" % (name, exc), flush=True)
        except ZHSError as exc:
            row.update({"结果": "错误", "信息": str(exc)[:200],
                        "耗时": round(time.time() - started, 2)})
            print("  [错误] %-26s %s" % (name, str(exc)[:120]), flush=True)
        except Exception as exc:
            row.update({"结果": "异常", "信息": traceback.format_exc(limit=2)[-300:],
                        "耗时": round(time.time() - started, 2)})
            print("  [异常] %-26s %s" % (name, str(exc)[:120]), flush=True)
        self.rows.append(row)
        return row

    def run(self, secret=None, recruit_id=None, course_id=None, gap=1.0):
        print("\n== 课程列表 ==", flush=True)
        self.step("my_course_list", self.client.my_course_list)

        if not secret:
            rows = self.client.my_course_list().get("rows") or []
            for row in rows:
                z = row.get("zhs") or {}
                if z.get("kind") == "shared":
                    secret, recruit_id, course_id = (z.get("secret"), z.get("recruitId"),
                                                     z.get("courseId"))
                    print("  选中共享课：%s" % row.get("courseName"), flush=True)
                    break
        if not secret:
            print("  没有共享课，跳过后续探测", flush=True)
            return self.rows

        print("\n== 共享课学习链路 ==", flush=True)
        time.sleep(gap)
        self.step("shared_gologin", self.client.shared_gologin, secret)
        time.sleep(gap)
        self.step("shared_course", self.client.shared_course, secret)
        time.sleep(gap)
        row = self.step("shared_videos", self.client.shared_videos, secret)
        videos = []
        if row["结果"] == "ok":
            try:
                videos = self.client.shared_videos(secret) or []
            except Exception:
                videos = []
        print("\n== 共享课试卷链路 ==", flush=True)
        time.sleep(gap)
        self.step("shared_exam_sso", self.client.shared_exam_sso)
        time.sleep(gap)
        self.step("shared_homework_list", self.client.shared_homework_list,
                  recruit_id, course_id, "1")
        time.sleep(gap)
        self.step("shared_exam_list", self.client.shared_exam_list,
                  recruit_id, course_id, "1")
        return self.rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cookies", help="已导出的 cookie JSON 文件")
    parser.add_argument("--gap", type=float, default=1.0, help="步骤间隔秒数")
    parser.add_argument("--json", help="把报告写到这个文件")
    args = parser.parse_args()

    client = ZHSApi()
    if args.cookies:
        with open(args.cookies, encoding="utf-8") as file:
            cookies = json.load(file)
        for item in cookies:
            client.session.cookies.set(item["name"], item["value"],
                                       domain=item.get("domain") or "", path=item.get("path") or "/")
        print("已载入 %d 条 cookie" % len(cookies))
    else:
        token, png = client.login_qr()
        path = os.path.expanduser("~/zhs-qr.png")
        with open(path, "wb") as file:
            file.write(png)
        print("二维码已保存：%s，请用智慧树 App 扫码" % path)
        for _ in range(180):
            time.sleep(1)
            status = client.login_qr_status(token)
            if status.get("status") == 1:
                client.finish_qr_login(status.get("oncePassword", ""))
                print("登录成功")
                break
            if status.get("status") == 2:
                print("二维码已失效")
                return 1
        else:
            print("扫码超时")
            return 1

    probe = Probe(client)
    rows = probe.run(gap=args.gap)
    print("\n== 汇总 ==")
    for row in rows:
        print("  %-26s %s" % (row["接口"], row["结果"]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as file:
            json.dump(rows, file, ensure_ascii=False, indent=1)
        print("\n报告：%s" % args.json)
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
