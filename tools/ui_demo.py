"""Offline terminal preview: python -m tools.ui_demo [snapshot.html | --native]."""
import sys

from ui import demo


if __name__ == "__main__":
    if "--native" in sys.argv[1:]:
        from tools.native_demo import main
        raise SystemExit(main())
    demo(sys.argv[1] if len(sys.argv) > 1 else None)
