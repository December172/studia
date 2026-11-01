# -*- coding: utf-8 -*-
# Original entry point author: Melon. Application orchestration now lives in app.py.
"""Entry point. No terminal rendering or business rules here."""
from MoocMain.log import Logger, is_dev_mode


def _read_pyproject_version() -> str:
    """读取 pyproject.toml 里的 version"""
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


APP_VERSION = _read_pyproject_version()


def main(view=None):
    from app import Application
    from ui import TerminalUI, Cancelled
    view = view or TerminalUI(dev=is_dev_mode())
    with view.session():
        try:
            view.header(APP_VERSION)
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
    if '--serve' in sys.argv[1:]:
        from web.hosting import main as hosted
        sys.exit(hosted([arg for arg in sys.argv[1:] if arg != '--serve']))
    elif '--web' in sys.argv[1:]:
        from web.server import serve
        serve()
    else:
        main()
