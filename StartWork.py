# -*- coding: utf-8 -*-
# Original entry point author: Melon. Application orchestration now lives in app.py.
"""Source / packaged entry point. No terminal rendering or business rules here."""
from MoocMain.log import Logger, is_dev_mode

_RELEASE_TAG_PLACEHOLDER = "${TAG_NAME}"


def _read_pyproject_version() -> str:
    """读取 pyproject.toml 里的 version（源码运行时用）"""
    try:
        from pathlib import Path
        text = Path(__file__).resolve().parent.joinpath("pyproject.toml").read_text(encoding="utf-8")
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("version") and "=" in s:
                return s.split("=", 1)[1].strip().strip('"').strip("'") or "0.0.0"
    except Exception:
        pass
    return "0.0.0"


def _resolve_app_version() -> str:
    """发布构建显示 git tag；源码运行显示 pyproject 版本"""
    tag = _RELEASE_TAG_PLACEHOLDER  # CI 注入后这里就是 v1.2.3
    if tag and not str(tag).startswith("${"):
        return str(tag)
    return _read_pyproject_version()


# 是否为 CI 打包的发布版（源码运行为 False，不检查更新）
IS_RELEASE_BUILD = bool(_RELEASE_TAG_PLACEHOLDER) and not str(_RELEASE_TAG_PLACEHOLDER).startswith("${")
APP_VERSION = _resolve_app_version()

def main(view=None):
    from app import Application
    from ui import TerminalUI, Cancelled
    from update import check_for_updates
    view = view or TerminalUI(dev=is_dev_mode())
    with view.session():
        try:
            view.header(APP_VERSION)
            check_for_updates(APP_VERSION, release_build=IS_RELEASE_BUILD)
            Application(view).run()
        except (Cancelled, KeyboardInterrupt, EOFError):
            view.cancelled = True
        except Exception as exc:
            Logger(__name__).get_log().exception("运行失败")
            if view.workspace:
                view.begin_operation()
            view.pending_error = str(exc)
        finally:
            view.finish()
    return view


if __name__ == '__main__':
    import sys
    if '--web' in sys.argv[1:]:
        from web.server import serve
        serve()
    else:
        main()
