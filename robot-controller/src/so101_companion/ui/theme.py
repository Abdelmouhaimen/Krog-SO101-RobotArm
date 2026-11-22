"""SO-101 Studio visual theme: one dark palette and one application stylesheet (presentation only)."""

from __future__ import annotations

from typing import Final

BG: Final = "#0e141b"  # window
PANEL: Final = "#131c25"  # right panel / status bar
CARD: Final = "#18232e"  # cards
CARD_HI: Final = "#1e2b38"  # hovered / raised controls
BORDER: Final = "#263544"
TEXT: Final = "#e6edf3"
MUTED: Final = "#8b98a5"
GREEN: Final = "#22c55e"
GREEN_DARK: Final = "#15803d"
RED: Final = "#dc2626"
RED_DARK: Final = "#991b1b"
BLUE: Final = "#3b82f6"
BLUE_DARK: Final = "#1d4ed8"
AMBER: Final = "#f59e0b"
PURPLE: Final = "#8b5cf6"

STYLESHEET: Final = f"""
QMainWindow, QWidget#studioRoot {{ background: {BG}; }}
QWidget {{ color: {TEXT}; font-size: 13px; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: {PANEL}; border: none; }}
QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 22px; font-weight: 700; }}
QLabel[role="subtitle"] {{ color: {MUTED}; font-size: 12px; }}
QLabel[role="section"] {{ font-weight: 600; font-size: 13px; }}
QLabel[role="muted"] {{ color: {MUTED}; font-size: 12px; }}
QLabel[role="reason"] {{ color: {AMBER}; font-size: 12px; }}
QLabel[role="big"] {{ font-size: 20px; font-weight: 700; }}
QFrame[role="card"] {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
QFrame[role="statusbar"] {{ background: {PANEL}; border-top: 1px solid {BORDER}; }}
QPushButton, QToolButton {{
    background: {CARD_HI}; border: 1px solid {BORDER}; border-radius: 10px; padding: 7px 12px;
}}
QPushButton:hover, QToolButton:hover {{ border-color: #3b4d60; background: #233243; }}
QPushButton:disabled, QToolButton:disabled {{ color: #5d6b78; background: #161f28; border-color: #1f2a35; }}
QPushButton:checked {{ background: #1f3b2c; border-color: {GREEN}; color: {GREEN}; }}
QPushButton[role="primary"] {{ font-size: 15px; font-weight: 600; min-height: 46px; }}
QPushButton[role="play"] {{ background: {GREEN_DARK}; border-color: {GREEN}; }}
QPushButton[role="play"]:disabled {{ background: #18301f; border-color: #1f3a27; color: #6b8a74; }}
QPushButton[role="rest"] {{ background: {BLUE_DARK}; border-color: {BLUE}; }}
QPushButton[role="rest"]:disabled {{ background: #172238; border-color: #1c2a44; color: #64748b; }}
QPushButton[role="record-active"] {{
    background: {RED_DARK}; border-color: {RED}; font-size: 15px; font-weight: 600; min-height: 46px;
}}
QPushButton[role="hold"] {{
    background: #b91c1c; border: 1px solid #ef4444; font-size: 15px; font-weight: 700; min-height: 42px;
}}
QPushButton[role="hold"]:hover {{ background: #dc2626; }}
QPushButton[role="hold"]:disabled {{ background: #3a1a1a; border-color: #4a2020; color: #a07070; }}
QPushButton[role="link"] {{ background: transparent; border: none; color: {BLUE}; padding: 2px; }}
QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
    background: {CARD_HI}; border: 1px solid {BORDER}; border-radius: 8px; padding: 5px 8px;
    selection-background-color: {BLUE_DARK};
}}
QComboBox[role="mode"] {{ border: 1px solid {GREEN}; font-size: 15px; font-weight: 600; min-height: 34px; }}
QComboBox[role="preset"] {{ font-size: 16px; font-weight: 700; color: {GREEN}; min-height: 30px; }}
QComboBox QAbstractItemView {{
    background: {CARD}; border: 1px solid {BORDER}; selection-background-color: #233243;
}}
QProgressBar {{ background: #0f1720; border: none; border-radius: 4px; max-height: 8px; }}
QProgressBar::chunk {{ background: {GREEN}; border-radius: 4px; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 10px; margin-top: 14px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {MUTED}; }}
QTableWidget {{
    background: {CARD}; gridline-color: {BORDER}; border: 1px solid {BORDER}; border-radius: 8px;
}}
QHeaderView::section {{ background: {CARD_HI}; color: {MUTED}; border: none; padding: 4px; }}
QPlainTextEdit {{
    background: #0f1720; border: 1px solid {BORDER}; border-radius: 8px; font-family: monospace;
}}
QSlider::groove:horizontal {{ height: 6px; background: #0f1720; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {TEXT}; width: 14px; margin: -5px 0; border-radius: 7px; }}
QSlider::sub-page:horizontal {{ background: {GREEN_DARK}; border-radius: 3px; }}
QMenu {{ background: {CARD}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: #233243; }}
QSplitter::handle {{ background: {BG}; }}
QCheckBox {{ spacing: 6px; }}
"""
