"""Terminal appearance. Business modules contain no Rich markup or styling."""
from rich import box
from rich.theme import Theme

# Claude MCP Apps palette, dark-mode values (users run dark terminals).
TEXT = "#faf9f5"        # text primary; also the inverse (focus) background
INK = "#141413"         # text on the inverse background
SECONDARY = "#c2c0b6"
TERTIARY = "#9c9a92"
BORDER = "#65645f"      # border-primary (#dedcd1 @ 40%) flattened onto #141413
SURFACE = "#262624"     # background secondary
SURFACE_RAISED = "#30302e"  # background primary
BRAND = "#d97757"       # Claude coral: title, progress, checked marks only
SUCCESS = "#7ab948"
WARNING = "#d1a041"
DANGER = "#ee8884"
INFO = "#80aadd"

STYLES = {
    "title": f"bold {BRAND}",
    "accent": f"bold {TEXT}",
    "muted": TERTIARY,
    "table_header": f"bold {SECONDARY}",
    "shortcut": f"bold {TEXT}",
    "info": "default",
    "success": SUCCESS,
    "warning": WARNING,
    "error": DANGER,
    "border": BORDER,
}
THEME = Theme(STYLES)
ICONS = {"info": "·", "success": "✓", "warning": "!", "error": "×", "prompt": "›"}
BOX = box.ROUNDED
TABLE_BORDER = box.SIMPLE
SPINNER = "line"  # ASCII frames have stable width in macOS Terminal fonts.
PROGRESS_ASCII = True
LOG_LINES = 8
PAGE_SIZE = 12
REFRESH_PER_SECOND = 8
BAR_STYLE = BORDER
BAR_COMPLETE = BRAND
BAR_FINISHED = SUCCESS
