"""Dark-mode palette and stylesheets (gui-coding-standards.md §7, §9)."""

DARK_BG = "#2c3e50"
DARK_BG_ALT = "#34495e"
TEXT_PRIMARY = "#ecf0f1"
TEXT_SECONDARY = "#bdc3c7"
ACCENT_BLUE = "#3498db"
ACCENT_GREEN = "#27ae60"
ACCENT_RED = "#e74c3c"
ACCENT_ORANGE = "#f39c12"
BORDER_COLOR = "#1a252f"

SEVERITY_COLORS = {
    "PASS": ACCENT_GREEN,
    "WARN": ACCENT_ORANGE,
    "FAIL": ACCENT_RED,
    "INFO": ACCENT_BLUE,
}

MAIN_WINDOW = f"""
    QMainWindow, QWidget {{
        background-color: {DARK_BG};
        color: {TEXT_PRIMARY};
    }}
    QToolBar {{
        background-color: {DARK_BG_ALT};
        border-bottom: 1px solid {BORDER_COLOR};
        spacing: 6px;
        padding: 4px;
    }}
    QStatusBar {{
        background-color: {DARK_BG_ALT};
        color: {TEXT_SECONDARY};
    }}
    QSplitter::handle {{
        background-color: {BORDER_COLOR};
    }}
    QScrollArea {{
        border: none;
    }}
    QLabel {{
        color: {TEXT_PRIMARY};
    }}
    QSlider::groove:horizontal {{
        height: 6px;
        background: {BORDER_COLOR};
        border-radius: 3px;
    }}
    QSlider::handle:horizontal {{
        width: 14px;
        margin: -5px 0;
        border-radius: 7px;
        background: {ACCENT_BLUE};
    }}
    QToolTip {{
        background-color: {DARK_BG_ALT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_COLOR};
    }}
    QToolButton {{
        padding: 4px 8px;
        border-radius: 3px;
        border: 1px solid transparent;
    }}
    QToolButton:hover {{ background-color: {BORDER_COLOR}; }}
    QToolButton:checked {{
        background-color: {ACCENT_BLUE};
        color: white;
        border: 1px solid {TEXT_PRIMARY};
        font-weight: bold;
    }}
"""

# Status-bar badge saying what the displayed image actually IS. Undistort is easy
# to lose track of and every mm reading depends on it, so it gets a colour, not a
# tick box you have to squint at.
IMAGE_STATE_OK = f"""
    QLabel {{
        background-color: {ACCENT_GREEN}; color: white; font-weight: bold;
        border-radius: 3px; padding: 2px 10px;
    }}
"""

IMAGE_STATE_RAW = f"""
    QLabel {{
        background-color: {ACCENT_RED}; color: white; font-weight: bold;
        border-radius: 3px; padding: 2px 10px;
    }}
"""

BUTTON_PRIMARY = f"""
    QPushButton {{
        background-color: {ACCENT_BLUE};
        color: white;
        border-radius: 4px;
        padding: 6px 12px;
        border: none;
    }}
    QPushButton:hover {{ background-color: #2980b9; }}
    QPushButton:disabled {{ background-color: #7f8c8d; }}
"""

BUTTON_SMALL = f"""
    QPushButton {{
        background-color: {DARK_BG_ALT};
        color: {TEXT_PRIMARY};
        border: 1px solid {ACCENT_BLUE};
        border-radius: 3px;
        padding: 2px 8px;
    }}
    QPushButton:hover {{ background-color: {ACCENT_BLUE}; }}
"""

RUN_LIST = f"""
    QListWidget {{
        background-color: {DARK_BG_ALT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER_COLOR};
        border-radius: 4px;
    }}
    QListWidget::item {{ padding: 6px; }}
    QListWidget::item:selected {{ background-color: {ACCENT_BLUE}; }}
"""

QC_PANEL = f"""
    QFrame#qc_row {{
        background-color: {DARK_BG_ALT};
        border-radius: 3px;
    }}
"""

GROUP_TITLE = f"""
    QLabel {{
        color: {TEXT_SECONDARY};
        font-weight: bold;
        padding: 4px 0;
    }}
"""
