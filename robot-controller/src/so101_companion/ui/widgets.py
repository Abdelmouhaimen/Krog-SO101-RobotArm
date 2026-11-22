"""Small presentation widgets for the Studio UI: painted icons, cards, a collapsible section, the
behavior timeline (scrub, trim handles, playhead, keyframe ticks) and the playback status chips.

Pure presentation: nothing here talks to the controller or the robot.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from so101_companion.ui import theme


# ------------------------------------------------------------------------------------ icons
def _pen(color: str, width: float) -> QPen:
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _draw_pc(p: QPainter, x: float, y: float, s: float, color: str) -> None:
    p.setPen(_pen(color, s * 0.09))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(x + s * 0.08, y + s * 0.14, s * 0.84, s * 0.56), s * 0.06, s * 0.06)
    p.drawLine(QPointF(x + s * 0.5, y + s * 0.70), QPointF(x + s * 0.5, y + s * 0.84))
    p.drawLine(QPointF(x + s * 0.3, y + s * 0.86), QPointF(x + s * 0.7, y + s * 0.86))


def _draw_robot(p: QPainter, x: float, y: float, s: float, color: str) -> None:
    p.setPen(_pen(color, s * 0.11))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawLine(QPointF(x + s * 0.15, y + s * 0.9), QPointF(x + s * 0.65, y + s * 0.9))  # base
    p.drawLine(QPointF(x + s * 0.4, y + s * 0.9), QPointF(x + s * 0.32, y + s * 0.5))  # upper arm
    p.drawLine(QPointF(x + s * 0.32, y + s * 0.5), QPointF(x + s * 0.68, y + s * 0.26))  # forearm
    p.drawLine(QPointF(x + s * 0.68, y + s * 0.26), QPointF(x + s * 0.86, y + s * 0.14))  # jaw
    p.drawLine(QPointF(x + s * 0.68, y + s * 0.26), QPointF(x + s * 0.9, y + s * 0.34))  # jaw
    p.setBrush(QColor(color))
    p.drawEllipse(QPointF(x + s * 0.32, y + s * 0.5), s * 0.06, s * 0.06)


def _draw_arrow(p: QPainter, x: float, y: float, s: float, color: str) -> None:
    p.setPen(_pen(color, s * 0.09))
    p.drawLine(QPointF(x + s * 0.15, y + s * 0.5), QPointF(x + s * 0.85, y + s * 0.5))
    p.drawLine(QPointF(x + s * 0.6, y + s * 0.3), QPointF(x + s * 0.85, y + s * 0.5))
    p.drawLine(QPointF(x + s * 0.6, y + s * 0.7), QPointF(x + s * 0.85, y + s * 0.5))


def mode_icon(kind: str, color: str = theme.TEXT, size: int = 22) -> QIcon:
    """``pc``, ``robot_to_pc`` or ``pc_to_robot`` drawn as small line icons (no emoji fonts needed)."""
    parts = {"pc": ["pc"], "robot_to_pc": ["robot", "arrow", "pc"], "pc_to_robot": ["pc", "arrow", "robot"]}[
        kind
    ]
    pix = QPixmap(size * len(parts), size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw = {"pc": _draw_pc, "robot": _draw_robot, "arrow": _draw_arrow}
    for i, part in enumerate(parts):
        draw[part](p, i * size, 0, size, color)
    p.end()
    return QIcon(pix)


def action_icon(kind: str, color: str = theme.TEXT, size: int = 20) -> QIcon:
    """``record``, ``stop``, ``play``, ``pause``, ``resume``, ``home``, ``warning``, ``more``."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)
    c = QColor(color)
    if kind == "record":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.RED) if color == theme.TEXT else c)
        p.drawEllipse(QPointF(s / 2, s / 2), s * 0.38, s * 0.38)
    elif kind == "stop":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(s * 0.18, s * 0.18, s * 0.64, s * 0.64), s * 0.1, s * 0.1)
    elif kind in {"play", "resume"}:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawPolygon(
            QPolygonF([QPointF(s * 0.22, s * 0.12), QPointF(s * 0.88, s * 0.5), QPointF(s * 0.22, s * 0.88)])
        )
    elif kind == "pause":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.BLUE) if color == theme.TEXT else c)
        p.drawRoundedRect(QRectF(s * 0.2, s * 0.14, s * 0.2, s * 0.72), s * 0.05, s * 0.05)
        p.drawRoundedRect(QRectF(s * 0.6, s * 0.14, s * 0.2, s * 0.72), s * 0.05, s * 0.05)
    elif kind == "home":
        p.setPen(_pen(color, s * 0.1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(s * 0.12, s * 0.5)
        path.lineTo(s * 0.5, s * 0.14)
        path.lineTo(s * 0.88, s * 0.5)
        p.drawPath(path)
        p.drawRect(QRectF(s * 0.24, s * 0.46, s * 0.52, s * 0.4))
    elif kind == "warning":
        p.setPen(_pen(color, s * 0.09))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(
            QPolygonF([QPointF(s * 0.5, s * 0.1), QPointF(s * 0.94, s * 0.88), QPointF(s * 0.06, s * 0.88)])
        )
        p.drawLine(QPointF(s * 0.5, s * 0.38), QPointF(s * 0.5, s * 0.62))
        p.drawPoint(QPointF(s * 0.5, s * 0.75))
    elif kind == "more":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        for x in (0.22, 0.5, 0.78):
            p.drawEllipse(QPointF(s * x, s / 2), s * 0.08, s * 0.08)
    p.end()
    return QIcon(pix)


# ------------------------------------------------------------------------------------ cards
def card(name: str = "") -> QFrame:
    frame = QFrame()
    frame.setProperty("role", "card")
    if name:
        frame.setObjectName(name)
    return frame


class AutoHideLabel(QLabel):
    """A label that takes no space while its text is empty (helper / reason lines)."""

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text))


def label(
    text: str = "", role: str = "", name: str = "", wrap: bool = False, autohide: bool = False
) -> QLabel:
    lbl = AutoHideLabel(text) if autohide else QLabel(text)
    if autohide:
        lbl.setVisible(bool(text))
    if role:
        lbl.setProperty("role", role)
    if name:
        lbl.setObjectName(name)
    if wrap:
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return lbl


class CollapsibleSection(QWidget):
    """A header button that shows/hides its body (collapsed by default)."""

    toggled = Signal(bool)

    def __init__(self, title: str, subtitle: str = "", *, expanded: bool = False, name: str = "") -> None:
        super().__init__()
        if name:
            self.setObjectName(name)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        self.header = QToolButton()
        self.header.setObjectName(f"{name}Header" if name else "")
        self.header.setCheckable(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.header.setStyleSheet(
            f"QToolButton{{text-align:left;padding:12px 14px;font-weight:600;background:{theme.CARD};"
            f"border:1px solid {theme.BORDER};border-radius:12px;}}"
        )
        self._title, self._subtitle = title, subtitle
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(2, 4, 2, 4)
        self.body_layout.setSpacing(10)
        outer.addWidget(self.header)
        outer.addWidget(self.body)
        self.header.toggled.connect(self._on_toggled)
        self.header.setChecked(expanded)
        self._on_toggled(expanded)

    def _on_toggled(self, on: bool) -> None:
        arrow = "▾" if on else "▸"
        sub = f"\n      {self._subtitle}" if self._subtitle else ""
        self.header.setText(f"{arrow}  {self._title}{sub}".replace("&", "&&"))  # "&" is a Qt mnemonic
        self.body.setVisible(on)
        self.toggled.emit(on)

    @property
    def expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, on: bool) -> None:
        self.header.setChecked(on)


# ------------------------------------------------------------------------------------ timeline
class TimelineBar(QWidget):
    """Behavior timeline in SOURCE seconds: a track, the trimmed range (two draggable handles), a playhead
    and keyframe ticks. Clicking/dragging elsewhere scrubs (display only)."""

    scrubbed = Signal(float)  # source seconds
    trim_changed = Signal(float, float)  # start, end (source seconds)

    HANDLE = 7.0
    MARGIN = 12.0

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("timelineBar")
        self.setMinimumHeight(46)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.duration = 0.0
        self.trim_start = 0.0
        self.trim_end = 0.0
        self.playhead: float | None = None
        self.keyframes: list[float] = []
        self._drag: str | None = None
        self.setEnabled(False)

    def sizeHint(self) -> QSize:
        return QSize(320, 46)

    # -- model
    def set_behavior(
        self, duration: float, trim_start: float, trim_end: float, keyframes: list[float]
    ) -> None:
        self.duration = max(0.0, duration)
        self.trim_start, self.trim_end = trim_start, trim_end
        self.keyframes = list(keyframes)
        self.playhead = None
        self.setEnabled(self.duration > 0)
        self.update()

    def set_playhead(self, t: float | None) -> None:
        self.playhead = None if t is None else min(max(t, 0.0), self.duration)
        self.update()

    # -- geometry
    def _x(self, t: float) -> float:
        w = self.width() - 2 * self.MARGIN
        return self.MARGIN + (0.0 if self.duration <= 0 else w * t / self.duration)

    def _t(self, x: float) -> float:
        w = max(1.0, self.width() - 2 * self.MARGIN)
        return min(max((x - self.MARGIN) / w * self.duration, 0.0), self.duration)

    # -- painting
    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        mid = self.height() / 2
        track = QRectF(self.MARGIN, mid - 9, self.width() - 2 * self.MARGIN, 18)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#0f1720"))
        p.drawRoundedRect(track, 6, 6)
        if self.duration > 0:
            x0, x1 = self._x(self.trim_start), self._x(self.trim_end)
            active = QColor(theme.PURPLE if self.isEnabled() else "#3b3355")
            active.setAlpha(150)
            p.setBrush(active)
            p.drawRoundedRect(QRectF(x0, mid - 9, max(2.0, x1 - x0), 18), 6, 6)
            # a soft "waveform" to make the clip read as motion over time
            p.setPen(_pen("#c4b5fd", 1.2))
            path = QPainterPath()
            for i in range(int(x1 - x0) + 1):
                x = x0 + i
                y = mid + 4.5 * math.sin(i / 9.0) * math.sin(i / 37.0 + 1.0)
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            p.drawPath(path)
            p.setPen(_pen("#fde68a", 2.0))
            for k in self.keyframes:
                xk = self._x(k)
                p.drawLine(QPointF(xk, mid + 9), QPointF(xk, mid + 14))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#ddd6fe"))
            for x in (x0, x1):
                p.drawRoundedRect(QRectF(x - self.HANDLE / 2, mid - 13, self.HANDLE, 26), 3, 3)
            if self.playhead is not None:
                xp = self._x(self.playhead)
                p.setPen(_pen("#ffffff", 2.0))
                p.drawLine(QPointF(xp, mid - 15), QPointF(xp, mid + 15))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#ffffff"))
                p.drawEllipse(QPointF(xp, mid - 15), 4, 4)
        p.end()

    # -- interaction
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.isEnabled() or self.duration <= 0:
            return
        x = event.position().x()
        if abs(x - self._x(self.trim_start)) <= self.HANDLE + 2:
            self._drag = "start"
        elif abs(x - self._x(self.trim_end)) <= self.HANDLE + 2:
            self._drag = "end"
        else:
            self._drag = "scrub"
            self.set_playhead(self._t(x))
            self.scrubbed.emit(self._t(x))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        x = event.position().x()
        near = (
            self.duration > 0
            and min(abs(x - self._x(self.trim_start)), abs(x - self._x(self.trim_end))) <= self.HANDLE + 2
        )
        self.setCursor(Qt.CursorShape.SizeHorCursor if near else Qt.CursorShape.PointingHandCursor)
        if self._drag is None:
            return
        t = self._t(x)
        min_len = min(0.1, self.duration)
        if self._drag == "start":
            self.trim_start = min(t, self.trim_end - min_len)
            self.update()
        elif self._drag == "end":
            self.trim_end = max(t, self.trim_start + min_len)
            self.update()
        else:
            self.set_playhead(t)
            self.scrubbed.emit(t)

    def rounded_trim(self) -> tuple[float, float]:
        """Trim handles rounded to milliseconds, never outside the source: rounding an untouched end
        handle of a 3.6496 s recording must not turn it into 3.650 s (past the end: rejected on save)."""
        start = min(max(0.0, round(self.trim_start, 3)), self.duration)
        end = self.duration if self.duration - self.trim_end < 0.0005 else round(self.trim_end, 3)
        return start, min(max(end, start), self.duration)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag in {"start", "end"}:
            self.trim_changed.emit(*self.rounded_trim())
        self._drag = None


# ------------------------------------------------------------------------------------ status chips
class StatusChips(QWidget):
    """Getting into position → Playing → Returning to rest, with the current step highlighted."""

    STEPS = ("Getting into position", "Playing", "Returning to rest")

    def __init__(self) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.chips: list[QLabel] = []
        for i, text in enumerate(self.STEPS):
            chip = QLabel(text)
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.chips.append(chip)
            row.addWidget(chip, 1)
            if i < len(self.STEPS) - 1:
                arrow = QLabel("→")
                arrow.setProperty("role", "muted")
                row.addWidget(arrow)
        self.set_step(None)

    def set_label(self, index: int, text: str) -> None:
        if self.chips[index].text() != text:
            self.chips[index].setText(text)

    def set_step(self, index: int | None, done_before: bool = True) -> None:
        self.current = index
        for i, chip in enumerate(self.chips):
            if index is not None and i == index:
                style = f"background:#163322;color:{theme.GREEN};border:1px solid {theme.GREEN};"
            elif index is not None and i < index and done_before:
                style = f"background:transparent;color:{theme.GREEN};border:1px solid #1f3a2a;"
            else:
                style = f"background:transparent;color:{theme.MUTED};border:1px solid {theme.BORDER};"
            chip.setStyleSheet(style + "border-radius:10px;padding:4px 6px;font-size:11px;")
